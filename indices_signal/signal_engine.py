# /opt/market/indices_signal/signal_engine.py

import sqlite3
import logging

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from indices_config import (
    TICKERS,
    DB_PATH,
    PRIMARY_TIMEFRAME,
    TREND_TIMEFRAME,
    SIGNAL_CONFIG,
    MARKET_HOURS,
    EMA_FAST,
    EMA_MEDIUM,
    EMA_LONG,
    RSI_PERIOD,
    ATR_PERIOD,
    ADX_PERIOD,
    MACD_FAST,
    MACD_SLOW,
    MACD_SIGNAL,
    BB_PERIOD,
    BB_STD,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)


# ============================================================
# INDICATORS
# ============================================================

def ema(series, period):
    return series.ewm(
        span=period,
        adjust=False,
    ).mean()


def rsi(series, period=14):

    delta = series.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(
        alpha=1 / period,
        min_periods=period,
        adjust=False,
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / period,
        min_periods=period,
        adjust=False,
    ).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)

    result = 100 - (
        100 / (1 + rs)
    )

    return result


def macd(
    series,
    fast=12,
    slow=26,
    signal=9,
):

    ema_fast = ema(series, fast)
    ema_slow = ema(series, slow)

    macd_line = ema_fast - ema_slow

    signal_line = ema(
        macd_line,
        signal,
    )

    histogram = (
        macd_line - signal_line
    )

    return (
        macd_line,
        signal_line,
        histogram,
    )


def atr(df, period=14):

    high_low = (
        df["high"] - df["low"]
    )

    high_close = (
        df["high"]
        - df["close"].shift()
    ).abs()

    low_close = (
        df["low"]
        - df["close"].shift()
    ).abs()

    true_range = pd.concat(
        [
            high_low,
            high_close,
            low_close,
        ],
        axis=1,
    ).max(axis=1)

    return true_range.ewm(
        alpha=1 / period,
        min_periods=period,
        adjust=False,
    ).mean()


def adx(df, period=14):

    high = df["high"]
    low = df["low"]
    close = df["close"]

    up_move = high.diff()

    down_move = (
        -low.diff()
    )

    plus_dm = pd.Series(
        np.where(
            (up_move > down_move)
            & (up_move > 0),
            up_move,
            0.0,
        ),
        index=df.index,
    )

    minus_dm = pd.Series(
        np.where(
            (down_move > up_move)
            & (down_move > 0),
            down_move,
            0.0,
        ),
        index=df.index,
    )

    tr = pd.concat(
        [
            high - low,
            (
                high - close.shift()
            ).abs(),
            (
                low - close.shift()
            ).abs(),
        ],
        axis=1,
    ).max(axis=1)

    atr_value = tr.ewm(
        alpha=1 / period,
        min_periods=period,
        adjust=False,
    ).mean()

    plus_di = (
        100
        * plus_dm.ewm(
            alpha=1 / period,
            min_periods=period,
            adjust=False,
        ).mean()
        / atr_value
    )

    minus_di = (
        100
        * minus_dm.ewm(
            alpha=1 / period,
            min_periods=period,
            adjust=False,
        ).mean()
        / atr_value
    )

    denominator = (
        plus_di + minus_di
    ).replace(0, np.nan)

    dx = (
        100
        * (plus_di - minus_di).abs()
        / denominator
    )

    return dx.ewm(
        alpha=1 / period,
        min_periods=period,
        adjust=False,
    ).mean()


def bollinger(
    series,
    period=20,
    std=2,
):

    middle = series.rolling(
        period
    ).mean()

    deviation = series.rolling(
        period
    ).std()

    upper = (
        middle
        + std * deviation
    )

    lower = (
        middle
        - std * deviation
    )

    return upper, middle, lower


# ============================================================
# LOAD DATA
# ============================================================

def load_prices(ticker, timeframe):

    conn = sqlite3.connect(DB_PATH)

    df = pd.read_sql_query(
        """
        SELECT *
        FROM prices
        WHERE ticker = ?
          AND timeframe = ?
        ORDER BY datetime ASC
        """,
        conn,
        params=(
            ticker,
            timeframe,
        ),
    )

    conn.close()

    if df.empty:
        return df

    df["datetime"] = pd.to_datetime(
        df["datetime"],
        utc=True,
    )

    return df


# ============================================================
# CLOSED CANDLE FILTER
# ============================================================

def remove_open_candles(
    df,
    timeframe,
):

    if df.empty:
        return df

    now = datetime.now(
        timezone.utc
    )

    result = df.copy()

    if timeframe == "4h":

        candle_end = (
            result["datetime"]
            + pd.Timedelta(hours=4)
        )

        result = result[
            candle_end
            <= now
        ]

    elif timeframe == "1d":

        candle_end = (
            result["datetime"]
            + pd.Timedelta(days=1)
        )

        result = result[
            candle_end
            <= now
        ]

    return result


# ============================================================
# SIGNAL ANALYSIS
# ============================================================

def analyze_ticker(ticker_key):

    cfg = TICKERS[ticker_key]

    # --------------------------------------------------------
    # Load data
    # --------------------------------------------------------

    df_4h = load_prices(
        ticker_key,
        PRIMARY_TIMEFRAME,
    )

    df_1d = load_prices(
        ticker_key,
        TREND_TIMEFRAME,
    )

    # --------------------------------------------------------
    # Remove currently forming candles
    # --------------------------------------------------------

    df_4h = remove_open_candles(
        df_4h,
        PRIMARY_TIMEFRAME,
    )

    df_1d = remove_open_candles(
        df_1d,
        TREND_TIMEFRAME,
    )

    # --------------------------------------------------------
    # Check history
    # --------------------------------------------------------

    if len(df_4h) < SIGNAL_CONFIG["min_4h_candles"]:

        logger.warning(
            "%s: insufficient 4H data (%d candles)",
            ticker_key,
            len(df_4h),
        )

        return None

    if len(df_1d) < SIGNAL_CONFIG["min_1d_candles"]:

        logger.warning(
            "%s: insufficient daily data (%d candles)",
            ticker_key,
            len(df_1d),
        )

        return None

    # ========================================================
    # DAILY TREND
    # ========================================================

    df_1d = df_1d.copy()

    df_1d["ema50"] = ema(
        df_1d["close"],
        EMA_MEDIUM,
    )

    last_1d = df_1d.iloc[-1]

    daily_trend = (
        "BULL"
        if last_1d["close"]
        > last_1d["ema50"]
        else "BEAR"
    )

    # ========================================================
    # 4H INDICATORS
    # ========================================================

    df = df_4h.copy()

    df["ema20"] = ema(
        df["close"],
        EMA_FAST,
    )

    df["ema50"] = ema(
        df["close"],
        EMA_MEDIUM,
    )

    df["ema100"] = ema(
        df["close"],
        EMA_LONG,
    )

    df["rsi"] = rsi(
        df["close"],
        RSI_PERIOD,
    )

    (
        df["macd"],
        df["macd_sig"],
        df["macd_hist"],
    ) = macd(
        df["close"],
        MACD_FAST,
        MACD_SLOW,
        MACD_SIGNAL,
    )

    df["atr"] = atr(
        df,
        ATR_PERIOD,
    )

    df["adx"] = adx(
        df,
        ADX_PERIOD,
    )

    (
        df["bb_up"],
        df["bb_mid"],
        df["bb_low"],
    ) = bollinger(
        df["close"],
        BB_PERIOD,
        BB_STD,
    )

    # --------------------------------------------------------
    # Latest completed candle
    # --------------------------------------------------------

    last = df.iloc[-1]
    prev = df.iloc[-2]

    price = last["close"]
    atr_val = last["atr"]
    adx_val = last["adx"]
    rsi_val = last["rsi"]

    # --------------------------------------------------------
    # Validate indicators
    # --------------------------------------------------------

    critical_values = [
        price,
        atr_val,
        adx_val,
        rsi_val,
        last["ema20"],
        last["ema50"],
        last["ema100"],
        last["macd_hist"],
        last["bb_mid"],
    ]

    if any(
        pd.isna(value)
        for value in critical_values
    ):

        logger.warning(
            "%s: indicators not ready",
            ticker_key,
        )

        return None

    # ========================================================
    # MARKET FILTERS
    # ========================================================

    if adx_val < SIGNAL_CONFIG["min_adx"]:

        logger.info(
            "%s: ADX too low (%.1f)",
            ticker_key,
            adx_val,
        )

        return None

    if atr_val < cfg["min_atr"]:

        logger.info(
            "%s: ATR too low (%.2f)",
            ticker_key,
            atr_val,
        )

        return None

    # ========================================================
    # BUY SCORE
    # ========================================================

    buy_score = 0
    buy_reasons = []

    if daily_trend == "BULL":

        buy_score += 25
        buy_reasons.append(
            "Daily uptrend"
        )

    if last["ema20"] > last["ema50"]:

        buy_score += 15
        buy_reasons.append(
            "4H EMA20 > EMA50"
        )

    if price > last["ema100"]:

        buy_score += 15
        buy_reasons.append(
            "Above EMA100"
        )

    if (
        last["macd_hist"] > 0
        and prev["macd_hist"] <= 0
    ):

        buy_score += 20
        buy_reasons.append(
            "MACD bullish crossover"
        )

    elif last["macd_hist"] > 0:

        buy_score += 10
        buy_reasons.append(
            "MACD positive"
        )

    if (
        SIGNAL_CONFIG["min_rsi_buy"]
        <= rsi_val
        <= SIGNAL_CONFIG["max_rsi_buy"]
    ):

        buy_score += 15

        buy_reasons.append(
            f"RSI healthy ({rsi_val:.0f})"
        )

    if price > last["bb_mid"]:

        buy_score += 10
        buy_reasons.append(
            "Above Bollinger middle"
        )

    # ========================================================
    # SELL SCORE
    # ========================================================

    sell_score = 0
    sell_reasons = []

    if daily_trend == "BEAR":

        sell_score += 25
        sell_reasons.append(
            "Daily downtrend"
        )

    if last["ema20"] < last["ema50"]:

        sell_score += 15
        sell_reasons.append(
            "4H EMA20 < EMA50"
        )

    if price < last["ema100"]:

        sell_score += 15
        sell_reasons.append(
            "Below EMA100"
        )

    if (
        last["macd_hist"] < 0
        and prev["macd_hist"] >= 0
    ):

        sell_score += 20
        sell_reasons.append(
            "MACD bearish crossover"
        )

    elif last["macd_hist"] < 0:

        sell_score += 10
        sell_reasons.append(
            "MACD negative"
        )

    if (
        SIGNAL_CONFIG["min_rsi_sell"]
        <= rsi_val
        <= SIGNAL_CONFIG["max_rsi_sell"]
    ):

        sell_score += 15

        sell_reasons.append(
            f"RSI healthy ({rsi_val:.0f})"
        )

    if price < last["bb_mid"]:

        sell_score += 10

        sell_reasons.append(
            "Below Bollinger middle"
        )

    # ========================================================
    # FINAL DECISION
    # ========================================================

    signal = None

    if (
        buy_score
        >= SIGNAL_CONFIG["min_score"]
        and buy_score > sell_score
    ):

        sl = (
            price
            - (
                atr_val
                * cfg["atr_multiplier_sl"]
            )
        )

        tp = (
            price
            + (
                atr_val
                * cfg["atr_multiplier_tp"]
            )
        )

        signal = {
            "ticker": ticker_key,
            "type": "BUY",
            "price": price,
            "sl": sl,
            "tp": tp,
            "score": buy_score,
            "reasons": buy_reasons,
            "rsi": rsi_val,
            "adx": adx_val,
            "atr": atr_val,
            "daily_trend": daily_trend,
            "bar_time": last["datetime"],
        }

    elif (
        sell_score
        >= SIGNAL_CONFIG["min_score"]
        and sell_score > buy_score
    ):

        sl = (
            price
            + (
                atr_val
                * cfg["atr_multiplier_sl"]
            )
        )

        tp = (
            price
            - (
                atr_val
                * cfg["atr_multiplier_tp"]
            )
        )

        signal = {
            "ticker": ticker_key,
            "type": "SELL",
            "price": price,
            "sl": sl,
            "tp": tp,
            "score": sell_score,
            "reasons": sell_reasons,
            "rsi": rsi_val,
            "adx": adx_val,
            "atr": atr_val,
            "daily_trend": daily_trend,
            "bar_time": last["datetime"],
        }

    # --------------------------------------------------------

    if signal:

        logger.info(
            "SIGNAL %s %s @ %.2f | score=%d",
            ticker_key,
            signal["type"],
            price,
            signal["score"],
        )

    else:

        logger.info(
            "%s: No signal | BUY=%d SELL=%d",
            ticker_key,
            buy_score,
            sell_score,
        )

    return signal


# ============================================================
# MARKET HOURS
# ============================================================

def is_market_open(ticker_key):

    now = datetime.now(
        timezone.utc
    )

    hour = now.hour

    hours = MARKET_HOURS[
        ticker_key
    ]

    return (
        hours["start"]
        <= hour
        <= hours["end"]
    )


# ============================================================
# COOLDOWN
# ============================================================

def check_cooldown(
    ticker_key,
    signal_type,
):

    """
    Suppress only the SAME signal direction.

    Example:

    BUY at 08:00
        ↓
    Another BUY at 10:00
        → blocked

    SELL at 10:00
        → allowed
    """

    conn = sqlite3.connect(DB_PATH)

    cur = conn.cursor()

    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(
            hours=SIGNAL_CONFIG[
                "cooldown_hours"
            ]
        )
    ).isoformat()

    cur.execute(
        """
        SELECT COUNT(*)
        FROM signals_sent
        WHERE ticker = ?
          AND signal_type = ?
          AND sent_at > ?
        """,
        (
            ticker_key,
            signal_type,
            cutoff,
        ),
    )

    count = cur.fetchone()[0]

    conn.close()

    return count == 0


# ============================================================
# SCHEMA ENSURE (outcome-tracking columns)
# ============================================================

def ensure_schema(db_path=DB_PATH):
    """Add outcome-tracking columns to signals_sent if missing.

    Safe to call repeatedly; only adds columns that don't exist.
    Keeps log_signal / outcome_tracker working on databases that
    were created before these columns were introduced.
    """
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    existing = {
        r[1] for r in cur.execute("PRAGMA table_info(signals_sent)")
    }
    for col, coltype in (
        ("sl", "REAL"),
        ("tp", "REAL"),
        ("atr", "REAL"),
        ("outcome", "TEXT"),
        ("outcome_price", "REAL"),
        ("resolved_at", "TEXT"),
    ):
        if col not in existing:
            cur.execute(
                f"ALTER TABLE signals_sent ADD COLUMN {col} {coltype}"
            )
    conn.commit()
    conn.close()


# ============================================================
# LOG SIGNAL
# ============================================================

def log_signal(signal):

    ensure_schema()

    conn = sqlite3.connect(DB_PATH)

    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO signals_sent
        (
            ticker,
            signal_type,
            sent_at,
            price,
            score,
            sl,
            tp,
            atr
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            signal["ticker"],
            signal["type"],
            datetime.now(
                timezone.utc
            ).isoformat(),
            signal["price"],
            signal["score"],
            signal["sl"],
            signal["tp"],
            signal["atr"],
        ),
    )

    conn.commit()
    conn.close()


# ============================================================
# MAIN ENGINE
# ============================================================

def run():

    ensure_schema()

    signals = []

    for ticker_key in TICKERS:

        if not is_market_open(ticker_key):

            logger.info(
                "%s: market closed",
                ticker_key,
            )

            continue

        sig = analyze_ticker(
            ticker_key
        )

        if not sig:
            continue

        if not check_cooldown(
            ticker_key,
            sig["type"],
        ):

            logger.info(
                "%s: %s signal in cooldown",
                ticker_key,
                sig["type"],
            )

            continue

        signals.append(sig)

    return signals


if __name__ == "__main__":

    signals = run()

    for signal in signals:
        print(signal)
