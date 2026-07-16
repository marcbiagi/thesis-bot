"""
llm_arm.py — the TREATMENT arm: a local open-weights LLM (served by
LM Studio's OpenAI-compatible API) reads the same data packet as the rules
arm and returns BUY / HOLD / SELL with its reasoning.

Reproducibility rules:
  - temperature pinned (config.LLM_TEMPERATURE, default 0)
  - model id pinned (config.LLM_MODEL) and stored on every run
  - the verbatim model output is stored in decisions.raw_response
  - any failure results in HOLD + an error record, never a directional signal
"""

import json
import logging
import re
import subprocess
import time

import requests

from config import (LLM_BASE_URL, LLM_CONTEXT_LEN, LLM_LOAD_TTL_S,
                    LLM_MAX_TOKENS, LLM_MODEL, LLM_RETRIES, LLM_TEMPERATURE,
                    LLM_TIMEOUT_S, LMS_BIN)

logger = logging.getLogger("llm_arm")

SYSTEM_PROMPT = (
    "You are the decision engine of a long-only equity portfolio in an "
    "academic research experiment. Each day you receive one stock's data "
    "packet: price and trend indicators, valuation fundamentals, recent "
    "news headlines, and your current position in the stock.\n"
    "Decide ONE action for a 6-12 month investment horizon:\n"
    "  BUY  - open a position (only if you do not already hold one)\n"
    "  HOLD - keep the current state (also correct when evidence is mixed "
    "or data is missing)\n"
    "  SELL - liquidate the position you hold\n"
    "Respond with ONLY a JSON object, no other text:\n"
    '{"signal": "BUY" | "HOLD" | "SELL", "confidence": <0.0-1.0>, '
    '"reasoning": "<2-4 sentences citing the specific data you weighed>"}'
)

RESPONSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "decision",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "signal": {"type": "string", "enum": ["BUY", "HOLD", "SELL"]},
                "confidence": {"type": "number"},
                "reasoning": {"type": "string"},
            },
            "required": ["signal", "confidence", "reasoning"],
        },
    },
}


def _fmt(v, pct=False):
    if v is None:
        return "N/A"
    return f"{v * 100:.1f}%" if pct else f"{v:.2f}" if isinstance(v, float) else str(v)


def build_user_prompt(packet: dict, position_qty: float, avg_cost: float | None) -> str:
    p = packet
    pos = (f"{position_qty:.4f} shares at avg cost ${avg_cost:.2f}"
           if position_qty > 0 else "none")
    headlines = "\n".join(f"  - {h}" for h in p["headlines"]) or "  (none available)"
    return (
        f"TICKER: {p['ticker']}   (data as of {p['asof_utc']})\n"
        f"CURRENT POSITION: {pos}\n\n"
        f"PRICE & TREND\n"
        f"  price: ${_fmt(p['price'])}   SMA50: ${_fmt(p['sma_short'])}   "
        f"SMA200: ${_fmt(p['sma_long'])}   RSI14: {_fmt(p['rsi'])}\n"
        f"  return 1m: {_fmt(p['ret_1m_pct'])}%   return 3m: {_fmt(p['ret_3m_pct'])}%\n\n"
        f"FUNDAMENTALS\n"
        f"  trailing P/E: {_fmt(p['trailing_pe'])}   forward P/E: {_fmt(p['forward_pe'])}\n"
        f"  ROE: {_fmt(p['roe'], pct=True)}   profit margin: {_fmt(p['profit_margin'], pct=True)}\n"
        f"  revenue growth: {_fmt(p['revenue_growth'], pct=True)}   "
        f"debt/equity: {_fmt(p['debt_to_equity'])}\n\n"
        f"RECENT HEADLINES\n{headlines}\n\n"
        f"Decide now. JSON only."
    )


def ensure_server() -> bool:
    """Check the LM Studio server; try to start it headlessly if it's down."""
    for attempt in range(2):
        try:
            r = requests.get(f"{LLM_BASE_URL}/models", timeout=10)
            if r.ok:
                return True
        except requests.RequestException:
            pass
        if attempt == 0:
            logger.info("LM Studio server down — attempting 'lms server start'")
            try:
                subprocess.run([LMS_BIN, "server", "start"],
                               capture_output=True, timeout=120)
                time.sleep(5)
            except Exception as exc:
                logger.error("could not start LM Studio server: %s", exc)
    return False


def _model_is_loaded() -> bool:
    try:
        out = subprocess.run([LMS_BIN, "ps"], capture_output=True, text=True,
                             timeout=30).stdout
        return LLM_MODEL in out
    except Exception as exc:
        logger.warning("could not check loaded models: %s", exc)
        return False


def ensure_model() -> bool:
    """
    Make sure the pinned model is actually loaded, not just downloaded.
    LM Studio auto-unloads idle JIT-loaded models (default TTL 1h), and a
    request against an unloaded model can 400 instead of reloading it.
    """
    if _model_is_loaded():
        return True
    logger.info("model not loaded — loading %s (context %d, ttl %ds)",
                LLM_MODEL, LLM_CONTEXT_LEN, LLM_LOAD_TTL_S)
    try:
        subprocess.run(
            [LMS_BIN, "load", LLM_MODEL,
             "--context-length", str(LLM_CONTEXT_LEN),
             "--ttl", str(LLM_LOAD_TTL_S), "--yes"],
            capture_output=True, timeout=600,
        )
    except Exception as exc:
        logger.error("model load failed: %s", exc)
    return _model_is_loaded()


def _parse(text: str) -> dict | None:
    """Parse the model's JSON; tolerate stray prose around it."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


def decide(packet: dict, position_qty: float, avg_cost: float | None):
    """
    Returns (signal, rationale, raw_response, latency_ms, error).
    On any failure: HOLD with the error recorded.
    """
    payload = {
        "model": LLM_MODEL,
        "temperature": LLM_TEMPERATURE,
        "max_tokens": LLM_MAX_TOKENS,
        "response_format": RESPONSE_SCHEMA,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(packet, position_qty, avg_cost)},
        ],
    }

    last_error = None
    for attempt in range(1 + LLM_RETRIES):
        start = time.monotonic()
        try:
            resp = requests.post(f"{LLM_BASE_URL}/chat/completions",
                                 json=payload, timeout=LLM_TIMEOUT_S)
            latency_ms = int((time.monotonic() - start) * 1000)
            resp.raise_for_status()
            choice = resp.json()["choices"][0]
            content = choice["message"].get("content") or ""
            # Reasoning models (Gemma 4) also emit a thinking trace — keep it:
            # it's qualitative research data. raw_response stores both parts.
            reasoning_trace = choice["message"].get("reasoning_content") or ""
            raw = json.dumps({"content": content,
                              "reasoning_content": reasoning_trace,
                              "finish_reason": choice.get("finish_reason")})
            parsed = _parse(content)
            if parsed and parsed.get("signal") in ("BUY", "HOLD", "SELL"):
                conf = parsed.get("confidence")
                rationale = (f"[conf {conf}] " if conf is not None else "") + \
                    str(parsed.get("reasoning", "")).strip()
                return parsed["signal"], rationale, raw, latency_ms, None
            last_error = (f"empty/unparseable content (attempt {attempt + 1}, "
                          f"finish_reason={choice.get('finish_reason')})")
            logger.warning("%s: %s: %r", packet["ticker"], last_error, content[:200])
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("%s: LLM call failed (attempt %d): %s",
                           packet["ticker"], attempt + 1, last_error)
            # A 400/404 usually means the model got unloaded — reload it
            # before burning the remaining retries.
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (400, 404):
                ensure_model()

    return "HOLD", "LLM unavailable — defaulting to HOLD (no directional bias)", \
        None, None, last_error
