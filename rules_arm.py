"""
rules_arm.py — the CONTROL arm: a deterministic ensemble of three classic
rule-based strategies (trend-following, value screen, news-keyword
sentiment), majority-voted.

Methodological rule: missing data or a failed sub-strategy votes HOLD,
never SELL — a crash must not masquerade as a bearish opinion.
"""

from collections import Counter

from config import (PE_BUY_BELOW, PE_SELL_ABOVE, ROE_BUY_ABOVE, ROE_SELL_BELOW,
                    RSI_OVERBOUGHT, SENTIMENT_BUY_AT, SENTIMENT_SELL_AT)

BULLISH = {
    "beat", "beats", "surge", "surges", "soar", "soars", "rally", "rallies",
    "record", "upgrade", "upgraded", "outperform", "strong", "growth",
    "profit", "profits", "jump", "jumps", "rise", "rises", "gain", "gains",
    "bullish", "breakthrough", "expansion", "momentum", "recovery",
    "optimistic", "dividend", "buyback",
}
BEARISH = {
    "miss", "misses", "fall", "falls", "drop", "drops", "plunge", "plunges",
    "decline", "declines", "slump", "slumps", "crash", "selloff", "loss",
    "losses", "downgrade", "downgraded", "underperform", "weak", "bearish",
    "lawsuit", "probe", "investigation", "recall", "layoff", "layoffs",
    "bankruptcy", "warning", "cuts", "fears", "risk",
}


def technical_vote(p: dict) -> tuple[str, str]:
    sma_s, sma_l, rsi = p["sma_short"], p["sma_long"], p["rsi"]
    if sma_s is None or sma_l is None or rsi is None:
        return "HOLD", "technical: insufficient history"
    if sma_s > sma_l and rsi < RSI_OVERBOUGHT:
        return "BUY", f"technical: uptrend (SMA50 {sma_s} > SMA200 {sma_l}), RSI {rsi} ok"
    if sma_s <= sma_l:
        return "SELL", f"technical: downtrend (SMA50 {sma_s} <= SMA200 {sma_l})"
    return "HOLD", f"technical: uptrend but overbought (RSI {rsi})"


def fundamental_vote(p: dict) -> tuple[str, str]:
    pe, roe = p["trailing_pe"], p["roe"]
    if pe is None or roe is None:
        return "HOLD", "fundamental: P/E or ROE unavailable"
    if pe < PE_BUY_BELOW and roe > ROE_BUY_ABOVE:
        return "BUY", f"fundamental: P/E {pe:.1f} < {PE_BUY_BELOW:.0f}, ROE {roe:.0%} > {ROE_BUY_ABOVE:.0%}"
    if pe > PE_SELL_ABOVE or roe < ROE_SELL_BELOW:
        return "SELL", f"fundamental: P/E {pe:.1f} or ROE {roe:.0%} outside limits"
    return "HOLD", f"fundamental: P/E {pe:.1f}, ROE {roe:.0%} in neutral zone"


def sentiment_vote(p: dict) -> tuple[str, str]:
    headlines = p["headlines"]
    if not headlines:
        return "HOLD", "sentiment: no headlines available"
    score = 0
    for h in headlines:
        words = h.lower().replace(",", " ").replace(":", " ").split()
        score += sum(1 for w in words if w in BULLISH)
        score -= sum(1 for w in words if w in BEARISH)
    if score >= SENTIMENT_BUY_AT:
        return "BUY", f"sentiment: net score {score:+d} across {len(headlines)} headlines"
    if score <= SENTIMENT_SELL_AT:
        return "SELL", f"sentiment: net score {score:+d} across {len(headlines)} headlines"
    return "HOLD", f"sentiment: neutral score {score:+d} across {len(headlines)} headlines"


def decide(packet: dict) -> tuple[str, str]:
    """Majority vote of the three sub-strategies; no majority -> HOLD."""
    votes, reasons = [], []
    for fn in (technical_vote, fundamental_vote, sentiment_vote):
        try:
            vote, reason = fn(packet)
        except Exception as exc:
            vote, reason = "HOLD", f"{fn.__name__} crashed: {exc}"
        votes.append(vote)
        reasons.append(f"{reason} -> {vote}")

    tally = Counter(votes)
    if tally["BUY"] >= 2:
        signal = "BUY"
    elif tally["SELL"] >= 2:
        signal = "SELL"
    else:
        signal = "HOLD"
    rationale = " | ".join(reasons) + f" | vote {dict(tally)} => {signal}"
    return signal, rationale
