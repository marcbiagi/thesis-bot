# Thesis Experiment: AI vs Rule-Based Investing

A two-year live forward-testing experiment comparing three simulated
portfolios ("arms") that each start with $100,000 of virtual capital:

| Arm         | Decision maker                                             |
|-------------|------------------------------------------------------------|
| `llm`       | Local open-weights LLM via LM Studio (treatment)           |
| `rules`     | Majority vote of 3 classic rule strategies (control)       |
| `benchmark` | SPY buy-and-hold (null hypothesis)                         |

Every trading day at 15:30 ET (30 min before close), `runner.py` builds one
data packet per watchlist ticker (trend indicators, fundamentals, headlines
— identical inputs for both deciding arms), records each arm's
BUY/HOLD/SELL decision with its full rationale and inputs in `thesis.db`,
and applies the decisions to the virtual portfolios.

## Files

- `config.py` — all pinned experiment parameters (watchlist, model, thresholds)
- `runner.py` — one daily cycle (`--force` / `--tickers X Y` for testing)
- `report.py` — scoreboard: equity per arm, decision counts, error count
- `market_data.py` / `rules_arm.py` / `llm_arm.py` / `portfolio.py` / `db.py`
- `com.thesisbot.daily.plist` — launchd schedule (Mon–Fri 15:30 local/ET)
- `thesis.db` — THE DATASET. Back this up off-machine regularly.
  `backups/` keeps the 14 most recent post-run copies automatically.

## Methodological commitments (write these into the thesis)

1. **Unit of observation is the decision, not the trade.** Every signal is
   logged with its inputs, enabling decision-level analysis (hit rates,
   forward returns after signals) — thousands of observations instead of
   ~24 monthly portfolio returns.
2. **Pinned configuration.** Model id, temperature (0), prompt, thresholds
   and watchlist are frozen at experiment start. Each run stores the model
   id and git commit; any change is a documented regime break.
3. **Symmetric information & execution.** Both deciding arms see the same
   packet and get the same fill rule (full fill at decision-time price, no
   costs). Differences between arms are therefore attributable to decisions.
4. **Failures are neutral.** A crashed data source or unreachable LLM
   records HOLD + an error, never a directional signal.
5. **Known limitations.** No transaction costs or slippage (results are an
   upper bound); single market regime (~2 years); paper prices from Yahoo
   Finance; runs skip days when the Mac is off at 15:30 ET (gaps are
   visible in the `runs` table).

## Before the official experiment start

1. Download the final pinned model in LM Studio (e.g. Qwen3 14B Q4) and set
   `LLM_MODEL` in `config.py` to its id (check `lms ls`).
2. Delete `thesis.db` and `backups/` so the dataset starts empty.
3. Commit, and write down the start date + hypotheses (pre-registration).

## Operations

- Status: `python3 report.py`
- Manual run: `python3 runner.py`
- Logs: `logs/runner.log`, `logs/launchd.err.log`
- Schedule on/off:
  `launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.thesisbot.daily.plist`
  `launchctl bootout gui/$UID/com.thesisbot.daily`
- The Mac must be awake at 15:30 ET on trading days (launchd runs a missed
  job on wake from sleep, but not after a shutdown).
