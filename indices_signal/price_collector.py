# /opt/market/indices_signal/price_collector.py

import logging
import os
import sqlite3
import sys
from pathlib import Path

import pandas as pd

from indices_config import (
    TICKERS,
    DB_PATH,
    PRIMARY_TIMEFRAME,
    TREND_TIMEFRAME,
    INTRADAY_LOOKBACK_DAYS,
    DAILY_LOOKBACK_DAYS,
    SIGNAL_CONFIG,
)
from util import migrate_timestamps, parse_db_ts, to_utc_str

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
import yahoo_client  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)


# ============================================================
# DATABASE
# ============================================================

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS prices (
            ticker TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            datetime TEXT NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume REAL,
            PRIMARY KEY (ticker, timeframe, datetime)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS signals_sent (
            ticker TEXT NOT NULL,
            signal_type TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            price REAL,
            score REAL,
            sl REAL,
            tp REAL,
            atr REAL,
            outcome TEXT,
            outcome_price REAL,
            resolved_at TEXT,
            PRIMARY KEY (ticker, sent_at)
        )
    """)

    # Migration: add outcome-tracking columns to databases that were
    # created before these columns existed.
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
    migrate_timestamps(conn)
    conn.close()

    logger.info("Database initialized")


# ============================================================
# DATA FETCHING
# ============================================================

FORCE_FETCH = os.environ.get("INDICES_FORCE_FETCH", "0") == "1"
EMPTY_STREAK_TRIPS = int(os.environ.get("INDICES_EMPTY_STREAK_TRIPS", "3"))
# Once the DB is warm, only pull a short window to catch new bars.
INCREMENTAL_PERIOD = os.environ.get("INDICES_INCREMENTAL_PERIOD", "10d")


def have_current_closed_bar(conn, ticker_key, timeframe, interval) -> bool:
    row = conn.execute(
        """
        SELECT datetime FROM prices
        WHERE ticker = ? AND timeframe = ?
        ORDER BY datetime DESC LIMIT 1
        """,
        (ticker_key, timeframe),
    ).fetchone()
    if not row:
        return False
    last = parse_db_ts(row[0])
    if last is None:
        return False
    return yahoo_client.is_closed_bar_current(last, interval)


def history_count(conn, ticker_key, timeframe) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM prices WHERE ticker = ? AND timeframe = ?",
        (ticker_key, timeframe),
    ).fetchone()[0]


def fetch_data(symbol, timeframe, period=None):
    """
    Fetch raw Yahoo Finance data.

    4H data is built from Yahoo 1H candles.
    Daily data is fetched directly.
    """

    if timeframe == "4h":
        interval = "1h"
        period = period or f"{INTRADAY_LOOKBACK_DAYS}d"
    else:
        interval = "1d"
        period = period or f"{DAILY_LOOKBACK_DAYS}d"

    logger.info(
        "Fetching %s | interval=%s | period=%s",
        symbol,
        interval,
        period,
    )

    if yahoo_client.is_circuit_open():
        info = yahoo_client.circuit_info()
        logger.warning(
            "Yahoo circuit OPEN for %.0fs (%s) — skip %s",
            info["remaining"], info["reason"] or "no reason", symbol,
        )
        return pd.DataFrame()

    df = yahoo_client.history(
        symbol,
        period=period,
        interval=interval,
        auto_adjust=True,
    )

    if df is None or df.empty:
        return pd.DataFrame()

    # Flatten yfinance multi-index columns if present
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    return df


# ============================================================
# RESAMPLING
# ============================================================

def resample_to_4h(df):
    """
    Convert 1H candles into 4H candles.

    The resulting candle timestamp represents the start of the
    4-hour bucket.

    We do not attempt to decide here whether the latest candle
    is complete. That decision is made by signal_engine.py.
    """

    if df.empty:
        return df

    result = df.resample("4h").agg(
        {
            "Open": "first",
            "High": "max",
            "Low": "min",
            "Close": "last",
            "Volume": "sum",
        }
    )

    # Remove empty buckets
    result = result.dropna(
        subset=["Open", "High", "Low", "Close"]
    )

    return result


# ============================================================
# STORE DATA
# ============================================================

def store_dataframe(ticker_key, timeframe, df):

    if df.empty:
        logger.warning(
            "%s @ %s: no data to store",
            ticker_key,
            timeframe,
        )
        return 0

    df = df.reset_index()

    date_col = (
        "Datetime"
        if "Datetime" in df.columns
        else "Date"
    )

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    rows = 0

    for _, row in df.iterrows():

        try:

            volume = row.get("Volume", 0)

            if pd.isna(volume):
                volume = 0

            cur.execute(
                """
                INSERT OR REPLACE INTO prices
                (
                    ticker,
                    timeframe,
                    datetime,
                    open,
                    high,
                    low,
                    close,
                    volume
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticker_key,
                    timeframe,
                    to_utc_str(row[date_col]),
                    float(row["Open"]),
                    float(row["High"]),
                    float(row["Low"]),
                    float(row["Close"]),
                    float(volume),
                ),
            )

            rows += 1

        except Exception as exc:

            logger.warning(
                "%s @ %s: skipped row: %s",
                ticker_key,
                timeframe,
                exc,
            )

    conn.commit()
    conn.close()

    logger.info(
        "Stored %d candles for %s @ %s",
        rows,
        ticker_key,
        timeframe,
    )

    return rows


# ============================================================
# FETCH AND STORE
# ============================================================

def fetch_and_store(ticker_key, timeframe):

    cfg = TICKERS[ticker_key]
    symbol = cfg["yahoo_symbol"]

    try:

        df = fetch_data(symbol, timeframe)

        if df.empty:
            logger.warning(
                "%s: Yahoo returned no data",
                ticker_key,
            )
            return 0

        if timeframe == "4h":
            df = resample_to_4h(df)

        return store_dataframe(
            ticker_key,
            timeframe,
            df,
        )

    except Exception as exc:

        logger.exception(
            "Error processing %s @ %s: %s",
            ticker_key,
            timeframe,
            exc,
        )

        return 0


# ============================================================
# MAIN COLLECTION
# ============================================================

def collect_all():

    init_db()

    fetched_n = 0
    skipped_n = 0
    empty_streak = 0
    stop_fetching = yahoo_client.is_circuit_open()

    if stop_fetching:
        info = yahoo_client.circuit_info()
        logger.warning(
            "Yahoo circuit OPEN for %.0fs (%s) — using stored bars only",
            info["remaining"], info["reason"] or "no reason",
        )

    conn = sqlite3.connect(DB_PATH)
    jobs = (
        (PRIMARY_TIMEFRAME, "4h", SIGNAL_CONFIG["min_4h_candles"]),
        (TREND_TIMEFRAME, "1d", SIGNAL_CONFIG["min_1d_candles"]),
    )

    for ticker_key in TICKERS:
        for timeframe, interval, min_rows in jobs:
            if stop_fetching:
                break
            if not FORCE_FETCH and have_current_closed_bar(
                conn, ticker_key, timeframe, interval
            ):
                skipped_n += 1
                logger.info(
                    "%s @ %s: already have current closed bar — skip Yahoo",
                    ticker_key, timeframe,
                )
                continue

            period = None
            if history_count(conn, ticker_key, timeframe) >= min_rows:
                period = INCREMENTAL_PERIOD

            written = fetch_and_store(ticker_key, timeframe, period=period)
            if yahoo_client.is_circuit_open():
                logger.warning(
                    "Yahoo circuit opened — stopping remaining fetches"
                )
                stop_fetching = True
                break
            if written == 0:
                empty_streak += 1
                if empty_streak >= EMPTY_STREAK_TRIPS:
                    yahoo_client.trip_circuit(
                        f"{empty_streak} consecutive empty Yahoo "
                        f"responses (last={ticker_key} {timeframe})"
                    )
                    stop_fetching = True
                    break
            else:
                empty_streak = 0
                fetched_n += 1
        if stop_fetching:
            break

    conn.close()
    logger.info(
        "Collection complete. fetched=%d skipped_fresh=%d circuit_open=%s",
        fetched_n, skipped_n, yahoo_client.is_circuit_open(),
    )


if __name__ == "__main__":
    collect_all()
