#!/usr/bin/env python3
"""
FX signal engine — MARKET EXECUTION MODEL.

Reads the latest completed 1h and 4h bar per pair from prices.db and
fires BUY / SELL when a FRESH crossover is detected.

KEY CHANGES vs previous version:
    - Crossover detection: signals only on the bar where EMA/MACD
      JUST crossed, not when the state has been true for N bars.
      This eliminates the ~1 hour lag where the signal fires on an
      already-moving trend.
    - SIGNAL_EXPIRY_HOURS now imported and used (was hardcoded to 2h).
    - 1h bar freshness check added (was missing entirely).
    - Direction dedup limited to a 4-hour window (was all-time).

Decision logic:
    strict (default): fresh 1h EMA cross + 4h EMA state agreement
                      + fresh MACD(1h) cross + RSI(1h) zone
    relaxed:          fresh 1h EMA cross + fresh MACD(1h) cross
                      + RSI(1h) zone (4h not checked)

Run via cron after price_collector.py:
    */15 * * * * cd /opt/market/fx_signal && \
        /usr/bin/python3 signal_engine.py
"""

import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from fx_config import (
    FX_PAIRS,
    RSI_OVERBOUGHT,
    RSI_OVERSOLD,
    SIGNAL_MODE,
    SIGNAL_EXPIRY_HOURS,
    get_min_sl_pips,
)
from util import migrate_timestamps, to_db_str, utc_now_str

VALID_MODES    = ("strict", "relaxed")
EXECUTION_TYPE = "market"

# Maximum age for a 1h bar before we refuse to signal on it.
# 1h candle + 30 min tolerance for Yahoo lag + cron offset.
MAX_1H_BAR_AGE_MINUTES = 90

PRICES_DB_PATH = Path(__file__).parent / "prices.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("signal_engine")


# ------------------------------------------------------------------
# DB setup
# ------------------------------------------------------------------

def init_signals_table(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS signals (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            pair            TEXT    NOT NULL,
            direction       TEXT    NOT NULL,
            entry           REAL    NOT NULL,
            stop_loss       REAL    NOT NULL,
            take_profit     REAL    NOT NULL,
            atr_1h          REAL    NOT NULL,
            rsi_1h          REAL    NOT NULL,
            rationale       TEXT,
            execution_type  TEXT,
            generated_at    TEXT    NOT NULL,
            expires_at      TEXT,
            delivered       INTEGER NOT NULL DEFAULT 0,
            delivered_at    TEXT,
            suppressed      INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    # Safe migrations for existing DBs
    _add_column_if_missing(conn, "signals", "expires_at",     "TEXT")
    _add_column_if_missing(conn, "signals", "execution_type", "TEXT")
    _add_column_if_missing(conn, "signals", "delivered_at",   "TEXT")
    _add_column_if_missing(conn, "signals", "suppressed",     "INTEGER NOT NULL DEFAULT 0")
    # Set when the DO NOT EXECUTE message is first sent for a signal —
    # guards against re-sending it every cron cycle.
    _add_column_if_missing(conn, "signals", "suppressed_notified_at", "TEXT")

    # Outcome tracking table — populated by outcome_tracker.py
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS signal_outcomes (
            signal_id       INTEGER PRIMARY KEY,
            pair            TEXT    NOT NULL,
            direction       TEXT    NOT NULL,
            entry           REAL,
            stop_loss       REAL,
            take_profit     REAL,
            generated_at    TEXT,
            outcome         TEXT,        -- 'TP_HIT' | 'SL_HIT' | 'EXPIRED'
            exit_price      REAL,
            minutes_to_exit REAL,        -- NULL until resolved
            resolved_at     TEXT         -- NULL until resolved
        )
        """
    )
    # Convert legacy 'T'/'+00:00' ISO timestamps to the SQLite-comparable
    # space format (REVIEW.md item #1).
    migrate_timestamps(conn)
    conn.commit()


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, col: str, dtype: str
) -> None:
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {dtype}")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Column already exists


# ------------------------------------------------------------------
# Data access
# ------------------------------------------------------------------

def get_latest_row(
    conn: sqlite3.Connection, pair: str, timeframe: str
):
    conn.row_factory = sqlite3.Row
    return conn.execute(
        """
        SELECT close, ema_fast, ema_slow, rsi, macd, macd_signal, atr, bar_time
        FROM price_signals
        WHERE pair = ? AND timeframe = ?
        ORDER BY bar_time DESC
        LIMIT 1
        """,
        (pair, timeframe),
    ).fetchone()


def get_previous_row(
    conn: sqlite3.Connection, pair: str, timeframe: str
):
    """Second most recent completed bar — needed for crossover detection."""
    conn.row_factory = sqlite3.Row
    return conn.execute(
        """
        SELECT close, ema_fast, ema_slow, rsi, macd, macd_signal, atr, bar_time
        FROM price_signals
        WHERE pair = ? AND timeframe = ?
        ORDER BY bar_time DESC
        LIMIT 1 OFFSET 1
        """,
        (pair, timeframe),
    ).fetchone()


def validate_row(row, label: str) -> bool:
    if row is None:
        log.warning("%s: no data row found", label)
        return False
    critical = ["close", "ema_fast", "ema_slow", "rsi", "macd", "macd_signal", "atr", "bar_time"]
    for col in critical:
        if row[col] is None:
            log.warning("%s: NULL in column '%s', skipping", label, col)
            return False
    return True


def check_bar_freshness(row, label: str) -> bool:
    """
    Returns True if the bar is recent enough to trade on.
    Rejects bars older than MAX_1H_BAR_AGE_MINUTES regardless of what
    the staleness check in price_collector passed — this is a second
    line of defence at decision time.
    """
    try:
        bar_dt = datetime.fromisoformat(row["bar_time"])
        if bar_dt.tzinfo is None:
            bar_dt = bar_dt.replace(tzinfo=timezone.utc)
        age_minutes = (datetime.now(timezone.utc) - bar_dt).total_seconds() / 60
        if age_minutes > MAX_1H_BAR_AGE_MINUTES:
            log.warning(
                "%s: 1h bar is %.0f min old (max %d), skipping stale signal",
                label, age_minutes, MAX_1H_BAR_AGE_MINUTES,
            )
            return False
        return True
    except (ValueError, TypeError) as exc:
        log.warning("%s: could not parse bar_time — %s", label, exc)
        return False


def get_last_signal_direction(
    conn: sqlite3.Connection, pair: str
) -> str | None:
    """
    Returns the most recent signal direction within the last
    SIGNAL_EXPIRY_HOURS * 2 window.

    Using all-time history caused valid fresh crossovers to be
    suppressed if the same direction had fired days ago.
    """
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT direction FROM signals
        WHERE pair = ?
          AND generated_at >= datetime('now', ?)
        ORDER BY id DESC
        LIMIT 1
        """,
        (pair, f"-{SIGNAL_EXPIRY_HOURS * 2} hours"),
    ).fetchone()
    return row["direction"] if row else None


# ------------------------------------------------------------------
# Decision logic — crossover-based
# ------------------------------------------------------------------

def decide(
    row_1h,
    row_1h_prev,
    row_4h,
    mode: str = "strict",
) -> tuple[str, str]:
    """
    Returns (direction, rationale).

    Crossover detection:
        We require that EMA and MACD JUST crossed on this bar,
        i.e. they were in the opposite state on the previous bar.
        This prevents firing mid-trend on a condition that has been
        true for hours, which was the primary cause of the ~1h delay.

    Fallback:
        If row_1h_prev is None (first run / fresh DB), falls back
        to state-based detection with a warning.
    """
    if mode not in VALID_MODES:
        raise ValueError(f"Unknown mode {mode!r}")

    ema_fast_1h = row_1h["ema_fast"]
    ema_slow_1h = row_1h["ema_slow"]
    rsi_1h      = row_1h["rsi"]
    macd_1h     = row_1h["macd"]
    macd_sig_1h = row_1h["macd_signal"]

    # Current state
    ema_bull_now  = ema_fast_1h > ema_slow_1h
    ema_bear_now  = ema_fast_1h < ema_slow_1h
    macd_bull_now = macd_1h     > macd_sig_1h
    macd_bear_now = macd_1h     < macd_sig_1h

    # Crossover detection — did we JUST cross on this bar?
    if row_1h_prev is not None:
        prev_ema_bull  = row_1h_prev["ema_fast"] > row_1h_prev["ema_slow"]
        prev_macd_bull = row_1h_prev["macd"]     > row_1h_prev["macd_signal"]

        ema_just_bull  = ema_bull_now  and not prev_ema_bull   # crossed UP
        ema_just_bear  = ema_bear_now  and     prev_ema_bull   # crossed DOWN
        macd_just_bull = macd_bull_now and not prev_macd_bull
        macd_just_bear = macd_bear_now and     prev_macd_bull
    else:
        # No previous row — fall back to state, warn once
        log.warning(
            "No previous 1h bar found — using state-based detection (first run?). "
            "Signal may be mid-trend; treat with extra caution."
        )
        ema_just_bull  = ema_bull_now
        ema_just_bear  = ema_bear_now
        macd_just_bull = macd_bull_now
        macd_just_bear = macd_bear_now

    # 4h context (strict mode): require 4h to be in the same EMA STATE
    # (not crossover — 4h flips infrequently; we use it for trend bias only)
    if mode == "strict":
        ema_fast_4h   = row_4h["ema_fast"]
        ema_slow_4h   = row_4h["ema_slow"]
        bull_trend_ok = ema_just_bull and (ema_fast_4h > ema_slow_4h)
        bear_trend_ok = ema_just_bear and (ema_fast_4h < ema_slow_4h)
        trend_desc    = "1h cross + 4h trend"
    else:
        bull_trend_ok = ema_just_bull
        bear_trend_ok = ema_just_bear
        trend_desc    = "1h cross (relaxed — 4h not checked)"

    # BUY
    if bull_trend_ok and macd_just_bull and rsi_1h < RSI_OVERBOUGHT:
        rationale = (
            f"[{mode} | MARKET] Fresh bullish EMA cross on 1h "
            f"({trend_desc}), MACD just crossed bullish, "
            f"RSI(1h)={rsi_1h:.1f} (not overbought). "
            f"Execute at MARKET — SL/TP are fixed levels."
        )
        return "BUY", rationale

    # SELL
    if bear_trend_ok and macd_just_bear and rsi_1h > RSI_OVERSOLD:
        rationale = (
            f"[{mode} | MARKET] Fresh bearish EMA cross on 1h "
            f"({trend_desc}), MACD just crossed bearish, "
            f"RSI(1h)={rsi_1h:.1f} (not oversold). "
            f"Execute at MARKET — SL/TP are fixed levels."
        )
        return "SELL", rationale

    return "NO_SIGNAL", "No fresh crossover confluence."


# ------------------------------------------------------------------
# SL/TP calculation
# ------------------------------------------------------------------

def compute_sl_tp(
    direction: str, entry: float, atr_1h: float, cfg: dict, pair: str = ""
) -> tuple[float, float]:
    min_sl_dist = get_min_sl_pips(pair) * cfg["pip_size"]
    sl_dist     = max(cfg["atr_mult_sl"] * atr_1h, min_sl_dist)
    tp_dist     = cfg["atr_mult_tp"] * atr_1h

    if direction == "BUY":
        return entry - sl_dist, entry + tp_dist
    if direction == "SELL":
        return entry + sl_dist, entry - tp_dist
    raise ValueError(f"Unexpected direction {direction!r}")


# ------------------------------------------------------------------
# Storage
# ------------------------------------------------------------------

def store_signal(
    conn: sqlite3.Connection,
    pair: str,
    direction: str,
    entry: float,
    stop_loss: float,
    take_profit: float,
    atr_1h: float,
    rsi_1h: float,
    rationale: str,
    execution_type: str,
) -> int:
    """
    Inserts signal and returns the new signal ID.

    IMPORTANT: this function does NOT commit — the caller owns the
    transaction (signal_engine.main wraps the dedup check + insert in
    BEGIN IMMEDIATE and commits once). Committing here would break the
    caller's atomicity: a later ROLLBACK could no longer undo this row.
    """
    now        = utc_now_str()
    expires_at = to_db_str(
        datetime.now(timezone.utc) + timedelta(hours=SIGNAL_EXPIRY_HOURS)
    )

    cur = conn.execute(
        """
        INSERT INTO signals
            (pair, direction, entry, stop_loss, take_profit,
             atr_1h, rsi_1h, rationale, execution_type,
             generated_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pair, direction, entry, stop_loss, take_profit,
            atr_1h, rsi_1h, rationale, execution_type,
            now, expires_at,
        ),
    )

    # Seed the outcome tracking row (same transaction as the insert)
    conn.execute(
        """
        INSERT OR IGNORE INTO signal_outcomes
            (signal_id, pair, direction, entry, stop_loss,
             take_profit, generated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (cur.lastrowid, pair, direction, entry, stop_loss, take_profit, now),
    )

    return cur.lastrowid


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def main() -> None:
    mode = os.environ.get("SIGNAL_MODE", SIGNAL_MODE)
    if mode not in VALID_MODES:
        log.error("Invalid SIGNAL_MODE=%r. Aborting.", mode)
        sys.exit(1)

    log.info(
        "signal_engine starting — mode=%s expiry=%dh execution=MARKET",
        mode, SIGNAL_EXPIRY_HOURS,
    )

    conn = sqlite3.connect(PRICES_DB_PATH)
    init_signals_table(conn)

    for pair, cfg in FX_PAIRS.items():
        # ── Fetch 1h rows ────────────────────────────────────────
        row_1h = get_latest_row(conn, pair, "1h")
        if not validate_row(row_1h, f"{pair}/1h"):
            continue

        if not check_bar_freshness(row_1h, f"{pair}/1h"):
            continue

        row_1h_prev = get_previous_row(conn, pair, "1h")
        # row_1h_prev may be None on first run — decide() handles that

        # ── Fetch 4h row (strict mode only) ──────────────────────
        row_4h = None

        if mode == "strict":
            row_4h = get_latest_row(conn, pair, "4h")
            if not validate_row(row_4h, f"{pair}/4h"):
                continue

            bt_1h = datetime.fromisoformat(row_1h["bar_time"])
            bt_4h = datetime.fromisoformat(row_4h["bar_time"])
            if bt_1h.tzinfo is None:
                bt_1h = bt_1h.replace(tzinfo=timezone.utc)
            if bt_4h.tzinfo is None:
                bt_4h = bt_4h.replace(tzinfo=timezone.utc)

            if (bt_1h - bt_4h) > timedelta(hours=6):
                log.warning(
                    "%s: 4h bar is >6h behind 1h bar — data sync issue, skipping",
                    pair,
                )
                continue

        # ── Signal decision ───────────────────────────────────────
        direction, rationale = decide(row_1h, row_1h_prev, row_4h, mode=mode)
        if direction == "NO_SIGNAL":
            log.info("%s: NO_SIGNAL — %s", pair, rationale)
            continue

        entry  = row_1h["close"]
        atr_1h = row_1h["atr"]
        rsi_1h = row_1h["rsi"]

        # ── Dedup within recent window ────────────────────────────
        conn.execute("BEGIN IMMEDIATE")
        try:
            last_direction = get_last_signal_direction(conn, pair)
            if last_direction == direction:
                log.info(
                    "%s: %s already fired within dedup window, skipping",
                    pair, direction,
                )
                conn.execute("ROLLBACK")
                continue

            stop_loss, take_profit = compute_sl_tp(direction, entry, atr_1h, cfg, pair)

            signal_id = store_signal(
                conn, pair, direction, entry, stop_loss, take_profit,
                atr_1h, rsi_1h, rationale, EXECUTION_TYPE,
            )
            # Single commit for the whole dedup+insert+outcome-seed
            # transaction (store_signal deliberately does not commit).
            conn.commit()

            log.info(
                "%s: NEW %s signal id=%d entry=%.5f sl=%.5f tp=%.5f",
                pair, direction, signal_id, entry, stop_loss, take_profit,
            )

        except Exception:
            conn.execute("ROLLBACK")
            raise

    conn.close()
    log.info("signal_engine complete.")


if __name__ == "__main__":
    main()
