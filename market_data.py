"""
market_data.py — builds the daily "data packet" for one ticker.

Both experiment arms receive the SAME packet, so they act on symmetric
information. Everything comes from Yahoo Finance via yfinance (keyless),
which keeps the pipeline free of API-key expiry risk over the two years.
"""

import logging
import time
from datetime import datetime, timezone

import yfinance as yf

from config import MAX_HEADLINES, RSI_WINDOW, SMA_LONG, SMA_SHORT

logger = logging.getLogger("market_data")


def _rsi(close, window: int = RSI_WINDOW) -> float | None:
    """Latest RSI using Wilder's exponential smoothing."""
    if len(close) < window + 1:
        return None
    delta = close.diff()
    gain = delta.clip(lower=0.0).ewm(alpha=1.0 / window, min_periods=window).mean()
    loss = (-delta.clip(upper=0.0)).ewm(alpha=1.0 / window, min_periods=window).mean()
    g, l = gain.iloc[-1], loss.iloc[-1]
    if l == 0:
        return 100.0
    return round(100.0 - 100.0 / (1.0 + g / l), 2)


def _headlines(tk) -> list[str]:
    """Most recent news titles; yfinance nests them differently by version."""
    titles = []
    try:
        for item in tk.news or []:
            title = item.get("title") or (item.get("content") or {}).get("title")
            if title:
                titles.append(title.strip())
            if len(titles) >= MAX_HEADLINES:
                break
    except Exception as exc:
        logger.warning("news fetch failed: %s", exc)
    return titles


def get_packet(ticker: str) -> dict:
    """
    Assemble the decision packet: price/trend, valuation, and news.
    Missing fields are None — arms must treat missing data as uncertainty
    (HOLD), never as a directional signal.
    """
    tk = yf.Ticker(ticker)
    packet = {
        "ticker": ticker,
        "asof_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "price": None, "sma_short": None, "sma_long": None, "rsi": None,
        "ret_1m_pct": None, "ret_3m_pct": None,
        "trailing_pe": None, "forward_pe": None, "roe": None,
        "profit_margin": None, "revenue_growth": None, "debt_to_equity": None,
        "headlines": [],
    }

    try:
        hist = tk.history(period="1y", interval="1d", auto_adjust=True)
        close = hist["Close"].dropna()
        if len(close) >= SMA_LONG:
            packet["sma_short"] = round(close.rolling(SMA_SHORT).mean().iloc[-1], 2)
            packet["sma_long"] = round(close.rolling(SMA_LONG).mean().iloc[-1], 2)
        if len(close) > 0:
            packet["price"] = round(float(close.iloc[-1]), 2)
            packet["rsi"] = _rsi(close)
        if len(close) > 21:
            packet["ret_1m_pct"] = round((close.iloc[-1] / close.iloc[-22] - 1) * 100, 2)
        if len(close) > 63:
            packet["ret_3m_pct"] = round((close.iloc[-1] / close.iloc[-64] - 1) * 100, 2)
    except Exception as exc:
        logger.warning("%s: price history failed: %s", ticker, exc)

    try:
        info = tk.info or {}
        packet["trailing_pe"] = info.get("trailingPE")
        packet["forward_pe"] = info.get("forwardPE")
        packet["roe"] = info.get("returnOnEquity")
        packet["profit_margin"] = info.get("profitMargins")
        packet["revenue_growth"] = info.get("revenueGrowth")
        packet["debt_to_equity"] = info.get("debtToEquity")
    except Exception as exc:
        logger.warning("%s: fundamentals failed: %s", ticker, exc)

    packet["headlines"] = _headlines(tk)

    # Total-return accounting: if today is this stock's ex-dividend date,
    # holders get the per-share amount credited to cash.
    packet["dividend_today"] = 0.0
    try:
        divs = tk.dividends
        if divs is not None and len(divs):
            from zoneinfo import ZoneInfo
            et_today = datetime.now(ZoneInfo("America/New_York")).date()
            last_ex = divs.index[-1].date()
            if last_ex == et_today:
                packet["dividend_today"] = round(float(divs.iloc[-1]), 4)
    except Exception as exc:
        logger.warning("%s: dividend check failed: %s", ticker, exc)

    # Prefer a live quote over yesterday's close when the market is open.
    try:
        live = tk.fast_info.last_price
        if live:
            packet["price"] = round(float(live), 2)
    except Exception:
        pass

    return packet


def get_tbill_rate() -> float | None:
    """Latest 13-week T-bill annualized yield (%) via ^IRX — the rate idle
    virtual cash earns. None if unavailable (caller falls back to last known)."""
    try:
        hist = yf.Ticker("^IRX").history(period="5d", interval="1d")
        if not hist.empty:
            return round(float(hist["Close"].dropna().iloc[-1]), 3)
    except Exception as exc:
        logger.warning("T-bill rate fetch failed: %s", exc)
    return None


def _last_spy_bar(retries: int = 3) -> datetime | None:
    """Latest SPY 1-minute bar, with retries — Yahoo has transient hiccups."""
    for attempt in range(retries):
        try:
            bars = yf.Ticker("SPY").history(period="1d", interval="1m")
            if not bars.empty:
                return bars.index[-1].to_pydatetime().astimezone(timezone.utc)
            logger.warning("market clock: empty SPY bars (attempt %d)", attempt + 1)
        except Exception as exc:
            logger.warning("market clock check failed (attempt %d): %s",
                           attempt + 1, exc)
        time.sleep(15 * (attempt + 1))
    return None


def _in_regular_hours_et() -> bool:
    """Calendar fallback: weekday 9:30-16:00 ET (ignores holidays)."""
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo("America/New_York"))
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= minutes < 16 * 60


def is_market_open() -> bool:
    """
    Keyless market-clock check: if SPY printed a 1-minute bar in the last
    20 minutes, the US market is trading.

    FAIL-OPEN: if the data source errors out entirely, fall back to the
    weekday/hours calendar. A Yahoo outage must never be mistaken for
    "market closed" — a wrongly-skipped day is lost data, while a run on
    an unrecognized holiday just records decisions at stale prices (and is
    visible in the data as such).
    """
    last = _last_spy_bar()
    if last is None:
        fallback = _in_regular_hours_et()
        logger.warning("market clock unavailable — calendar fallback says %s",
                       "OPEN" if fallback else "CLOSED")
        return fallback
    return (datetime.now(timezone.utc) - last).total_seconds() < 20 * 60


def had_session_today() -> bool:
    """True if the US market traded today (ET calendar day), open or closed."""
    from zoneinfo import ZoneInfo

    last = _last_spy_bar()
    if last is None:
        # Data source down: assume a weekday had a session (holidays are the
        # rare false positive; the catch-up run then just prices at last close).
        return datetime.now(ZoneInfo("America/New_York")).weekday() < 5
    et = ZoneInfo("America/New_York")
    return last.astimezone(et).date() == datetime.now(et).date()
