#!/usr/bin/env python3
"""
Backfill signal outcomes by replaying stored signals against real OHLC bars.

WHY THIS EXISTS
---------------
outcome_tracker.py resolves signals against a LIVE SPOT SNAPSHOT every
pipeline run. Two problems with that:

  1. price_signals stores only CLOSES (no high/low), so any TP/SL touch
     and reversal *between* pipeline runs (15 min apart) is invisible —
     outcomes silently stay NULL and later become EXPIRED.
  2. Signals generated before outcome seeding landed (2026-08-24) have
     no signal_outcomes row at all — 18 of 20 delivered signals on the
     live box were completely untracked.

This script reconstructs the record:

  - fetches 1h OHLC (with High/Low) from Yahoo once per pair (30d),
    via the shared circuit-aware yahoo_client;
  - falls back to stored CLOSES in price_signals if Yahoo is unavailable
    (approximation: only resolves when a CLOSE is beyond the level);
  - walks bars after each signal's generated_at in time order;
    first bar breaching SL or TP resolves the trade;
  - a single bar breaching BOTH is scored SL_HIT (conservative);
  - no breach after TRACK_HOURS -> EXPIRED;
  - never overwrites an outcome that is already resolved;
  - fills missing signal_outcomes rows for old signals.

Exit prices assume a fill AT the SL/TP level (real fills are slightly
worse). minutes_to_exit uses the breaching bar's CLOSE time.

Usage (from repo root or fx_signal/):
    python3 fx_signal/backfill_outcomes.py            # replay + write
    python3 fx_signal/backfill_outcomes.py --dry-run  # report only
"""

import argparse
import logging
import sqlite3
import sys
import time
from datetime import timedelta
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
for _p in (str(_HERE), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pandas as pd  # noqa: E402
import yahoo_client  # noqa: E402
from fx_config import FX_PAIRS, SIGNAL_EXPIRY_HOURS  # noqa: E402
from util import parse_db_ts, to_db_str, utc_now_str  # noqa: E402

PRICES_DB_PATH = _HERE / "prices.db"

# Match outcome_tracker's window so stats are consistent.
TRACK_HOURS = max(int(SIGNAL_EXPIRY_HOURS * 6), 24)

# How much 1h history to pull for the replay. Must cover the oldest
# delivered signal; 30d is safely above it and cheap (one call/pair).
REPLAY_PERIOD = "30d"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("backfill")


# ------------------------------------------------------------------
# Data sources
# ------------------------------------------------------------------

def fetch_ohlc(yf_symbol: str) -> pd.DataFrame | None:
    """1h OHLC from Yahoo, index normalised to UTC. None on failure."""
    df = yahoo_client.history(yf_symbol, interval="1h", period=REPLAY_PERIOD)
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = df.columns.get_level_values(0)
    for col in ("High", "Low", "Close"):
        if col not in df.columns:
            log.warning("%s: missing %s column", yf_symbol, col)
            return None
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    else:
        idx = idx.tz_convert("UTC")
    df.index = idx
    return df[["High", "Low", "Close"]].dropna()


def stored_closes(conn: sqlite3.Connection, pair: str) -> pd.DataFrame | None:
    """Fallback: stored closes only (approximation, misses intra-bar)."""
    rows = conn.execute(
        """
        SELECT bar_time, close FROM price_signals
        WHERE pair = ? AND timeframe = '1h'
        ORDER BY bar_time
        """,
        (pair,),
    ).fetchall()
    if not rows:
        return None
    times = [parse_db_ts(r[0]) for r in rows]
    if any(t is None for t in times):
        return None
    return pd.DataFrame(
        {"Close": [float(r[1]) for r in rows]},
        index=pd.DatetimeIndex(times),
    )


# ------------------------------------------------------------------
# Replay core
# ------------------------------------------------------------------

def replay_signal(direction, entry, sl, tp, gen_dt, bars) -> dict | None:
    """
    Walk bars in time order starting at the bar that was forming when
    the signal was generated. Returns a result dict or None if the
    trade is still open (no breach within the track window).

    bars: DataFrame with High/Low/Close (or Close-only fallback) and a
    tz-aware UTC DatetimeIndex. Bar index = bar START time.
    """
    bar_len = pd.Timedelta(hours=1)
    # First bar of exposure: the bar whose start hour == generation hour.
    start = gen_dt.replace(minute=0, second=0, microsecond=0)
    window_end = gen_dt + timedelta(hours=TRACK_HOURS)
    has_hl = "High" in bars.columns and "Low" in bars.columns

    for bar_start, row in bars.iterrows():
        if bar_start < start:
            continue
        if bar_start >= window_end:
            break
        bar_close_time = bar_start + bar_len

        if has_hl:
            high, low = float(row["High"]), float(row["Low"])
            if direction == "BUY":
                sl_hit, tp_hit = low <= sl, high >= tp
            else:
                sl_hit, tp_hit = high >= sl, low <= tp
        else:
            close = float(row["Close"])
            if direction == "BUY":
                sl_hit, tp_hit = close <= sl, close >= tp
            else:
                sl_hit, tp_hit = close >= sl, close <= tp

        if sl_hit:  # conservative: SL wins ties / same-bar double breach
            return _result("SL_HIT", sl, gen_dt, bar_close_time)
        if tp_hit:
            return _result("TP_HIT", tp, gen_dt, bar_close_time)

    return None


def _result(outcome, exit_price, gen_dt, exit_dt) -> dict:
    return {
        "outcome": outcome,
        "exit_price": exit_price,
        "minutes_to_exit": round((exit_dt - gen_dt).total_seconds() / 60, 1),
        "resolved_at": to_db_str(exit_dt),
    }


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="report only, do not write to the DB")
    args = ap.parse_args()

    conn = sqlite3.connect(PRICES_DB_PATH)

    signals = conn.execute(
        """
        SELECT id, pair, direction, entry, stop_loss, take_profit,
               generated_at
        FROM signals
        WHERE delivered = 1
        ORDER BY id
        """
    ).fetchall()
    log.info("Loaded %d delivered signal(s).", len(signals))
    if not signals:
        return

    # Existing outcome state per signal_id.
    existing = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT signal_id, outcome FROM signal_outcomes"
        ).fetchall()
    }

    # One fetch per pair, shared across its signals.
    bars_by_pair: dict[str, pd.DataFrame | None] = {}
    for pair in {s[1] for s in signals}:
        cfg = FX_PAIRS.get(pair)
        df = None
        if cfg:
            df = fetch_ohlc(cfg["yf_symbol"])
            if df is None:
                log.warning(
                    "%s: Yahoo OHLC unavailable — falling back to stored "
                    "closes (approximation, misses intra-bar touches)", pair,
                )
                df = stored_closes(conn, pair)
            time.sleep(0.4)
        bars_by_pair[pair] = df
        src = "OHLC" if df is not None and "High" in df.columns else (
            "closes" if df is not None else "NONE"
        )
        log.info("%s: replay source = %s (%d bars)",
                 pair, src, 0 if df is None else len(df))

    now = utc_now_str()
    now_dt = parse_db_ts(now)
    report, writes, skipped = [], [], 0

    for sig_id, pair, direction, entry, sl, tp, gen_at in signals:
        prev = existing.get(sig_id, "MISSING")
        if prev not in ("MISSING", None):
            report.append((sig_id, pair, direction, prev, "kept"))
            continue  # already resolved — never overwrite

        if sl is None or tp is None:
            skipped += 1
            report.append((sig_id, pair, direction, "NO_SLTP", "skipped"))
            continue

        gen_dt = parse_db_ts(gen_at)
        bars = bars_by_pair.get(pair)
        if gen_dt is None or bars is None or bars.empty:
            skipped += 1
            report.append((sig_id, pair, direction, "NO_DATA", "skipped"))
            continue

        res = replay_signal(direction, entry, sl, tp, gen_dt, bars)
        if res is None:
            if gen_dt < now_dt - timedelta(hours=TRACK_HOURS):
                res = {
                    "outcome": "EXPIRED", "exit_price": None,
                    "minutes_to_exit": TRACK_HOURS * 60.0,
                    "resolved_at": to_db_str(
                        gen_dt + timedelta(hours=TRACK_HOURS)),
                }
            else:
                report.append((sig_id, pair, direction, "OPEN", "open"))
                continue

        if not args.dry_run:
            if prev == "MISSING":
                conn.execute(
                    """
                    INSERT INTO signal_outcomes
                        (signal_id, pair, direction, entry, stop_loss,
                         take_profit, generated_at, outcome, exit_price,
                         minutes_to_exit, resolved_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (sig_id, pair, direction, entry, sl, tp, gen_at,
                     res["outcome"], res["exit_price"],
                     res["minutes_to_exit"], res["resolved_at"]),
                )
            else:
                conn.execute(
                    """
                    UPDATE signal_outcomes
                    SET outcome = ?, exit_price = ?, minutes_to_exit = ?,
                        resolved_at = ?
                    WHERE signal_id = ? AND outcome IS NULL
                    """,
                    (res["outcome"], res["exit_price"],
                     res["minutes_to_exit"], res["resolved_at"], sig_id),
                )
        writes.append((sig_id, pair, direction, res["outcome"],
                       res["minutes_to_exit"]))
        report.append((sig_id, pair, direction, res["outcome"], "written"))

    if not args.dry_run:
        conn.commit()
    conn.close()

    # ---- Report -----------------------------------------------------
    print(f"\n{'id':>3}  {'pair':<7} {'dir':<4} {'outcome':<8} action")
    for sig_id, pair, direction, outcome, action in report:
        print(f"{sig_id:>3}  {pair:<7} {direction:<4} {outcome:<8} {action}")

    resolved = [w for w in writes]
    counts: dict[str, int] = {}
    for _, _, _, outcome, _ in resolved:
        counts[outcome] = counts.get(outcome, 0) + 1

    print(f"\nReplayed this run : {len(resolved)}  "
          f"(TP {counts.get('TP_HIT', 0)} | SL {counts.get('SL_HIT', 0)} | "
          f"EXPIRED {counts.get('EXPIRED', 0)})  skipped={skipped}")
    if args.dry_run:
        print("DRY RUN — nothing written. Re-run without --dry-run to save.")

    # Full-history summary from whatever is now in the table.
    conn = sqlite3.connect(PRICES_DB_PATH)
    rows = conn.execute(
        """
        SELECT direction, entry, stop_loss, take_profit, outcome
        FROM signal_outcomes
        WHERE outcome IN ('TP_HIT', 'SL_HIT')
          AND entry IS NOT NULL
          AND stop_loss IS NOT NULL
          AND take_profit IS NOT NULL
          AND stop_loss != entry
        """
    ).fetchall()
    conn.close()
    if rows:
        wins = losses = 0
        r_sum = 0.0
        for direction, entry, sl, tp, outcome in rows:
            risk = abs(entry - sl)
            reward = abs(tp - entry)
            r = reward / risk if risk else 1.0
            if outcome == "TP_HIT":
                wins += 1
                r_sum += r
            else:
                losses += 1
                r_sum -= 1.0
        n = wins + losses
        print(f"\nALL-TIME (decided trades): {n} | "
              f"TP {wins} ({wins / n * 100:.0f}%) | SL {losses} "
              f"({losses / n * 100:.0f}%)")
        print(f"Expectancy: {r_sum / n:+.2f} R per trade "
              f"(total {r_sum:+.1f} R)")
        print("  (1R = one risk unit; EXPIRED trades excluded, treated as scratch)")


if __name__ == "__main__":
    main()
