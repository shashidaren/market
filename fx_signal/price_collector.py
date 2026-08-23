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
from util import UTC_FMT, migrate_timestamps, parse_db_ts, utc_now_str

# Shared Yahoo client lives at the repo root (circuit breaker + session).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
import yahoo_client  # noqa: E402

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

COLLECTOR_SLEEP_SECS = float(os.environ.get("FX_COLLECTOR_SLEEP", "0.5"))
# Optional speed-up: fetch all *stale* symbols per timeframe in ONE
# parallel yf.download() call. Off by default — multi-symbol FX downloads
# have a known Yahoo quirk (identical series for every symbol), so the
# result is validated and falls back to per-symbol fetches automatically.
BATCH_FETCH_ENABLED  = os.environ.get("FX_BATCH_FETCH", "0") == "1"
# Skip Yahoo when we already have the current completed bar. The live
# cron is every minute; without this we hammer Yahoo 20×/min for a
# closed-bar strategy that only needs a new fetch after each hour close.
# Set FX_FORCE_FETCH=1 to disable (debug / after a Yahoo outage).
FORCE_FETCH          = os.environ.get("FX_FORCE_FETCH", "0") == "1"
# Consecutive empty responses in one run that trip the circuit. Empty
# is how Yahoo often signals a ban without raising YFRateLimitError.
# A single empty (one bad symbol) is NOT enough.
EMPTY_STREAK_TRIPS   = int(os.environ.get("FX_EMPTY_STREAK_TRIPS", "3"))


def _flatten_columns(df: pd.DataFrame) -> pd.DataFrame:
    """yfinance sometimes returns MultiIndex columns for FX tickers."""
    if df is not None and isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = df.columns.get_level_values(0)
    return df


def fetch_ohlcv(
    yf_symbol: str, interval: str, period: str
) -> pd.DataFrame | None:
    """
    Fetch OHLCV via the shared yahoo_client (session reuse, retry,
    circuit breaker). Returns None on failure / circuit-open.
    """
    df = yahoo_client.history(yf_symbol, interval=interval, period=period)
    if df is None or df.empty:
        return None
    return _flatten_columns(df)


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
    df = yahoo_client.download(
        list(yf_symbols), interval=interval, period=period,
        group_by="ticker", threads=True, progress=False,
    )

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


def have_current_closed_bar(
    conn: sqlite3.Connection, pair: str, timeframe: str, interval: str
) -> bool:
    """
    True if prices.db already holds the current completed bar for this
    pair/timeframe — so another Yahoo fetch would be wasted.
    """
    row = conn.execute(
        """
        SELECT bar_time FROM price_signals
        WHERE pair = ? AND timeframe = ?
        ORDER BY bar_time DESC LIMIT 1
        """,
        (pair, timeframe),
    ).fetchone()
    if not row:
        return False
    last = parse_db_ts(row[0])
    if last is None:
        return False
    return yahoo_client.is_closed_bar_current(last, interval)


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

    fetched_n = 0
    skipped_n = 0
    empty_streak = 0
    stop_fetching = False

    info = yahoo_client.circuit_info()
    if info["open"]:
        log.warning(
            "Yahoo circuit OPEN for %.0fs (%s) — using stored bars only",
            info["remaining"], info["reason"] or "no reason recorded",
        )
        stop_fetching = True
    else:
        log.info(
            "Yahoo circuit closed — skip-if-fresh=%s force_fetch=%s",
            "on" if not FORCE_FETCH else "off", FORCE_FETCH,
        )

    # Which (pair, tf) still need a Yahoo fetch this run?
    need_fetch: list[tuple[str, str]] = []
    if not stop_fetching:
        for pair in FX_PAIRS:
            for tf_name, tf_cfg in TIMEFRAMES.items():
                if not FORCE_FETCH and have_current_closed_bar(
                    conn, pair, tf_name, tf_cfg["interval"]
                ):
                    skipped_n += 1
                    log.info(
                        "%s [%s]: already have current closed bar — skip Yahoo",
                        pair, tf_name,
                    )
                    continue
                need_fetch.append((pair, tf_name))

    # Optional batch prefetch (FX_BATCH_FETCH=1): only for symbols that
    # actually need data. Any unusable timeframe falls back to the
    # per-symbol loop below.
    fetched: dict = {}
    if BATCH_FETCH_ENABLED and need_fetch and not stop_fetching:
        by_tf: dict[str, list[str]] = {}
        for pair, tf_name in need_fetch:
            by_tf.setdefault(tf_name, []).append(FX_PAIRS[pair]["yf_symbol"])
        for tf_name, symbols in by_tf.items():
            if len(symbols) < 3:
                continue  # not worth a batch for 1-2 symbols
            tf_cfg = TIMEFRAMES[tf_name]
            log.info(
                "batch fetch [%s] for %d stale symbol(s)...",
                tf_cfg["interval"], len(symbols),
            )
            t0 = time.perf_counter()
            dfs = fetch_ohlcv_batch(
                symbols, tf_cfg["interval"], tf_cfg["period"],
            )
            if yahoo_client.is_circuit_open():
                log.warning("Yahoo circuit opened during batch — stopping fetches")
                stop_fetching = True
                break
            if not dfs:
                log.warning(
                    "batch fetch [%s] unusable — per-symbol fallback",
                    tf_cfg["interval"],
                )
                continue
            for sym, df in dfs.items():
                pair = symbol_to_pair.get(sym)
                if pair:
                    fetched[(pair, tf_name)] = df
            log.info(
                "batch fetch [%s] ok in %.2fs (%d symbols)",
                tf_cfg["interval"], time.perf_counter() - t0, len(dfs),
            )
            time.sleep(COLLECTOR_SLEEP_SECS)

    for pair, cfg in FX_PAIRS.items():
        if stop_fetching and not fetched:
            break
        for tf_name, tf_cfg in TIMEFRAMES.items():
            try:
                df = fetched.get((pair, tf_name))
                if df is None:
                    if (pair, tf_name) not in need_fetch:
                        continue
                    if stop_fetching or yahoo_client.is_circuit_open():
                        log.warning(
                            "Yahoo circuit open — stopping remaining fetches"
                        )
                        stop_fetching = True
                        break
                    # Regular (or batch-fallback) per-symbol fetch
                    df = fetch_ohlcv(
                        cfg["yf_symbol"],
                        tf_cfg["interval"],
                        tf_cfg["period"],
                    )
                    if yahoo_client.is_circuit_open():
                        log.warning(
                            "Yahoo circuit opened mid-run — stopping remaining fetches"
                        )
                        stop_fetching = True
                        break
                    if df is None:
                        empty_streak += 1
                        if empty_streak >= EMPTY_STREAK_TRIPS:
                            yahoo_client.trip_circuit(
                                f"{empty_streak} consecutive empty Yahoo "
                                f"responses (last={cfg['yf_symbol']} {tf_name})"
                            )
                            stop_fetching = True
                            break
                        continue
                    empty_streak = 0
                    fetched_n += 1
                    time.sleep(COLLECTOR_SLEEP_SECS)
                else:
                    fetched_n += 1

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
