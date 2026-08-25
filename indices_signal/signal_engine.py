# /opt/market/indices_signal/signal_engine.py

import sqlite3
import logging

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from util import is_market_open, migrate_timestamps, utc_now_str
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
# HARD VETOES
#
# Standalone so they are unit-testable without a DB or yfinance.
# ============================================================

# COMEX gold futures roll months (Feb/Apr/Jun/Aug/Oct/Dec).
# Rollover "day" is the 3rd-to-last business day of the month
# *before* the contract month (e.g. Dec contract rolls around
# the 3rd-last business day of November).
_GC_ROLL_MONTHS = (2, 4, 6, 8, 10, 12)


def _roll_dates_for_year(year: int) -> list[datetime]:
    """Return UTC-midnight datetimes for the six GC roll days in `year`.

    Roll day = 3rd-last BUSINESS day of the PRIOR month (i.e. Jan for Feb
    contract, Mar for Apr, etc.). Weekends are skipped; we do not account
    for US holidays — good enough for a ±1 day suppression window.
    """
    import calendar
    dates = []
    for contract_month in _GC_ROLL_MONTHS:
        prior_month = contract_month - 1
        y = year
        if prior_month == 0:
            prior_month = 12
            y = year - 1
        last_day = calendar.monthrange(y, prior_month)[1]
        # Walk backwards from the last day counting business days
        bd_count = 0
        d = datetime(y, prior_month, last_day, tzinfo=timezone.utc)
        target = None
        while bd_count < 3:
            if d.weekday() < 5:  # Mon–Fri
                bd_count += 1
                if bd_count == 3:
                    target = d
                    break
            d -= timedelta(days=1)
        if target is not None:
            dates.append(target)
    return dates


def near_gc_rollover(now: datetime | None = None,
                    days_before: int = 1,
                    days_after: int = 1) -> bool:
    """True if `now` is within [roll-day - days_before, roll-day + days_after].

    Only meaningful for GOLD (GC=F). We don't try to be precise about
    intraday timing — the entire window is suppressed so a false signal
    from a gap can't fire.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    # Check the year window around `now` to cover Dec→Jan roll.
    for y in (now.year - 1, now.year, now.year + 1):
        for roll in _roll_dates_for_year(y):
            if roll - timedelta(days=days_before) <= now \
               <= roll + timedelta(days=days_after):
                return True
    return False


def _latest_volume_ratio(df_4h, n_baseline: int, bar_idx: int = -1) -> float | None:
    """(latest 4H bar volume) / (median volume over last n_baseline bars)."""
    if "volume" not in df_4h.columns:
        return None
    vols = df_4h["volume"].dropna()
    if len(vols) < n_baseline:
        return None
    latest = float(vols.iloc[bar_idx])
    baseline = float(np.median(vols.iloc[-n_baseline - 1 : -1].values))
    if baseline <= 0:
        return None
    return latest / baseline


def detect_swing_points(df, lookback: int = 5):
    """Find swing highs and lows in the dataframe.

    A swing high is a bar where the high is greater than the highs
    of `lookback` bars on each side. A swing low is where the low
    is less than the lows of `lookback` bars on each side.

    Returns (swing_highs, swing_lows) as lists of (index, price) tuples.
    """
    swing_highs = []
    swing_lows = []

    if len(df) < 2 * lookback + 1:
        return swing_highs, swing_lows

    for i in range(lookback, len(df) - lookback):
        # Check swing high
        is_high = True
        for j in range(i - lookback, i + lookback + 1):
            if j == i:
                continue
            if df.iloc[j]["high"] >= df.iloc[i]["high"]:
                is_high = False
                break
        if is_high:
            swing_highs.append((i, df.iloc[i]["high"]))

        # Check swing low
        is_low = True
        for j in range(i - lookback, i + lookback + 1):
            if j == i:
                continue
            if df.iloc[j]["low"] <= df.iloc[i]["low"]:
                is_low = False
                break
        if is_low:
            swing_lows.append((i, df.iloc[i]["low"]))

    return swing_highs, swing_lows


def check_bos(df, signal_type: str, lookback: int = 5, search_window: int = 20) -> bool:
    """Check if Break of Structure occurred for the signal direction.

    BUY: current close must be above the most recent swing high
    SELL: current close must be below the most recent swing low

    Returns True if BOS confirmed, False otherwise.
    """
    if len(df) < lookback * 2 + search_window:
        logger.info("BOS: insufficient data (%d bars)", len(df))
        return False

    # Get recent data window
    recent = df.iloc[-search_window:]
    swing_highs, swing_lows = detect_swing_points(recent, lookback)

    current_close = df.iloc[-1]["close"]

    if signal_type == "BUY":
        if not swing_highs:
            logger.info("BOS BUY: no swing highs found in window")
            return False
        # Get the most recent swing high
        last_swing_high = swing_highs[-1][1]
        if current_close > last_swing_high:
            logger.info(
                "BOS BUY: confirmed — price %.2f broke above swing high %.2f",
                current_close, last_swing_high,
            )
            return True
        else:
            logger.info(
                "BOS BUY: not confirmed — price %.2f below swing high %.2f",
                current_close, last_swing_high,
            )
            return False

    elif signal_type == "SELL":
        if not swing_lows:
            logger.info("BOS SELL: no swing lows found in window")
            return False
        # Get the most recent swing low
        last_swing_low = swing_lows[-1][1]
        if current_close < last_swing_low:
            logger.info(
                "BOS SELL: confirmed — price %.2f broke below swing low %.2f",
                current_close, last_swing_low,
            )
            return True
        else:
            logger.info(
                "BOS SELL: not confirmed — price %.2f above swing low %.2f",
                current_close, last_swing_low,
            )
            return False

    return False




def bar_age_hours(bar_time, now=None):
    """Hours since the completed 4H bar CLOSED (bar start + 4h)."""

    if now is None:
        now = datetime.now(timezone.utc)

    if hasattr(bar_time, "to_pydatetime"):
        bar_time = bar_time.to_pydatetime()

    if bar_time.tzinfo is None:
        bar_time = bar_time.replace(tzinfo=timezone.utc)
    else:
        bar_time = bar_time.astimezone(timezone.utc)

    bar_close = bar_time + timedelta(hours=4)

    return (now - bar_close).total_seconds() / 3600.0


def is_bar_stale(bar_time, now=None):
    """
    True when the completed 4H bar closed more than
    max_bar_age_hours ago — e.g. Friday's last gold candle
    scored on the Sunday-evening reopen. A stale bar makes the
    entire entry/SL/TP geometry fiction: the quoted entry
    reference belongs to a market that no longer exists.
    """

    max_age = SIGNAL_CONFIG.get(
        "max_bar_age_hours", 6
    )

    return bar_age_hours(bar_time, now) > max_age


def rsi_vetoes(signal_type, rsi_val):
    """
    Absolute RSI gate, independent of score.

    The scoring zones (min/max_rsi_buy/sell) only decide whether
    RSI ADDS +15; the other five checks alone can reach min_score,
    which let a BUY through at RSI 76.6. This blocks:
      BUY  when RSI > rsi_veto_buy_above  (default 70)
      SELL when RSI < rsi_veto_sell_below (default 30)
    """

    if signal_type == "BUY":
        return rsi_val > SIGNAL_CONFIG.get(
            "rsi_veto_buy_above", 70
        )

    if signal_type == "SELL":
        return rsi_val < SIGNAL_CONFIG.get(
            "rsi_veto_sell_below", 30
        )

    return False


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

    # Stale-bar veto: the "latest completed candle" must have
    # closed recently. After a weekend/holiday gap the newest bar
    # in the DB can be days old (gold: Friday 20:00 bar scored on
    # the Sunday 22:00+ reopen) — its price, ATR, SL and TP no
    # longer describe the live market.
    if is_bar_stale(last["datetime"]):

        logger.info(
            "%s: last 4H bar is stale (closed %.1fh ago, max %dh) — no signal",
            ticker_key,
            bar_age_hours(last["datetime"]),
            SIGNAL_CONFIG.get("max_bar_age_hours", 6),
        )

        return None

    # ── GC=F futures data-quality guards (GOLD only) ─────────
    if ticker_key == "GOLD":
        # 1. Rollover-window veto. Around the 6 COMEX roll dates
        #    per year Yahoo's GC=F chart develops artificial gaps
        #    and ATR/volume spikes when the front month switches.
        #    Suppress signals in that window so we don't trade on
        #    a chart discontinuity masquerading as a breakout.
        days_before = SIGNAL_CONFIG.get("rollover_suppress_days_before", 1)
        days_after  = SIGNAL_CONFIG.get("rollover_suppress_days_after", 1)
        if near_gc_rollover(datetime.now(timezone.utc),
                            days_before=days_before,
                            days_after=days_after):
            logger.info(
                "GOLD: within GC=F futures roll window (±%d/%d days) — "
                "suppressing signal (chart gaps distort ATR/EMA/RSI)",
                days_before, days_after,
            )
            return None

        # 2. Volume data assertion. The volume-ratio veto below
        #    depends on GOLD 4H volume being populated. If Yahoo
        #    ever stops serving volume for GC=F (or the resampler
        #    drops it), the veto silently becomes a no-op. This
        #    explicit check catches that regression.
        if "volume" not in df_4h.columns or df_4h["volume"].dropna().empty:
            logger.warning(
                "GOLD: 4H volume data missing or empty — volume veto "
                "cannot operate; suppressing signal until data restored"
            )
            return None

        vol_populated = (
            df_4h["volume"].dropna().shape[0]
            >= SIGNAL_CONFIG.get("volume_baseline_bars", 30)
        )
        if not vol_populated:
            logger.warning(
                "GOLD: insufficient volume history (%d bars, need %d) — "
                "volume veto unreliable; suppressing signal",
                df_4h["volume"].dropna().shape[0],
                SIGNAL_CONFIG.get("volume_baseline_bars", 30),
            )
            return None

        # 3. Volume sanity veto. If the latest completed 4H bar
        #    has less than MIN_VOLUME_RATIO of the median recent
        #    volume, we're on a dead/thinning contract (imminent
        #    roll, holiday, or dead session) — prices are not
        #    reliable enough to anchor SL/TP.
        vol_ratio = _latest_volume_ratio(
            df_4h,
            n_baseline=SIGNAL_CONFIG.get("volume_baseline_bars", 30),
        )
        min_ratio = SIGNAL_CONFIG.get("min_volume_ratio", 0.20)
        if vol_ratio is not None and vol_ratio < min_ratio:
            logger.info(
                "GOLD: latest 4H volume %.0f%% of median — too thin "
                "(likely pre-roll or holiday); suppressing signal",
                vol_ratio * 100,
            )
            return None

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

    # ── Trend alignment gate (optional) ─────────────────────
    #
    # When require_trend_alignment is True, suppress only the
    # *misaligned direction* by zeroing its score — do not return
    # early. A high but misaligned BUY must not kill a valid SELL
    # (and vice versa).
    if SIGNAL_CONFIG.get("require_trend_alignment", False):
        buy_aligned = (daily_trend == "BULL" and last["ema20"] > last["ema50"])
        sell_aligned = (daily_trend == "BEAR" and last["ema20"] < last["ema50"])
        if buy_score >= SIGNAL_CONFIG["min_score"] and not buy_aligned:
            logger.info(
                "%s: BUY score %d meets threshold but trend misaligned "
                "(daily=%s, EMA20/50=%s) — BUY side suppressed",
                ticker_key, buy_score, daily_trend,
                "aligned" if last["ema20"] > last["ema50"] else "crossed down",
            )
            buy_score = 0
        if sell_score >= SIGNAL_CONFIG["min_score"] and not sell_aligned:
            logger.info(
                "%s: SELL score %d meets threshold but trend misaligned "
                "(daily=%s, EMA20/50=%s) — SELL side suppressed",
                ticker_key, sell_score, daily_trend,
                "aligned" if last["ema20"] < last["ema50"] else "crossed up",
            )
            sell_score = 0

    # ── Break of Structure (BOS) gate ──────────────────────
    #
    # When require_bos is True, a signal is suppressed unless
    # price has broken above a recent swing high (for BUY) or
    # below a recent swing low (for SELL). This confirms the trend
    # is continuing (BOS = Break of Structure), not just indicators
    # aligning on a rising/falling price that hasn't proven it can
    # break key levels yet.
    if SIGNAL_CONFIG.get("require_bos", False):
        # Determine which direction would win
        if (buy_score >= SIGNAL_CONFIG["min_score"]
            and buy_score > sell_score):
            if not check_bos(
                df, "BUY",
                lookback=SIGNAL_CONFIG.get("bos_swing_lookback", 5),
                search_window=SIGNAL_CONFIG.get("bos_search_window", 20),
            ):
                logger.info(
                    "%s: BUY suppressed — no Break of Structure "
                    "(price hasn't broken above recent swing high)",
                    ticker_key,
                )
                return None
        elif (sell_score >= SIGNAL_CONFIG["min_score"]
              and sell_score > buy_score):
            if not check_bos(
                df, "SELL",
                lookback=SIGNAL_CONFIG.get("bos_swing_lookback", 5),
                search_window=SIGNAL_CONFIG.get("bos_search_window", 20),
            ):
                logger.info(
                    "%s: SELL suppressed — no Break of Structure "
                    "(price hasn't broken below recent swing low)",
                    ticker_key,
                )
                return None

    if (
        buy_score
        >= SIGNAL_CONFIG["min_score"]
        and buy_score > sell_score
    ):

        # RSI hard veto — an overbought BUY passes the additive
        # score (75 without any RSI points) but is the worst
        # possible entry timing. Absolute gate, not a score item.
        if rsi_vetoes("BUY", rsi_val):

            logger.info(
                "%s: BUY vetoed — RSI %.1f > %.0f (overbought)",
                ticker_key,
                rsi_val,
                SIGNAL_CONFIG.get("rsi_veto_buy_above", 70),
            )

            return None

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

        # Add BOS confirmation to reasons if enabled
        if SIGNAL_CONFIG.get("require_bos", False):
            buy_reasons.append("BOS confirmed (broke swing high)")

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

        # RSI hard veto — mirror of the BUY case: never SELL
        # into oversold, regardless of score.
        if rsi_vetoes("SELL", rsi_val):

            logger.info(
                "%s: SELL vetoed — RSI %.1f < %.0f (oversold)",
                ticker_key,
                rsi_val,
                SIGNAL_CONFIG.get("rsi_veto_sell_below", 30),
            )

            return None

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

        # Add BOS confirmation to reasons if enabled
        if SIGNAL_CONFIG.get("require_bos", False):
            sell_reasons.append("BOS confirmed (broke swing low)")

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


# is_market_open is imported from util (weekday-aware).


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
    ).strftime("%Y-%m-%d %H:%M:%S")

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


def check_quick_flip(
    ticker_key,
    signal_type,
):
    """Check whether an OPPOSITE signal was sent inside the cooldown window.

    Returns a dict with flip details if found, or None. The signal is
    still allowed through (a genuine reversal can happen), but the
    message is annotated so recipients know the prior signal was recent.

    Example:
        BUY  at 08:00 (sent)
        SELL at 12:00 (inside 8h cooldown)
        → quick_flip = {"prior_type": "BUY", "hours_ago": 4.0}
    """
    if not SIGNAL_CONFIG.get("flag_quick_flips", True):
        return None

    opposite = "SELL" if signal_type == "BUY" else "BUY"

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(
            hours=SIGNAL_CONFIG["cooldown_hours"]
        )
    ).strftime("%Y-%m-%d %H:%M:%S")

    cur.execute(
        """
        SELECT sent_at, score
        FROM signals_sent
        WHERE ticker = ?
          AND signal_type = ?
          AND sent_at > ?
        ORDER BY sent_at DESC
        LIMIT 1
        """,
        (
            ticker_key,
            opposite,
            cutoff,
        ),
    )

    row = cur.fetchone()
    conn.close()

    if row is None:
        return None

    from util import parse_db_ts
    prior_time = parse_db_ts(row[0])
    hours_ago = (
        (datetime.now(timezone.utc) - prior_time).total_seconds() / 3600.0
        if prior_time else 0.0
    )

    logger.info(
        "%s: QUICK FLIP — %s signal fired %.1fh after opposite %s "
        "(score %s) within cooldown window",
        ticker_key, signal_type, hours_ago, opposite, row[1],
    )

    return {
        "prior_type": opposite,
        "prior_score": row[1],
        "hours_ago": round(hours_ago, 1),
    }


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
        ("quick_flip", "INTEGER"),  # 1 if opposite signal was within cooldown
    ):
        if col not in existing:
            cur.execute(
                f"ALTER TABLE signals_sent ADD COLUMN {col} {coltype}"
            )
    conn.commit()
    migrate_timestamps(conn)
    conn.close()


# ============================================================
# LOG SIGNAL
# ============================================================

def log_signal(signal):

    ensure_schema()

    conn = sqlite3.connect(DB_PATH)

    cur = conn.cursor()

    # quick_flip: 1 if an opposite signal was sent within cooldown, else 0
    qf = 1 if signal.get("quick_flip") else 0

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
            atr,
            quick_flip
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            signal["ticker"],
            signal["type"],
            utc_now_str(),
            signal["price"],
            signal["score"],
            signal["sl"],
            signal["tp"],
            signal["atr"],
            qf,
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

        # ── Quick-flip annotation ─────────────────────────────
        # An opposite signal inside the cooldown window is still
        # sent (genuine reversals happen), but flagged so the
        # message warns recipients and the outcome tracker can
        # measure whether flips underperform.
        quick_flip = check_quick_flip(
            ticker_key,
            sig["type"],
        )
        if quick_flip:
            sig["quick_flip"] = quick_flip

        signals.append(sig)

    return signals


if __name__ == "__main__":

    signals = run()

    for signal in signals:
        print(signal)
