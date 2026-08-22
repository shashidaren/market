#!/usr/bin/env python3
"""
FX price + indicator collector.

Pulls OHLCV per pair/timeframe from Yahoo Finance, computes
EMA/RSI/MACD/ATR, and stores the last N COMPLETED (closed) bars into
SQLite for signal_engine.py to read.

KEY POINTS:
    - df.iloc[-1] is always the live, forming candle — its OHLCV is
      incomplete. We store df.iloc[-N_COMPLETED_BARS-1 : -1] only.
    - All bar timestamps are normalised to UTC before storage so that
      SQLite datetime() comparisons work correctly regardless of the
      timezone yfinance returns (BST, EST, etc.).
    - Staleness is checked against df.iloc[-2] (last closed bar),
      NOT df.iloc[-1] (live candle which is always 'fresh').

Run via run_pipeline.sh (recommended) or directly:
    */15 * * * * cd /opt/market/fx_signal && \
        /usr/bin/python3 price_collector.py \
        >> /var/log/webscrap-fx-collector.log 2>&1

Requires: yfinance, pandas
    pip install yfinance pandas --break-system-packages
"""

import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

from fx_config import (
    ATR_PERIOD,
    EMA_FAST,
    EMA_SLOW,
    FX_PAIRS,
    MACD_SIGNAL,
    N_COMPLETED_BARS,
    RSI_PERIOD,
    TIMEFRAMES,
)
from util import UTC_FMT, migrate_timestamps, utc_now_str

DB_PATH = Path(__file__).parent / "prices.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("price_collector")


# ------------------------------------------------------------------
# DB init
# ------------------------------------------------------------------

def init_db(conn: sqlite3.Connection) -> None:
    """
    Create tables and indexes.
    WAL mode prevents 'database is locked' when signal_engine reads
    while we write.
    """
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS price_signals (
            pair          TEXT    NOT NULL,
            timeframe     TEXT    NOT NULL,
            bar_time      TEXT    NOT NULL,
            close         REAL    NOT NULL,
            ema_fast      REAL,
            ema_slow      REAL,
            rsi           REAL,
            macd          REAL,
            macd_signal   REAL,
            atr           REAL,
            bars_fetched  INTEGER,
            collected_at  TEXT    NOT NULL,
            PRIMARY KEY (pair, timeframe, bar_time)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pair_tf "
        "ON price_signals(pair, timeframe)"
    )
    # Convert any legacy 'T'/'+00:00' ISO timestamps to the SQLite-
    # comparable space format so datetime('now') comparisons work.
    migrate_timestamps(conn)
    conn.commit()


# ------------------------------------------------------------------
# Indicator calculations
# ------------------------------------------------------------------

def compute_ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def compute_rsi(series: pd.Series, period: int) -> pd.Series:
    delta    = series.diff()
    gain     = delta.clip(lower=0)
    loss     = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs       = avg_gain / avg_loss.replace(0, 1e-10)
    return 100 - (100 / (1 + rs))


def compute_macd(
    series: pd.Series, fast: int, slow: int, signal: int
) -> tuple[pd.Series, pd.Series]:
    ema_fast    = compute_ema(series, fast)
    ema_slow    = compute_ema(series, slow)
    macd_line   = ema_fast - ema_slow
    signal_line = compute_ema(macd_line, signal)
    return macd_line, signal_line


def compute_atr(df: pd.DataFrame, period: int) -> pd.Series:
    high       = df["High"]
    low        = df["Low"]
    prev_close = df["Close"].shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low  - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df               = df.copy()
    df["ema_fast"]   = compute_ema(df["Close"], EMA_FAST)
    df["ema_slow"]   = compute_ema(df["Close"], EMA_SLOW)
    df["rsi"]        = compute_rsi(df["Close"], RSI_PERIOD)
    df["macd"], df["macd_signal"] = compute_macd(
        df["Close"], EMA_FAST, EMA_SLOW, MACD_SIGNAL
    )
    df["atr"]        = compute_atr(df, ATR_PERIOD)
    return df


# ------------------------------------------------------------------
# Timezone helper
# ------------------------------------------------------------------

def to_utc_str(bar_time) -> str:
    """
    Normalise a bar timestamp to a UTC string in SQLite-comparable
    format: 'YYYY-MM-DD HH:MM:SS' (same format as SQLite datetime('now')).

    yfinance may return timestamps in local market timezone (BST, EST,
    JST etc.) depending on the host system and yfinance version. Storing
    everything in UTC ensures SQLite datetime() string comparisons are
    always correct — a BST timestamp stored as-is would be 1 hour off in
    every expiry/dedup/freshness check.

    NOTE: the space separator is deliberate. 'YYYY-MM-DDTHH:MM:SS+00:00'
    (isoformat) breaks SQLite comparisons because 'T' > ' ' — see util.py.
    """
    if hasattr(bar_time, "tzinfo") and bar_time.tzinfo is not None:
        # Already timezone-aware — convert to UTC
        bar_time_utc = bar_time.astimezone(timezone.utc)
    elif hasattr(bar_time, "replace"):
        # Naive timestamp — assume UTC (yfinance occasionally returns these)
        bar_time_utc = bar_time.replace(tzinfo=timezone.utc)
    else:
        # Fallback — should not happen
        log.warning("Unexpected bar_time type %s — using current UTC", type(bar_time))
        bar_time_utc = datetime.now(timezone.utc)

    return bar_time_utc.strftime(UTC_FMT)


# ------------------------------------------------------------------
# Data fetch
# ------------------------------------------------------------------

# One shared HTTP session for ALL yfinance calls in this run. yfinance
# negotiates a Yahoo cookie + crumb per Ticker unless you pass a session —
# sharing it means that negotiation (2 extra requests per symbol) happens
# once per run instead of once per symbol. That is the single biggest
# request-count reduction available, and it also keeps TLS connections
# warm. If the private backend API changes, we degrade to no session.
try:
    from yfinance.data import _backend as _yf_backend
    try:
        SESSION = _yf_backend.Session(impersonate="chrome")
    except TypeError:
        SESSION = _yf_backend.Session()
except Exception:
    SESSION = None

try:
    from yfinance.exceptions import YFRateLimitError
except ImportError:
    YFRateLimitError = None

YF_MAX_ATTEMPTS      = 3          # attempts per symbol fetch
RETRY_BACKOFF_BASE   = 2.0        # seconds: attempt n waits 2**n
RATE_LIMIT_WAIT_SECS = 60.0       # hard backoff when Yahoo says too-many-requests
COLLECTOR_SLEEP_SECS = float(os.environ.get("FX_COLLECTOR_SLEEP", "0.5"))
# Optional speed-up: fetch all symbols per timeframe in ONE parallel
# yf.download() call. Off by default — multi-symbol FX downloads have a
# known Yahoo quirk (identical series for every symbol), so the result is
# validated and falls back to per-symbol fetches automatically.
BATCH_FETCH_ENABLED  = os.environ.get("FX_BATCH_FETCH", "0") == "1"


def fetch_ohlcv(
    yf_symbol: str, interval: str, period: str
) -> pd.DataFrame | None:
    """
    Fetch OHLCV with retry + exponential backoff.

    Yahoo drops connections intermittently and rate-limits aggressively
    (YFRateLimitError). Instead of failing the whole run, retry a few
    times with backoff, and wait a full minute on an explicit
    rate-limit signal. The run_pipeline flock guard means an extra 60s
    here simply skips the next cron cycle — far better than hammering.
    """
    last_exc = None
    for attempt in range(1, YF_MAX_ATTEMPTS + 1):
        try:
            if SESSION is not None:
                df = yf.Ticker(yf_symbol, session=SESSION).history(
                    interval=interval, period=period
                )
            else:
                df = yf.Ticker(yf_symbol).history(interval=interval, period=period)
        except Exception as exc:
            last_exc = exc
            if YFRateLimitError is not None and isinstance(exc, YFRateLimitError):
                log.warning(
                    "%s: Yahoo rate-limited — backing off %.0fs",
                    yf_symbol, RATE_LIMIT_WAIT_SECS,
                )
                time.sleep(RATE_LIMIT_WAIT_SECS)
            elif attempt < YF_MAX_ATTEMPTS:
                log.warning(
                    "%s: request failed (attempt %d/%d): %s",
                    yf_symbol, attempt, YF_MAX_ATTEMPTS, exc,
                )
                time.sleep(RETRY_BACKOFF_BASE ** attempt)
            continue

        if df is None or df.empty:
            if attempt < YF_MAX_ATTEMPTS:
                log.warning(
                    "%s: empty DataFrame (attempt %d/%d), retrying",
                    yf_symbol, attempt, YF_MAX_ATTEMPTS,
                )
                time.sleep(RETRY_BACKOFF_BASE ** attempt)
                continue
            log.warning("%s: empty DataFrame returned by yfinance", yf_symbol)
            return None

        # yfinance sometimes returns MultiIndex columns for FX tickers
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df

    log.error(
        "%s: all %d attempts failed — last error: %s",
        yf_symbol, YF_MAX_ATTEMPTS, last_exc,
    )
    return None


def fetch_ohlcv_batch(
    yf_symbols: list, interval: str, period: str
) -> dict | None:
    """
    Fetch one timeframe for many symbols via a single parallel
    yf.download() call.

    Returns {symbol: df} or None if the response is unusable — the
    caller then falls back to per-symbol fetch_ohlcv().

    Guard: Yahoo's multi-symbol chart endpoint occasionally returns the
    IDENTICAL series for every FX symbol. We compare Close series across
    symbols and treat identical output as a failed batch (never trust it).
    """
    try:
        df = yf.download(
            list(yf_symbols), interval=interval, period=period,
            group_by="ticker", threads=True, progress=False,
            session=SESSION,
        )
    except Exception:
        log.exception("batch yfinance request failed (%s)", interval)
        return None

    if df is None or df.empty:
        log.warning("batch (%s): empty response", interval)
        return None

    out, closes = {}, {}
    if isinstance(df.columns, pd.MultiIndex):
        for sym in df.columns.get_level_values(0).unique():
            try:
                sub = df[sym]
            except KeyError:
                continue
            sub = sub.dropna(subset=["Close"])
            if len(sub) < 2:
                continue
            closes[sym] = sub["Close"].reset_index(drop=True)
            out[sym] = sub
    else:
        # Single-symbol response (shouldn't happen with >1 symbols)
        out["_"] = df
        closes["_"] = df["Close"].reset_index(drop=True)

    if len(out) < 2:
        log.warning("batch (%s): too few symbols returned (%d)", interval, len(out))
        return None

    # FX duplicate-series guard — identical Close across symbols is a
    # Yahoo quirk, not real data.
    ref = None
    for sym, s in closes.items():
        if ref is None:
            ref = s
            continue
        if len(s) == len(ref) and (s == ref).all():
            log.warning(
                "batch (%s): identical Close series across symbols (%s) — "
                "Yahoo FX quirk, falling back to per-symbol fetch",
                interval, sym,
            )
            return None
    return out


# ------------------------------------------------------------------
# Staleness check — uses completed bar, NOT live candle
# ------------------------------------------------------------------

def is_stale(df: pd.DataFrame, interval: str) -> bool:
    """
    Reject data if the last COMPLETED bar (iloc[-2]) is older than
    expected.

    We deliberately check iloc[-2], NOT iloc[-1]:
        iloc[-1] is the live forming candle — always 'just now' —
        checking it would mask genuine staleness.

    Threshold = 3x interval:
        A completed bar is by definition >= 1 interval old, so 3x
        gives 2 intervals of tolerance for Yahoo lag + missed crons.
    """
    if len(df) < 2:
        log.warning("DataFrame has fewer than 2 rows — cannot check staleness")
        return True

    completed_bar_time = df.index[-2]

    if not isinstance(completed_bar_time, pd.Timestamp):
        log.warning("Bar index is not a Timestamp: %s", type(completed_bar_time))
        return True

    # Normalise to UTC for comparison
    if completed_bar_time.tzinfo is None:
        completed_bar_time = completed_bar_time.tz_localize("UTC")
    else:
        completed_bar_time = completed_bar_time.tz_convert("UTC")

    now_utc = pd.Timestamp.now(tz="UTC")

    try:
        max_age = pd.Timedelta(interval) * 3
    except ValueError:
        log.warning(
            "Cannot parse interval %r as Timedelta, skipping staleness check",
            interval,
        )
        return False

    age = now_utc - completed_bar_time
    if age > max_age:
        log.warning(
            "Stale data: last closed bar is %s old (threshold %s), bar_time=%s",
            age, max_age, completed_bar_time,
        )
        return True

    return False


# ------------------------------------------------------------------
# Storage — N completed bars per run, UTC timestamps
# ------------------------------------------------------------------

def store_completed_bars(
    conn: sqlite3.Connection,
    pair: str,
    timeframe: str,
    df: pd.DataFrame,
    n_bars: int = N_COMPLETED_BARS,
) -> int:
    """
    Store the last `n_bars` completed (closed) candles.

    Excludes df.iloc[-1] which is the live forming candle.

    All timestamps are converted to UTC via to_utc_str() before
    storage so SQLite datetime() comparisons are always correct,
    regardless of what timezone yfinance returns.

    Storing multiple rows (default 5) ensures signal_engine always
    has a 'previous' bar for crossover detection, even on first run.

    Returns the number of bars actually written.
    """
    if len(df) < 2:
        log.warning(
            "%s [%s]: not enough bars to extract completed candles",
            pair, timeframe,
        )
        return 0

    # Slice: last n_bars closed bars — never touch the live candle
    start_idx    = -(n_bars + 1)
    completed_df = df.iloc[start_idx:-1]

    now      = utc_now_str()
    written  = 0
    critical = ["ema_fast", "ema_slow", "rsi", "macd", "macd_signal", "atr"]

    for bar_time, row in completed_df.iterrows():
        # Skip bars where indicators haven't warmed up yet
        if any(pd.isna(row[col]) for col in critical):
            log.debug(
                "%s [%s]: NaN indicators at %s — warm-up bar, skipping",
                pair, timeframe, bar_time,
            )
            continue

        # Always store as UTC
        bar_time_str = to_utc_str(bar_time)

        conn.execute(
            """
            INSERT OR REPLACE INTO price_signals
                (pair, timeframe, bar_time, close,
                 ema_fast, ema_slow, rsi, macd, macd_signal,
                 atr, bars_fetched, collected_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pair, timeframe, bar_time_str,
                float(row["Close"]),
                float(row["ema_fast"]), float(row["ema_slow"]),
                float(row["rsi"]),
                float(row["macd"]),     float(row["macd_signal"]),
                float(row["atr"]),
                len(df), now,
            ),
        )
        written += 1

    conn.commit()
    return written


# ------------------------------------------------------------------
# Housekeeping
# ------------------------------------------------------------------

def prune_old_data(conn: sqlite3.Connection, days: int = 90) -> None:
    """
    Remove rows older than `days` based on bar_time (not collected_at).

    Using bar_time prevents re-inserted historical bars from surviving
    past the prune window after a DB wipe and re-run.
    """
    conn.execute(
        "DELETE FROM price_signals WHERE bar_time < datetime('now', ?)",
        (f"-{days} days",),
    )
    conn.commit()


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    prune_old_data(conn)

    # Minimum bars needed for stable indicator warm-up.
    # Longest lookback period + buffer for EWM convergence.
    min_bars_needed = max(EMA_SLOW, RSI_PERIOD, ATR_PERIOD, MACD_SIGNAL) + 20

    symbol_to_pair = {cfg["yf_symbol"]: pair for pair, cfg in FX_PAIRS.items()}

    # Optional batch prefetch (FX_BATCH_FETCH=1): fetch every symbol per
    # timeframe in one parallel call. Any unusable timeframe falls back
    # to the normal per-symbol loop below.
    fetched: dict = {}
    if BATCH_FETCH_ENABLED:
        for tf_name, tf_cfg in TIMEFRAMES.items():
            log.info(
                "batch fetch [%s] for %d symbols...",
                tf_cfg["interval"], len(symbol_to_pair),
            )
            t0 = time.perf_counter()
            dfs = fetch_ohlcv_batch(
                list(symbol_to_pair.keys()),
                tf_cfg["interval"],
                tf_cfg["period"],
            )
            if not dfs:
                log.warning(
                    "batch fetch [%s] unusable — per-symbol fallback",
                    tf_cfg["interval"],
                )
                continue
            for sym, df in dfs.items():
                fetched[(symbol_to_pair[sym], tf_name)] = df
            log.info(
                "batch fetch [%s] ok in %.2fs (%d symbols)",
                tf_cfg["interval"], time.perf_counter() - t0, len(dfs),
            )
            time.sleep(COLLECTOR_SLEEP_SECS)

    for pair, cfg in FX_PAIRS.items():
        for tf_name, tf_cfg in TIMEFRAMES.items():
            try:
                df = fetched.get((pair, tf_name))
                if df is None:
                    # Regular (or batch-fallback) per-symbol fetch
                    df = fetch_ohlcv(
                        cfg["yf_symbol"],
                        tf_cfg["interval"],
                        tf_cfg["period"],
                    )
                    if df is None:
                        continue
                    time.sleep(COLLECTOR_SLEEP_SECS)

                if is_stale(df, tf_cfg["interval"]):
                    log.warning("%s [%s]: stale data, skipping", pair, tf_name)
                    continue

                # Count non-NaN closes — gaps/holidays can make len() misleading
                valid_closes = df["Close"].dropna()
                if len(valid_closes) < min_bars_needed:
                    log.warning(
                        "%s [%s]: only %d valid bars (need %d), skipping",
                        pair, tf_name, len(valid_closes), min_bars_needed,
                    )
                    continue

                df = compute_indicators(df)

                # Sanity check on the completed bar we're about to store
                completed_bar = df.iloc[-2]
                critical_cols = ["ema_fast", "ema_slow", "rsi", "macd", "macd_signal", "atr"]
                if any(pd.isna(completed_bar[col]) for col in critical_cols):
                    log.warning(
                        "%s [%s]: NaN in completed bar indicators, skipping store",
                        pair, tf_name,
                    )
                    continue

                # Log what's being stored — the completed bar, not the live candle
                log.info(
                    "%s [%s]: storing bar=%s close=%.5f ema_f=%.5f ema_s=%.5f "
                    "rsi=%.1f macd=%.5f atr=%.5f total_bars=%d",
                    pair, tf_name,
                    to_utc_str(df.index[-2]),
                    completed_bar["Close"],
                    completed_bar["ema_fast"],
                    completed_bar["ema_slow"],
                    completed_bar["rsi"],
                    completed_bar["macd"],
                    completed_bar["atr"],
                    len(df),
                )

                written = store_completed_bars(conn, pair, tf_name, df)
                log.info(
                    "%s [%s]: wrote %d completed bar(s)",
                    pair, tf_name, written,
                )

            except Exception:
                log.exception("Failed to process %s [%s]", pair, tf_name)
                continue

    conn.close()
    log.info("price_collector complete.")


if __name__ == "__main__":
    main()
