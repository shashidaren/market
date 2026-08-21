# /opt/market/indices_signal/price_collector.py

import sqlite3
import logging

import pandas as pd
import yfinance as yf

from indices_config import (
    TICKERS,
    DB_PATH,
    PRIMARY_TIMEFRAME,
    TREND_TIMEFRAME,
    INTRADAY_LOOKBACK_DAYS,
    DAILY_LOOKBACK_DAYS,
)

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
            PRIMARY KEY (ticker, sent_at)
        )
    """)

    conn.commit()
    conn.close()

    logger.info("Database initialized")


# ============================================================
# DATA FETCHING
# ============================================================

def fetch_data(symbol, timeframe):
    """
    Fetch raw Yahoo Finance data.

    4H data is built from Yahoo 1H candles.
    Daily data is fetched directly.
    """

    if timeframe == "4h":
        interval = "1h"
        period = f"{INTRADAY_LOOKBACK_DAYS}d"
    else:
        interval = "1d"
        period = f"{DAILY_LOOKBACK_DAYS}d"

    logger.info(
        "Fetching %s | interval=%s | period=%s",
        symbol,
        interval,
        period,
    )

    df = yf.download(
        symbol,
        period=period,
        interval=interval,
        progress=False,
        auto_adjust=True,
    )

    if df.empty:
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
                    str(row[date_col]),
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

    for ticker_key in TICKERS:

        fetch_and_store(
            ticker_key,
            PRIMARY_TIMEFRAME,
        )

        fetch_and_store(
            ticker_key,
            TREND_TIMEFRAME,
        )

    logger.info("Collection complete")


if __name__ == "__main__":
    collect_all()
