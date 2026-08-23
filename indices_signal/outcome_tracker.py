#!/usr/bin/env python3
"""
Signal outcome tracker for indices_signal.

Runs after telegram_bot.py on each pipeline tick. For every open
(unresolved) signal in signals_sent, it replays the stored 4H price
history to determine whether Take Profit or Stop Loss was hit first,
and records the outcome. Signals still open beyond the tracking window
are marked EXPIRED so the statistics stay honest.

Unlike fx_signal's tracker (which checks the live yfinance spot price),
this one uses the price history already stored in prices.db — so it is
fully deterministic and needs no network.

Run via the pipeline (run_pipeline.sh), or manually:
    python3 outcome_tracker.py
"""

import logging
import sqlite3
import sys
from datetime import datetime, timezone

from indices_config import DB_PATH, PRIMARY_TIMEFRAME
from util import migrate_timestamps, utc_now_str

# How long (hours) we keep checking a signal for TP/SL before expiry.
OUTCOME_TRACK_HOURS = 7 * 24

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("outcome_tracker")


def ensure_schema():
    from signal_engine import ensure_schema as _ensure
    _ensure(DB_PATH)


def get_open(conn):
    return conn.execute(
        """
        SELECT rowid, ticker, signal_type, price, sl, tp, sent_at
        FROM signals_sent
        WHERE outcome IS NULL
          AND sl IS NOT NULL
          AND sent_at >= datetime('now', ?)
        ORDER BY sent_at ASC
        """,
        (f"-{OUTCOME_TRACK_HOURS} hours",),
    ).fetchall()


def resolve(row, conn):
    """Walk stored 4H candles after the signal to find TP/SL first."""
    _rowid, ticker, sig_type, _entry, sl, tp, sent_at = row
    if sl is None or tp is None:
        return None

    direction = "BUY" if sig_type == "BUY" else "SELL"

    candles = conn.execute(
        """
        SELECT high, low, datetime
        FROM prices
        WHERE ticker = ? AND timeframe = ? AND datetime > ?
          AND high IS NOT NULL AND low IS NOT NULL
        ORDER BY datetime ASC
        """,
        (ticker, PRIMARY_TIMEFRAME, sent_at),
    ).fetchall()

    for high, low, dt in candles:
        if direction == "BUY":
            if low <= sl:
                return "SL_HIT", low, dt
            if high >= tp:
                return "TP_HIT", high, dt
        else:
            if high >= sl:
                return "SL_HIT", high, dt
            if low <= tp:
                return "TP_HIT", low, dt
    return None


def expire_old(conn):
    cur = conn.execute(
        """
        UPDATE signals_sent
        SET outcome = 'EXPIRED', resolved_at = ?
        WHERE outcome IS NULL
          AND (sent_at < datetime('now', ?) OR sl IS NULL)
        """,
        (
            utc_now_str(),
            f"-{OUTCOME_TRACK_HOURS} hours",
        ),
    )
    if cur.rowcount:
        log.info("Expired %d unresolved signal(s).", cur.rowcount)
    conn.commit()


def print_stats(conn):
    rows = conn.execute(
        """
        SELECT ticker,
               COUNT(*) AS total,
               SUM(CASE WHEN outcome = 'TP_HIT' THEN 1 ELSE 0 END) AS tp
        FROM signals_sent
        WHERE outcome IS NOT NULL
        GROUP BY ticker
        ORDER BY ticker
        """
    ).fetchall()
    if not rows:
        log.info("No resolved outcomes yet.")
        return
    log.info("── Outcome Statistics ──────────────────────────────")
    for ticker, total, tp in rows:
        rate = (tp or 0) / total * 100 if total else 0
        log.info("  %s: %d trades | TP rate %.0f%%", ticker, total, rate)
    log.info("────────────────────────────────────────────────────")


def main():
    ensure_schema()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")

    expire_old(conn)

    open_rows = get_open(conn)
    if not open_rows:
        log.info("No open outcomes to check.")
        print_stats(conn)
        conn.close()
        return

    log.info("Checking %d open outcome(s)...", len(open_rows))
    resolved = 0
    for row in open_rows:
        result = resolve(row, conn)
        if not result:
            continue
        outcome, exit_price, _exit_dt = result
        rowid = row[0]
        conn.execute(
            """
            UPDATE signals_sent
            SET outcome = ?, outcome_price = ?, resolved_at = ?
            WHERE rowid = ?
            """,
            (
                outcome,
                exit_price,
                utc_now_str(),
                rowid,
            ),
        )
        conn.commit()
        log.info(
            "rowid=%s %s: %s at %.2f",
            rowid, row[2], outcome, exit_price,
        )
        resolved += 1

    log.info("Resolved %d / %d open outcome(s).", resolved, len(open_rows))
    print_stats(conn)
    conn.close()


if __name__ == "__main__":
    main()
