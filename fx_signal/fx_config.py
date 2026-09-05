#!/usr/bin/env python3
"""
Static config for FX pairs tracked by price_collector.py and
signal_engine.py.

One dict drives all pairs — adding a new pair means adding one entry
here and restarting the pipeline. No code changes in collector, engine,
or bot.

All strategy constants are centralized here so you can tune risk and
execution parameters without touching logic code.
"""

# ------------------------------------------------------------------
# Auto-load .env (next to this file) so every stage can be run directly:
#   python3 telegram_bot.py / signal_engine.py / preflight_check.py ...
# Variables already exported (e.g. by cron / run_pipeline.sh) win —
# setdefault never overrides an existing environment variable.
# Also loads repo-root /opt/market/.env via env_loader when available.
# ------------------------------------------------------------------
import os as _os
import sys as _sys
from pathlib import Path as _Path

_REPO_ROOT = _Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_REPO_ROOT))
try:
    from env_loader import load_env as _load_env
    _load_env(local_dir=_Path(__file__).resolve().parent)
except Exception:
    _env_file = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".env")
    if _os.path.exists(_env_file):
        with open(_env_file) as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _, _v = _line.partition("=")
                    _os.environ.setdefault(_k.strip(), _v.strip().strip('\'"'))


# ------------------------------------------------------------------
# Exports
# ------------------------------------------------------------------
__all__ = [
    "FX_PAIRS",
    "TIMEFRAMES",
    "EMA_FAST",
    "EMA_SLOW",
    "RSI_PERIOD",
    "MACD_SIGNAL",
    "ATR_PERIOD",
    "RSI_OVERBOUGHT",
    "RSI_OVERSOLD",
    "SIGNAL_MODE",
    "SIGNAL_EXPIRY_HOURS",
    "MIN_SL_PIPS",
    "STALE_WARN_PIPS",
    "STALE_SUPPRESS_PIPS",
    "MIN_LIVE_RR",
    "N_COMPLETED_BARS",
    "MIN_BARS_FOR_STATS",
    "CALENDAR_WINDOW_MINUTES",
    "ACCOUNT_BALANCE",
    "ACCOUNT_CURRENCY",
    "RISK_PER_TRADE_PCT",
    "MIN_LOT",
    "CONTRACT_SIZE",
    "validate_config",
]


# ------------------------------------------------------------------
# Pairs
# ------------------------------------------------------------------
FX_PAIRS = {
    # --- Core USD majors ---
    "EURUSD": {
        "yf_symbol":    "EURUSD=X",
        "atr_mult_sl":  1.5,
        "atr_mult_tp":  2.0,
        "pip_size":     0.0001,
        "base":         "EUR",
        "quote":        "USD",
    },
    "GBPUSD": {
        "yf_symbol":    "GBPUSD=X",
        "atr_mult_sl":  1.5,
        "atr_mult_tp":  2.0,
        "pip_size":     0.0001,
        "base":         "GBP",
        "quote":        "USD",
    },
    "USDJPY": {
        "yf_symbol":    "USDJPY=X",
        "atr_mult_sl":  1.2,
        "atr_mult_tp":  1.8,
        "pip_size":     0.01,
        "base":         "USD",
        "quote":        "JPY",
    },

    # --- Additional USD majors ---
    "AUDUSD": {
        "yf_symbol":    "AUDUSD=X",
        "atr_mult_sl":  1.5,
        "atr_mult_tp":  2.0,
        "pip_size":     0.0001,
        "base":         "AUD",
        "quote":        "USD",
    },
    "USDCAD": {
        "yf_symbol":    "USDCAD=X",
        "atr_mult_sl":  1.5,
        "atr_mult_tp":  2.0,
        "pip_size":     0.0001,
        "base":         "USD",
        "quote":        "CAD",
    },
    "USDCHF": {
        "yf_symbol":    "USDCHF=X",
        "atr_mult_sl":  1.5,
        "atr_mult_tp":  2.0,
        "pip_size":     0.0001,
        "base":         "USD",
        "quote":        "CHF",
    },
    "NZDUSD": {
        "yf_symbol":    "NZDUSD=X",
        "atr_mult_sl":  1.5,
        "atr_mult_tp":  2.0,
        "pip_size":     0.0001,
        "base":         "NZD",
        "quote":        "USD",
    },

    # --- JPY crosses ---
    "EURJPY": {
        "yf_symbol":    "EURJPY=X",
        "atr_mult_sl":  1.2,
        "atr_mult_tp":  1.8,
        "pip_size":     0.01,
        "base":         "EUR",
        "quote":        "JPY",
    },
    "GBPJPY": {
        "yf_symbol":    "GBPJPY=X",
        "atr_mult_sl":  1.2,
        "atr_mult_tp":  1.8,
        "pip_size":     0.01,
        "base":         "GBP",
        "quote":        "JPY",
    },

    # --- Non-USD crosses ---
    "EURGBP": {
        "yf_symbol":    "EURGBP=X",
        "atr_mult_sl":  1.5,
        "atr_mult_tp":  2.0,
        "pip_size":     0.0001,
        "base":         "EUR",
        "quote":        "GBP",
    },

    # --- Crypto (Yahoo uses BTC-USD, not BTCUSD=X) ---
    # pip_size=1.0 → 1 "pip" = $1. Global STALE_WARN/SUPPRESS (15/30)
    # are tight for BTC; expect more WARN/SUPPRESS on fast moves until
    # pair-specific thresholds are added. ATR multiples kept modest.
    "BTCUSD": {
        "yf_symbol":    "BTC-USD",
        "atr_mult_sl":  1.5,
        "atr_mult_tp":  2.0,
        "pip_size":     1.0,
        "base":         "BTC",
        "quote":        "USD",
    },
}

# ------------------------------------------------------------------
# Data collection
# ------------------------------------------------------------------
TIMEFRAMES = {
    "1h": {"interval": "1h", "period": "5d"},
    "4h": {"interval": "4h", "period": "1mo"},
}

# How many completed (closed) bars to store per collector run.
# Must be >= 2 so signal_engine always has a previous bar for
# crossover detection, even on first run.
N_COMPLETED_BARS = 5

# ------------------------------------------------------------------
# Indicator parameters (shared across all pairs/timeframes)
# ------------------------------------------------------------------
EMA_FAST       = 8       # Faster response vs original 12
EMA_SLOW       = 21      # Smoother than original 26, still reliable
RSI_PERIOD     = 14
MACD_SIGNAL    = 9
ATR_PERIOD     = 14

RSI_OVERBOUGHT = 70
RSI_OVERSOLD   = 30

# ------------------------------------------------------------------
# Signal logic
# ------------------------------------------------------------------
# Production modes: strict or relaxed.
#   strict  - requires 1h + 4h EMA crossover agreement
#   relaxed - 1h EMA crossover only
SIGNAL_MODE = "strict"

# ------------------------------------------------------------------
# Execution & risk parameters
# ------------------------------------------------------------------
# How many hours until an undelivered signal expires.
SIGNAL_EXPIRY_HOURS = 4

# Minimum stop-loss distance in pips, regardless of ATR calculation.
MIN_SL_PIPS = 5

# Live R:R floor: if executable reward:risk at delivery time
# drops below this, the signal is flagged DO NOT EXECUTE.
MIN_LIVE_RR = 1.0

# Adverse drift thresholds (pips). Favourable drift (pullback toward
# entry) is never penalised.
#   WARN     - yellow flag, R:R degraded but still executable
#   SUPPRESS - red flag, price moved too far, do not trade
STALE_WARN_PIPS     = 15
STALE_SUPPRESS_PIPS = 30

# ------------------------------------------------------------------
# Historical statistics display
# ------------------------------------------------------------------
# Minimum resolved outcomes before historical TP rate is shown in
# the Telegram message. Below this, the number is noise.
MIN_BARS_FOR_STATS = 30

# ------------------------------------------------------------------
# Economic calendar
# ------------------------------------------------------------------
# Look-BACK window in minutes for high-impact news events. The Finnhub
# news proxy only sees events that were already published (there is no
# free forward-looking calendar endpoint), so this is the volatility
# window AFTER a release, not before.
CALENDAR_WINDOW_MINUTES = 60

# ------------------------------------------------------------------
# Position sizing (shown in the Telegram message)
# ------------------------------------------------------------------
# Account balance and currency for the sizing line. Override via env:
#   ACCOUNT_BALANCE=100 ACCOUNT_CURRENCY=USD RISK_PER_TRADE_PCT=1.0
ACCOUNT_BALANCE    = float(_os.environ.get("ACCOUNT_BALANCE", "100"))
ACCOUNT_CURRENCY   = _os.environ.get("ACCOUNT_CURRENCY", "USD")
RISK_PER_TRADE_PCT = float(_os.environ.get("RISK_PER_TRADE_PCT", "1.0"))

# Smallest tradable size (0.01 = 1,000 units at most brokers).
MIN_LOT = 0.01
# Standard contract size (1.00 lot = 100,000 units of base currency).
# Note: BTC sizing in Telegram is illustrative only — brokers use
# different contract units for crypto.
CONTRACT_SIZE = 100_000

# ------------------------------------------------------------------
# Runtime validation
# ------------------------------------------------------------------
def validate_config() -> None:
    """Call at startup to catch config typos before they crash
    the pipeline mid-run.
    """
    required_keys = {
        "yf_symbol", "atr_mult_sl", "atr_mult_tp",
        "pip_size", "base", "quote",
    }
    # FX majors use 0.0001 / 0.01; crypto (e.g. BTC) uses 1.0 ($1 = 1 pip).
    allowed_pip_sizes = {0.0001, 0.01, 1.0}

    for pair, cfg in FX_PAIRS.items():
        missing = required_keys - set(cfg.keys())
        if missing:
            raise ValueError(f"FX_PAIRS['{pair}'] missing keys: {missing}")

        if cfg["pip_size"] not in allowed_pip_sizes:
            raise ValueError(
                f"FX_PAIRS['{pair}'] has unexpected pip_size {cfg['pip_size']}"
            )

        if cfg["atr_mult_sl"] <= 0 or cfg["atr_mult_tp"] <= 0:
            raise ValueError(
                f"FX_PAIRS['{pair}'] ATR multiples must be positive"
            )

    if SIGNAL_MODE not in ("strict", "relaxed"):
        raise ValueError(
            f"SIGNAL_MODE must be 'strict' or 'relaxed', got {SIGNAL_MODE!r}"
        )

    if MIN_LIVE_RR < 0.5:
        raise ValueError(f"MIN_LIVE_RR={MIN_LIVE_RR} is dangerously low")

    if STALE_SUPPRESS_PIPS <= STALE_WARN_PIPS:
        raise ValueError("STALE_SUPPRESS_PIPS must be > STALE_WARN_PIPS")

    if N_COMPLETED_BARS < 2:
        raise ValueError("N_COMPLETED_BARS must be >= 2 for crossover detection")

    if SIGNAL_EXPIRY_HOURS < 1:
        raise ValueError("SIGNAL_EXPIRY_HOURS must be >= 1")

    if ACCOUNT_BALANCE <= 0:
        raise ValueError(f"ACCOUNT_BALANCE={ACCOUNT_BALANCE} must be > 0")

    if not 0 < RISK_PER_TRADE_PCT <= 10:
        raise ValueError(
            f"RISK_PER_TRADE_PCT={RISK_PER_TRADE_PCT} must be in (0, 10]"
        )

    if MIN_LOT <= 0:
        raise ValueError(f"MIN_LOT={MIN_LOT} must be > 0")

    if not ACCOUNT_CURRENCY or len(ACCOUNT_CURRENCY) != 3:
        raise ValueError(f"ACCOUNT_CURRENCY={ACCOUNT_CURRENCY!r} must be a 3-letter code")


# Auto-validate on import so bad config fails immediately
validate_config()
