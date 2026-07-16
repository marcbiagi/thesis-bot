"""
runner.py — one daily experiment cycle.

For every watchlist ticker: build the shared data packet, collect a
decision from each arm (LLM treatment, rules control), apply both to their
virtual portfolios, then snapshot all arm equities (benchmark included).

Usage:
    python3 runner.py                  # normal daily run (skips if market closed)
    python3 runner.py --force          # run even if the market is closed (testing)
    python3 runner.py --tickers AAPL MSFT   # subset (testing)
"""

import argparse
import json
import logging
import shutil
import subprocess
import sys
from datetime import datetime

import config
import dashboard
import db
import llm_arm
import market_data
import portfolio
import rules_arm

config.LOG_DIR.mkdir(exist_ok=True)
config.BACKUP_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(config.LOG_DIR / "runner.log"),
    ],
)
logger = logging.getLogger("runner")


def code_version() -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(config.ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def backup_db() -> None:
    """Copy the DB after each successful run; keep the newest N copies."""
    stamp = datetime.now().strftime("%Y%m%d")
    shutil.copy2(config.DB_PATH, config.BACKUP_DIR / f"thesis-{stamp}.db")
    backups = sorted(config.BACKUP_DIR.glob("thesis-*.db"))
    for old in backups[: -config.BACKUPS_TO_KEEP]:
        old.unlink()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true",
                        help="run even if the market is closed (testing only)")
    parser.add_argument("--tickers", nargs="*", default=None,
                        help="subset of tickers (testing only)")
    args = parser.parse_args()

    conn = db.connect()

    run_note = "forced test run" if args.force else ""
    if not market_data.is_market_open() and not args.force:
        # Catch-up: the Mac may have been asleep at 15:30. If today's session
        # happened and no run captured it, run now at close prices rather
        # than losing the day.
        if market_data.had_session_today() and not db.completed_run_today(conn):
            run_note = "late catch-up run (after close, close prices)"
            logger.info("Missed the in-session window — running late catch-up.")
        else:
            run_id = db.start_run(conn, config.LLM_MODEL, code_version(),
                                  notes="market closed")
            db.finish_run(conn, run_id, "skipped_market_closed")
            dashboard.generate()
            logger.info("Market closed — run %d recorded as skipped.", run_id)
            return 0

    llm_up = llm_arm.ensure_server() and llm_arm.ensure_model()
    if not llm_up:
        logger.error("LM Studio server/model unavailable — LLM arm will "
                     "record HOLD + error for every ticker this run.")

    tickers = [t.upper() for t in (args.tickers or config.WATCHLIST)]
    run_id = db.start_run(conn, config.LLM_MODEL, code_version(), notes=run_note)
    logger.info("Run %d started | model=%s | code=%s | %d tickers",
                run_id, config.LLM_MODEL, code_version(), len(tickers))

    # --- Phase 1: gather all packets (shared inputs for both arms) ----------
    packets, prices = {}, {}
    for t in tickers:
        p = market_data.get_packet(t)
        packets[t] = p
        if p["price"]:
            prices[t] = p["price"]
        logger.info("%s packet: price=%s pe=%s rsi=%s headlines=%d",
                    t, p["price"], p["trailing_pe"], p["rsi"], len(p["headlines"]))

    # Benchmark price (needed for its snapshot / one-time initialization).
    spy = market_data.get_packet(config.BENCHMARK_TICKER)
    if spy["price"]:
        prices[config.BENCHMARK_TICKER] = spy["price"]

    # Arm equities are fixed at the start of the run so position sizing is
    # not affected by the order in which tickers are processed.
    eq_llm = portfolio.equity(conn, config.ARM_LLM, prices)
    eq_rules = portfolio.equity(conn, config.ARM_RULES, prices)

    # --- Phase 2: decisions + simulated execution ---------------------------
    for t in tickers:
        p = packets[t]
        inputs_json = json.dumps(p)

        # Control arm: deterministic rules.
        signal, rationale = rules_arm.decide(p)
        action = portfolio.apply_signal(conn, run_id, config.ARM_RULES, t,
                                        signal, p["price"], eq_rules)
        db.record_decision(conn, run_id, config.ARM_RULES, t, signal,
                           p["price"], rationale, inputs_json=inputs_json)
        logger.info("[RULES] %s -> %s | %s", t, signal, action)

        # Treatment arm: local LLM.
        qty, avg_cost = portfolio.get_position(conn, config.ARM_LLM, t)
        signal, rationale, raw, latency_ms, error = llm_arm.decide(p, qty, avg_cost)
        action = portfolio.apply_signal(conn, run_id, config.ARM_LLM, t,
                                        signal, p["price"], eq_llm)
        db.record_decision(conn, run_id, config.ARM_LLM, t, signal,
                           p["price"], rationale, inputs_json=inputs_json,
                           raw_response=raw, latency_ms=latency_ms, error=error)
        logger.info("[LLM]   %s -> %s (%sms) | %s", t, signal, latency_ms, action)

    # --- Phase 3: benchmark + snapshots -------------------------------------
    note = portfolio.init_benchmark(conn, run_id, config.ARM_BENCHMARK,
                                    config.BENCHMARK_TICKER, spy["price"])
    logger.info("[BENCH] %s", note)

    for arm in config.ARMS:
        eq = portfolio.snapshot(conn, run_id, arm, prices)
        logger.info("[EQUITY] %-9s $%s", arm, f"{eq:,.2f}")

    db.finish_run(conn, run_id, "completed")
    backup_db()
    dashboard.generate()
    logger.info("Run %d completed — dashboard updated.", run_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
