"""
portfolio.py — virtual portfolio accounting, identical for every arm.

All three arms are simulated with the same fill rule (fill the full order
at the decision-time price, no costs), so differences between arms come
from DECISIONS only, never from execution mechanics. The absence of
transaction costs and slippage is a documented limitation of the study.
"""

import logging

from config import INITIAL_CASH, MIN_ORDER_NOTIONAL, TARGET_WEIGHT
from db import utcnow

logger = logging.getLogger("portfolio")


def get_cash(conn, arm: str) -> float:
    return conn.execute("SELECT cash FROM arms WHERE arm = ?", (arm,)).fetchone()["cash"]


def _set_cash(conn, arm: str, cash: float) -> None:
    conn.execute("UPDATE arms SET cash = ? WHERE arm = ?", (cash, arm))


def get_position(conn, arm: str, ticker: str) -> tuple[float, float | None]:
    row = conn.execute(
        "SELECT qty, avg_cost FROM positions WHERE arm = ? AND ticker = ?",
        (arm, ticker),
    ).fetchone()
    return (row["qty"], row["avg_cost"]) if row else (0.0, None)


def equity(conn, arm: str, prices: dict[str, float]) -> float:
    """Cash + positions marked at today's prices (last known price if absent)."""
    total = get_cash(conn, arm)
    for row in conn.execute("SELECT ticker, qty, avg_cost FROM positions WHERE arm = ?", (arm,)):
        px = prices.get(row["ticker"]) or row["avg_cost"]
        total += row["qty"] * px
    return total


def _record_trade(conn, run_id, arm, ticker, side, qty, price) -> None:
    conn.execute(
        "INSERT INTO trades (run_id, arm, ticker, side, qty, price, notional, created_utc)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, arm, ticker, side, qty, price, qty * price, utcnow()),
    )


def apply_signal(conn, run_id: int, arm: str, ticker: str, signal: str,
                 price: float | None, arm_equity: float) -> str:
    """Translate a signal into (at most) one simulated fill. Returns a note."""
    if price is None or price <= 0:
        return "no price available — no action"
    qty, _ = get_position(conn, arm, ticker)

    if signal == "BUY":
        if qty > 0:
            return "already holding — no action"
        cash = get_cash(conn, arm)
        notional = min(cash, TARGET_WEIGHT * arm_equity)
        if notional < MIN_ORDER_NOTIONAL:
            return f"insufficient cash (${cash:.2f}) — no action"
        buy_qty = notional / price
        conn.execute(
            "INSERT INTO positions (arm, ticker, qty, avg_cost) VALUES (?, ?, ?, ?)",
            (arm, ticker, buy_qty, price),
        )
        _set_cash(conn, arm, cash - notional)
        _record_trade(conn, run_id, arm, ticker, "BUY", buy_qty, price)
        conn.commit()
        return f"bought {buy_qty:.4f} @ ${price:.2f} (${notional:.2f})"

    if signal == "SELL":
        if qty <= 0:
            return "no position — no action"
        proceeds = qty * price
        conn.execute("DELETE FROM positions WHERE arm = ? AND ticker = ?", (arm, ticker))
        _set_cash(conn, arm, get_cash(conn, arm) + proceeds)
        _record_trade(conn, run_id, arm, ticker, "SELL", qty, price)
        conn.commit()
        return f"sold {qty:.4f} @ ${price:.2f} (${proceeds:.2f})"

    return "hold — no action"


def init_benchmark(conn, run_id: int, arm: str, ticker: str, price: float | None) -> str:
    """One-time full-cash SPY purchase; afterwards the benchmark never trades."""
    qty, _ = get_position(conn, arm, ticker)
    if qty > 0 or price is None or price <= 0:
        return "benchmark already invested" if qty > 0 else "no benchmark price"
    cash = get_cash(conn, arm)
    buy_qty = cash / price
    conn.execute(
        "INSERT INTO positions (arm, ticker, qty, avg_cost) VALUES (?, ?, ?, ?)",
        (arm, ticker, buy_qty, price),
    )
    _set_cash(conn, arm, 0.0)
    _record_trade(conn, run_id, arm, ticker, "BUY", buy_qty, price)
    conn.commit()
    return f"benchmark invested: {buy_qty:.4f} {ticker} @ ${price:.2f}"


def snapshot(conn, run_id: int, arm: str, prices: dict[str, float]) -> float:
    eq = equity(conn, arm, prices)
    conn.execute(
        "INSERT INTO snapshots (run_id, arm, cash, equity, created_utc) VALUES (?, ?, ?, ?, ?)",
        (run_id, arm, get_cash(conn, arm), eq, utcnow()),
    )
    conn.commit()
    return eq
