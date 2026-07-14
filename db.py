"""
db.py — SQLite persistence layer.

Design principle: the DECISION, not the trade, is the unit of observation.
Every arm's signal for every ticker on every run is stored, together with
the full input packet it saw, so the thesis can analyze thousands of
decision-level observations (hit rates, forward returns) rather than a
single equity curve.
"""

import sqlite3
from datetime import datetime, timezone

from config import ARMS, DB_PATH, INITIAL_CASH

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    started_utc  TEXT NOT NULL,
    finished_utc TEXT,
    status       TEXT NOT NULL DEFAULT 'running',
    llm_model    TEXT,
    code_version TEXT,
    notes        TEXT
);

CREATE TABLE IF NOT EXISTS decisions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL REFERENCES runs(run_id),
    arm          TEXT NOT NULL,
    ticker       TEXT NOT NULL,
    signal       TEXT NOT NULL,             -- BUY / HOLD / SELL
    price        REAL,                      -- price at decision time
    rationale    TEXT,                      -- human-readable reasoning
    inputs_json  TEXT,                      -- full data packet the arm saw
    raw_response TEXT,                      -- LLM only: verbatim model output
    latency_ms   INTEGER,
    error        TEXT,                      -- non-null if the arm failed
    created_utc  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES runs(run_id),
    arm         TEXT NOT NULL,
    ticker      TEXT NOT NULL,
    side        TEXT NOT NULL,              -- BUY / SELL
    qty         REAL NOT NULL,
    price       REAL NOT NULL,
    notional    REAL NOT NULL,
    created_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    arm      TEXT NOT NULL,
    ticker   TEXT NOT NULL,
    qty      REAL NOT NULL,
    avg_cost REAL NOT NULL,
    PRIMARY KEY (arm, ticker)
);

CREATE TABLE IF NOT EXISTS arms (
    arm  TEXT PRIMARY KEY,
    cash REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES runs(run_id),
    arm         TEXT NOT NULL,
    cash        REAL NOT NULL,
    equity      REAL NOT NULL,
    created_utc TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_decisions_arm_ticker ON decisions(arm, ticker);
CREATE INDEX IF NOT EXISTS idx_snapshots_arm ON snapshots(arm);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # Seed each arm's cash balance exactly once.
    for arm in ARMS:
        conn.execute(
            "INSERT OR IGNORE INTO arms (arm, cash) VALUES (?, ?)",
            (arm, INITIAL_CASH),
        )
    conn.commit()
    return conn


def start_run(conn, llm_model: str, code_version: str, notes: str = "") -> int:
    cur = conn.execute(
        "INSERT INTO runs (started_utc, llm_model, code_version, notes) "
        "VALUES (?, ?, ?, ?)",
        (utcnow(), llm_model, code_version, notes),
    )
    conn.commit()
    return cur.lastrowid


def finish_run(conn, run_id: int, status: str) -> None:
    conn.execute(
        "UPDATE runs SET finished_utc = ?, status = ? WHERE run_id = ?",
        (utcnow(), status, run_id),
    )
    conn.commit()


def record_decision(conn, run_id, arm, ticker, signal, price, rationale,
                    inputs_json=None, raw_response=None, latency_ms=None,
                    error=None) -> None:
    conn.execute(
        "INSERT INTO decisions (run_id, arm, ticker, signal, price, rationale,"
        " inputs_json, raw_response, latency_ms, error, created_utc)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, arm, ticker, signal, price, rationale, inputs_json,
         raw_response, latency_ms, error, utcnow()),
    )
    conn.commit()
