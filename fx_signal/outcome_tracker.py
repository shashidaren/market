#!/usr/bin/env python3
"""
Signal outcome tracker.

Runs after price_collector.py on each cron tick. Checks all open
(unresolved) signal outcomes against current price and records
whether TP or SL was hit.

Once MIN_BARS_FOR_STATS outcomes are recorded per pair/direction,
telegram_bot.py will display historical TP rates and median trade
duration in the signal message.

Run via cron (same schedule as signal_engine):
    */15 * * * * cd /opt/market/fx_signal && \
        /usr/bin/python3 outcome_tracker.py \
        >> /var/log/webscrap-fx-outcome.log 2>&1
"""

import logging
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yfinance as yf

from fx_config import FX_PAIRS, SIGNAL_EXPIRY_HOURS

PRICES_DB_PATH = Path(__file__).parent / "prices.db"

# Outcome tracker checks signals for up to N hours after generation.
# Set this longer than SIGNAL_EXPIRY_HOURS to catch slow-movers.
OUTCOME_TRACK_HOURS = max(SIGNAL_EXPIRY_HOURS * 6, 24)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("outcome_tracker")


def get_open_outcomes(conn: sqlite3.Connection) -> list[tuple]:
    """
    Returns unresolved outcome rows that are still within the
    tracking window and have been delivered (so we know the trade
    was actually communicated to the user).
    """
    rows = conn.execute(
        """
        SELECT
            so.signal_id,
            so.pair,
            so.direction,
            so.entry,
            so.stop_loss,
            so.take_profit,
            so.generated_at
        FROM signal_outcomes so
        JOIN signals s ON s.id = so.signal_id
        WHERE so.outcome IS NULL
          AND so.generated_at >= datetime('now', ?)
          AND s.delivered = 1
        ORDER BY so.signal_id ASC
        """,
        (f"-{OUTCOME_TRACK_HOURS} hours",),
    ).fetchall()
    return rows


def get_live_price(yf_symbol: str) -> float | None:
    """Fetch latest price via yfinance fast_info (attribute access)."""
    try:
        fi    = yf.Ticker(yf_symbol).fast_info
        price = getattr(fi, "last_price", None)
        if price is None:
            price = getattr(fi, "previous_close", None)
        return float(price) if price is not None else None
    except Exception:
        log.exception("Price fetch failed for %s", yf_symbol)
        return None


def resolve_outcome(
    signal_id: int,
    pair: str,
    direction: str,
    entry: float,
    stop_loss: float,
    take_profit: float,
    generated_at: str,
    live_price: float,
    conn: sqlite3.Connection,
) -> bool:
    """
    Checks if TP or SL has been breached by the current live price.
    Records outcome if triggered.

    Returns True if outcome was recorded, False if still open.

    NOTE: This uses the live spot price as a proxy. For precise
    backtesting you would need tick-level data. For dashboard
    statistics this is sufficient.
    """
    tp_hit = False
    sl_hit = False

    if direction == "BUY":
        tp_hit = live_price >= take_profit
        sl_hit = live_price <= stop_loss
    else:  # SELL
        tp_hit = live_price <= take_profit
        sl_hit = live_price >= stop_loss

    if not tp_hit and not sl_hit:
        return False

    outcome     = "TP_HIT" if tp_hit else "SL_HIT"
    exit_price  = live_price
    resolved_at = datetime.now(timezone.utc).isoformat()

    # Calculate minutes from signal generation to resolution
    try:
        gen_dt = datetime.fromisoformat(generated_at)
        if gen_dt.tzinfo is None:
            gen_dt = gen_dt.replace(tzinfo=timezone.utc)
        minutes_to_exit = (
            datetime.now(timezone.utc) - gen_dt
        ).total_seconds() / 60
    except (ValueError, TypeError):
        minutes_to_exit = None

    conn.execute(
        """
        UPDATE signal_outcomes
        SET outcome         = ?,
            exit_price      = ?,
            minutes_to_exit = ?,
            resolved_at     = ?
        WHERE signal_id = ?
        """,
        (outcome, exit_price, minutes_to_exit, resolved_at, signal_id),
    )
    conn.commit()

    log.info(
        "signal_id=%d %s %s: %s at %.5f (%.0f min from generation)",
        signal_id, direction, pair, outcome, exit_price,
        minutes_to_exit or 0,
    )
    return True


def expire_old_outcomes(conn: sqlite3.Connection) -> None:
    """
    Mark signals as EXPIRED if they never hit TP or SL within the
    tracking window. Keeps statistics honest — expired trades are
    not wins.
    """
    conn.execute(
        """
        UPDATE signal_outcomes
        SET outcome     = 'EXPIRED',
            resolved_at = datetime('now')
        WHERE outcome IS NULL
          AND generated_at < datetime('now', ?)
        """,
        (f"-{OUTCOME_TRACK_HOURS} hours",),
    )
    expired_count = conn.execute(
        "SELECT changes()"
    ).fetchone()[0]
    if expired_count:
        log.info("Expired %d unresolved signal outcome(s)", expired_count)
    conn.commit()


def print_stats(conn: sqlite3.Connection) -> None:
    """Log a summary of resolved outcomes per pair/direction."""
    rows = conn.execute(
        """
        SELECT pair, direction,
               COUNT(*) as total,
               SUM(CASE WHEN outcome = 'TP_HIT' THEN 1 ELSE 0 END) as tp_hits,
               AVG(CASE WHEN outcome = 'TP_HIT' THEN minutes_to_exit END) as avg_tp_min
        FROM signal_outcomes
        WHERE outcome IS NOT NULL
        GROUP BY pair, direction
        ORDER BY pair, direction
        """
    ).fetchall()

    if not rows:
        log.info("No resolved outcomes yet.")
        return

    log.info("── Outcome Statistics ──────────────────────────────")
    for r in rows:
        pair, direction, total, tp_hits, avg_tp = r
        tp_rate  = (tp_hits / total * 100) if total else 0
        avg_str  = f"{avg_tp:.0f} min" if avg_tp else "n/a"
        log.info(
            "  %s %s: %d trades | TP rate %.0f%% | Avg TP time %s",
            pair, direction, total, tp_rate, avg_str,
        )
    log.info("────────────────────────────────────────────────────")


def main() -> None:
    conn = sqlite3.connect(PRICES_DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")

    # First expire anything beyond the tracking window
    expire_old_outcomes(conn)

    open_outcomes = get_open_outcomes(conn)
    if not open_outcomes:
        log.info("No open outcomes to check.")
        print_stats(conn)
        conn.close()
        return

    log.info("Checking %d open outcome(s)...", len(open_outcomes))

    resolved = 0
    for row in open_outcomes:
        (signal_id, pair, direction, entry,
         stop_loss, take_profit, generated_at) = row

        yf_symbol = FX_PAIRS.get(pair, {}).get("yf_symbol")
        if not yf_symbol:
            log.warning("No yf_symbol for %s, skipping", pair)
            continue

        live_price = get_live_price(yf_symbol)
        if live_price is None:
            log.warning(
                "signal_id=%d %s: live price unavailable, skipping",
                signal_id, pair,
            )
            continue

        did_resolve = resolve_outcome(
            signal_id, pair, direction,
            entry, stop_loss, take_profit,
            generated_at, live_price, conn,
        )
        if did_resolve:
            resolved += 1

        time.sleep(0.3)   # rate limit

    log.info(
        "Resolved %d / %d open outcome(s) this run.",
        resolved, len(open_outcomes),
    )
    print_stats(conn)
    conn.close()


if __name__ == "__main__":
    main()
