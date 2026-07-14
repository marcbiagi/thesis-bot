"""
report.py — quick experiment health check and scoreboard.

Usage: python3 report.py
"""

import db
from config import ARMS, INITIAL_CASH


def main() -> None:
    conn = db.connect()

    print("=" * 64)
    print("THESIS EXPERIMENT — STATUS REPORT")
    print("=" * 64)

    runs = conn.execute(
        "SELECT status, COUNT(*) n FROM runs GROUP BY status").fetchall()
    total = sum(r["n"] for r in runs)
    print(f"\nRuns: {total} total  "
          + "  ".join(f"[{r['status']}: {r['n']}]" for r in runs))
    last = conn.execute(
        "SELECT run_id, started_utc, status, llm_model FROM runs "
        "ORDER BY run_id DESC LIMIT 1").fetchone()
    if last:
        print(f"Last run: #{last['run_id']} {last['started_utc']} "
              f"({last['status']}, model={last['llm_model']})")

    print(f"\n{'ARM':<11}{'EQUITY':>14}{'RETURN':>10}{'POSITIONS':>11}{'CASH':>14}")
    for arm in ARMS:
        snap = conn.execute(
            "SELECT equity, cash FROM snapshots WHERE arm = ? "
            "ORDER BY id DESC LIMIT 1", (arm,)).fetchone()
        npos = conn.execute(
            "SELECT COUNT(*) n FROM positions WHERE arm = ?", (arm,)).fetchone()["n"]
        if snap:
            ret = (snap["equity"] / INITIAL_CASH - 1) * 100
            print(f"{arm:<11}{snap['equity']:>13,.2f}{ret:>9.2f}%"
                  f"{npos:>11}{snap['cash']:>13,.2f}")
        else:
            print(f"{arm:<11}{'(no snapshot yet)':>14}")

    print("\nDecision counts by arm and signal:")
    for row in conn.execute(
            "SELECT arm, signal, COUNT(*) n FROM decisions "
            "GROUP BY arm, signal ORDER BY arm, signal"):
        print(f"  {row['arm']:<9} {row['signal']:<5} {row['n']}")

    errors = conn.execute(
        "SELECT COUNT(*) n FROM decisions WHERE error IS NOT NULL").fetchone()["n"]
    print(f"\nDecisions with errors: {errors}")

    print("\nMost recent LLM reasoning samples:")
    for row in conn.execute(
            "SELECT ticker, signal, rationale FROM decisions "
            "WHERE arm = 'llm' AND error IS NULL "
            "ORDER BY id DESC LIMIT 3"):
        print(f"  {row['ticker']} -> {row['signal']}: {row['rationale'][:150]}")


if __name__ == "__main__":
    main()
