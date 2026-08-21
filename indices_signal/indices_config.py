# /opt/market/indices_signal/indices_config.py

import os

# ------------------------------------------------------------------
# Auto-load .env (next to this file) so every stage can be run
# directly without manually exporting variables. Values already in
# the environment (cron / run_pipeline.sh) always win.
# ------------------------------------------------------------------
_env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_file):
    with open(_env_file) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                os.environ.setdefault(_k.strip(), _v.strip())


# ============================================================
# INSTRUMENT CONFIGURATION
# ============================================================

TICKERS = {
    "GOLD": {
        "yahoo_symbol": "GC=F",
        "display_name": "Gold (XAU/USD)",
        "emoji": "🥇",

        # ATR-based stop loss / take profit
        "atr_multiplier_sl": 2.0,
        "atr_multiplier_tp": 3.5,

        # Ignore extremely quiet conditions
        "min_atr": 3.0,
    },

    "US30": {
        "yahoo_symbol": "^DJI",
        "display_name": "US30 (Dow Jones)",
        "emoji": "🇺🇸",

        "atr_multiplier_sl": 2.0,
        "atr_multiplier_tp": 3.5,
        "min_atr": 50.0,
    },

    "US100": {
        "yahoo_symbol": "^NDX",
        "display_name": "US100 (Nasdaq)",
        "emoji": "💻",

        "atr_multiplier_sl": 2.0,
        "atr_multiplier_tp": 3.5,
        "min_atr": 30.0,
    },
}


# ============================================================
# TIMEFRAMES / DATA
# ============================================================

PRIMARY_TIMEFRAME = "4h"
TREND_TIMEFRAME = "1d"

# Yahoo hourly data is limited, especially for indices.
# 60 days is enough for the current 4H strategy.
INTRADAY_LOOKBACK_DAYS = 60
DAILY_LOOKBACK_DAYS = 365


# ============================================================
# INDICATOR PERIODS
# ============================================================

EMA_FAST = 20
EMA_MEDIUM = 50
EMA_LONG = 100

RSI_PERIOD = 14
ATR_PERIOD = 14
ADX_PERIOD = 14

MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

BB_PERIOD = 20
BB_STD = 2


# ============================================================
# SIGNAL RULES
# ============================================================

SIGNAL_CONFIG = {
    # Minimum score required before generating a signal
    "min_score": 75,

    # Minimum historical candles required
    #
    # Yahoo only provides limited intraday history for indices.
    # 100 4H candles is enough for EMA100 and indicator warm-up.
    "min_4h_candles": 100,
    "min_1d_candles": 50,

    # Trend strength
    "min_adx": 25,

    # RSI zones
    "min_rsi_buy": 40,
    "max_rsi_buy": 65,

    "min_rsi_sell": 35,
    "max_rsi_sell": 60,

    # Duplicate protection
    #
    # Same direction signals are suppressed for 8 hours.
    # An opposite signal may still be sent.
    "cooldown_hours": 8,
}


# ============================================================
# DATABASE
# ============================================================

DB_PATH = "/opt/market/indices_signal/prices.db"


# ============================================================
# TELEGRAM
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

MESSAGE_PREFIX = "📊 [INDICES]"


# ============================================================
# MARKET HOURS
#
# Used only as a coarse filter before analysis.
# UTC times.
# US market hours vary with DST, so the collector/closed-candle
# logic remains the authoritative source of usable candles.
# ============================================================

MARKET_HOURS = {
    "GOLD": {
        "start": 0,
        "end": 23,
    },

    "US30": {
        "start": 12,
        "end": 21,
    },

    "US100": {
        "start": 12,
        "end": 21,
    },
}
