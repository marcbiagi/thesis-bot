"""
config.py — single source of truth for every experiment parameter.

METHODOLOGY NOTE: once the experiment officially starts, nothing in this
file may change without recording it as a documented regime break in the
thesis. Every run stores LLM_MODEL and the git commit hash alongside each
decision, so any change is visible in the data.
"""

from pathlib import Path

# --- Experiment universe ------------------------------------------------------
# 25 US large caps, diversified across sectors, chosen before the experiment
# starts (no additions/removals afterwards — survivorship stays documented).
WATCHLIST = [
    # Tech / communication
    "AAPL", "MSFT", "NVDA", "GOOGL", "META", "CRM", "NFLX",
    # Consumer
    "AMZN", "TSLA", "WMT", "HD", "MCD", "KO", "PG", "DIS",
    # Financials
    "JPM", "BAC", "V",
    # Healthcare
    "JNJ", "UNH", "PFE",
    # Industrials / energy
    "CAT", "BA", "XOM", "CVX",
]
BENCHMARK_TICKER = "SPY"  # buy-and-hold null hypothesis

# --- Experiment arms ----------------------------------------------------------
ARM_LLM = "llm"            # treatment: local open-weights LLM decides
ARM_RULES = "rules"        # control: deterministic rule ensemble
ARM_BENCHMARK = "benchmark"  # SPY buy-and-hold
ARMS = [ARM_LLM, ARM_RULES, ARM_BENCHMARK]

# --- Virtual portfolio simulation ----------------------------------------------
INITIAL_CASH = 100_000.0   # each arm starts with the same virtual capital
TARGET_WEIGHT = 0.04       # a BUY opens a position sized at 4% of arm equity
MIN_ORDER_NOTIONAL = 10.0  # skip dust orders

# --- LLM arm (LM Studio local server, OpenAI-compatible) -----------------------
LLM_BASE_URL = "http://localhost:1234/v1"
# PINNED for the duration of the experiment. Change ONLY before the official
# start date (download the final model in LM Studio, put its id here).
# Gemma 4 12B QAT: official Google quantization-aware checkpoint, chosen
# July 2026 as the best sub-15B open-weights reasoner that fits 16 GB.
LLM_MODEL = "google/gemma-4-12b-qat"
LLM_TEMPERATURE = 0.0      # deterministic as possible, for reproducibility
# Gemma 4 is a reasoning model: it spends tokens thinking before answering.
# The budget must cover reasoning + the final JSON, or content comes back
# empty. 1500 proved too tight (~4% of day-1 decisions truncated).
LLM_MAX_TOKENS = 3000
LLM_TIMEOUT_S = 300        # local inference on an M4 can be slow; be patient
LLM_RETRIES = 2
LMS_BIN = "/Users/marcobiagi/.lmstudio/bin/lms"
LLM_CONTEXT_LEN = 8192     # part of the pinned model configuration
LLM_LOAD_TTL_S = 10800     # keep the model loaded 3h after last use

# --- Rule-based arm thresholds --------------------------------------------------
SMA_SHORT = 50
SMA_LONG = 200
RSI_WINDOW = 14
RSI_OVERBOUGHT = 70.0
PE_BUY_BELOW = 30.0
PE_SELL_ABOVE = 45.0
ROE_BUY_ABOVE = 0.15
ROE_SELL_BELOW = 0.05
SENTIMENT_BUY_AT = 2       # net lexicon score >= +2 -> BUY
SENTIMENT_SELL_AT = -2     # net lexicon score <= -2 -> SELL
MAX_HEADLINES = 8

# --- Paths ----------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "thesis.db"
LOG_DIR = ROOT / "logs"
BACKUP_DIR = ROOT / "backups"
BACKUPS_TO_KEEP = 14
