# /opt/market/indices_signal/indices_config.py

import os
from pathlib import Path as _Path

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
                os.environ.setdefault(_k.strip(), _v.strip().strip('\'"'))


# ============================================================
# INSTRUMENT CONFIGURATION
# ============================================================

TICKERS = {
    "GOLD": {
        # NOTE: GC=F is the COMEX front-month gold FUTURES contract, NOT the
        # spot XAU/USD price your broker shows. The two can diverge by $5–$20+
        # due to contango/backwardation and will gap on futures rollover days
        # (see ROLLOVER_DATES below). This pipeline uses GC=F because Yahoo
        # does not reliably serve spot XAU/USD; treat every price, SL and TP
        # as a *reference*, not a broker-exact level.
        "yahoo_symbol": "GC=F",
        "display_name": "Gold (COMEX GC=F — not spot XAU/USD)",
        "emoji": "🥇",
        "data_source_note": (
            "Price source is COMEX gold futures (GC=F), NOT spot XAU/USD. "
            "Expect a $5–$20 basis vs. your broker; confirm live spot price "
            "before entry and around futures roll dates."
        ),

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

    # RSI zones (scoring bonus — inside the zone earns +15)
    "min_rsi_buy": 40,
    "max_rsi_buy": 65,

    "min_rsi_sell": 35,
    "max_rsi_sell": 60,

    # RSI hard veto (blocks the signal regardless of score)
    #
    # The scoring zones above only decide whether RSI ADDS points.
    # Without a veto, the other five checks alone can reach
    # min_score (25+15+15+10+10 = 75), so a BUY could fire at
    # RSI 76+ — textbook overbought. These are absolute gates.
    "rsi_veto_buy_above": 70,
    "rsi_veto_sell_below": 30,

    # Stale-bar veto
    #
    # Max hours between the completed 4H bar's CLOSE and "now"
    # before the signal is discarded. Without this, the first cron
    # tick after the weekend (gold reopens Sun 22:00 UTC) scored
    # Friday's last candle as if it had just closed, and the
    # message quoted entry/SL/TP off a market ~50h old.
    # 6h = one bar length + slack for a delayed collector run.
    "max_bar_age_hours": 6,

    # Duplicate protection
    #
    # Same direction signals are suppressed for 8 hours.
    # An opposite signal may still be sent.
    "cooldown_hours": 8,

    # Outcome tracking / messaging
    "signal_expiry_hours": 8,   # staleness hint shown in the message
    "min_bars_for_stats": 10,   # min resolved signals before showing track record

    # ── GC=F futures data-quality guards ──────────────────────────
    #
    # COMEX gold futures roll 6 times per year (Feb/Apr/Jun/Aug/Oct/Dec).
    # For several days around the roll the front-month chart Yahoo serves
    # develops artificial gaps and volume/ATR spikes that look like real
    # moves but are purely a contract-switch artifact. These guards
    # suppress signals in that window and when the latest 4H bar has
    # abnormally low volume (stale contract about to roll / thin holiday).
    #
    # Rollover day is defined as the 3rd-last business day of the prior
    # month (GC standard). Suppression starts N days before and ends N
    # days after the roll date.
    "rollover_suppress_days_before": 1,
    "rollover_suppress_days_after": 1,

    # Volume sanity check: if the most recent 4H bar's volume is less
    # than this fraction of the median 4H volume, treat the bar as
    # untradeable (thinning front-month / dead session) and skip.
    "min_volume_ratio": 0.20,
    # How many recent 4H bars to use for the median volume baseline.
    "volume_baseline_bars": 30,

    # ── Trend alignment gate (optional stricter filter) ─────────
    #
    # When True, a signal is suppressed unless BOTH the daily trend
    # AND the 4H EMA20/50 alignment agree with the signal direction.
    # Without this gate the additive score can reach min_score (75)
    # from daily trend alone (25) + price > EMA100 (15) + MACD cross
    # (20) + Bollinger (10) + one more small check — even when the
    # 4H EMA stack contradicts the daily direction.
    #
    # Turn on after reviewing outcome data showing mixed-trend signals
    # underperform; leave off until then to avoid over-filtering.
    "require_trend_alignment": False,

    # ── Quick-flip policy ──────────────────────────────────────
    #
    # When True, an opposite-direction signal fired within the
    # cooldown window of the previous signal is flagged in the
    # message and logged. The signal is still sent (a real reversal
    # can happen), but recipients are warned that the prior signal
    # was recent. Helps track how often the engine flips direction.
    "flag_quick_flips": True,

    # ── Score bands for outcome tracking ───────────────────────
    #
    # Resolved signals are grouped by these bands when printing
    # track-record statistics. Helps identify whether high-score
    # signals actually outperform low-score ones.
    "score_bands": [
        (75, 79, "75-79"),
        (80, 84, "80-84"),
        (85, 89, "85-89"),
        (90, 100, "90-100"),
    ],
}


# ============================================================
# DATABASE
# ============================================================

# Next to this file so it works on the server (/opt/market/...) and
# in any other checkout. Override with INDICES_DB_PATH if needed.
DB_PATH = os.environ.get(
    "INDICES_DB_PATH",
    str(_Path(__file__).resolve().parent / "prices.db"),
)


# ============================================================
# TELEGRAM
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

MESSAGE_PREFIX = "📊 [INDICES]"


# ============================================================
# MARKET HOURS
#
# Hour windows kept for documentation. The live filter is
# util.is_market_open — weekday-aware (US indices closed Sat/Sun;
# gold closed Sat + Sunday before 22:00 UTC).
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
