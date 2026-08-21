#!/usr/bin/env python3
"""
Signal strictness comparison tool (observational only) - evaluates the
current price_signals data under several confluence rule variants and
prints what each WOULD fire, side by side. Does NOT write to the
`signals` table and does NOT touch Telegram.

Variants, from strictest to loosest:
  strict  - EMA trend agrees on BOTH 1h and 4h, MACD confirms, RSI zone
  relaxed - EMA trend + MACD + RSI on 1h only (drops 4h agreement)
  loose   - EMA trend (1h) + RSI zone only (drops MACD too)

Run manually, any time - it's read-only:
    python3 signal_strictness_compare.py
"""

import sqlite3
import sys
from pathlib import Path

from fx_config import FX_PAIRS, RSI_OVERBOUGHT, RSI_OVERSOLD
from signal_engine import decide

PRICES_DB_PATH = Path(__file__).parent / "prices.db"


def get_latest_row(conn: sqlite3.Connection, pair: str, timeframe: str):
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


def validate_row(row, name: str) -> bool:
    if row is None:
        print(f"  {name}: NO DATA")
        return False
    critical = ["close", "ema_fast", "ema_slow", "rsi", "macd", "macd_signal", "atr", "bar_time"]
    for col in critical:
        if row[col] is None:
            print(f"  {name}: NULL value in column '{col}'")
            return False
    return True


def decide_loose(row_1h) -> tuple[str, str]:
    """EMA trend (1h only) + RSI zone. No MACD, no 4h check."""
    ema_f1 = row_1h["ema_fast"]
    ema_s1 = row_1h["ema_slow"]
    rsi1 = row_1h["rsi"]

    bull = ema_f1 > ema_s1 and rsi1 < RSI_OVERBOUGHT
    bear = ema_f1 < ema_s1 and rsi1 > RSI_OVERSOLD

    if bull:
        return "BUY", "1h trend only, RSI not overbought (no MACD, no 4h)"
    if bear:
        return "SELL", "1h trend only, RSI not oversold (no MACD, no 4h)"
    return "NO_SIGNAL", "-"


def main() -> None:
    conn = sqlite3.connect(PRICES_DB_PATH)

    # Tally for summary statistics
    tally = {
        "strict": {"BUY": 0, "SELL": 0, "NO_SIGNAL": 0},
        "relaxed": {"BUY": 0, "SELL": 0, "NO_SIGNAL": 0},
        "loose": {"BUY": 0, "SELL": 0, "NO_SIGNAL": 0},
    }

    print(f"{'PAIR':<8} {'VARIANT':<28} {'RESULT':<10} {'BAR_TIME':<22} REASON")
    print("-" * 110)

    for pair in FX_PAIRS:
        row_1h = get_latest_row(conn, pair, "1h")

        if not validate_row(row_1h, f"{pair}/1h"):
            print()
            continue

        bar_time = row_1h["bar_time"]

        # --- strict (needs 4h) ---
        row_4h = get_latest_row(conn, pair, "4h")
        if validate_row(row_4h, f"{pair}/4h"):
            direction, reason = decide(row_1h, row_4h, mode="strict")
        else:
            direction, reason = "N/A", "missing or invalid 4h data"
        tally["strict"][direction if direction in tally["strict"] else "NO_SIGNAL"] += 1
        print(f"{pair:<8} {'strict':<28} {direction:<10} {bar_time:<22} {reason}")

        # --- relaxed (1h only, imported from signal_engine) ---
        direction, reason = decide(row_1h, None, mode="relaxed")
        tally["relaxed"][direction] += 1
        print(f"{pair:<8} {'relaxed (drop 4h)':<28} {direction:<10} {bar_time:<22} {reason}")

        # --- loose (local, not in production) ---
        direction, reason = decide_loose(row_1h)
        tally["loose"][direction] += 1
        print(f"{pair:<8} {'loose (drop 4h+MACD)':<28} {direction:<10} {bar_time:<22} {reason}")
        print()

    # Summary
    print("-" * 110)
    print(f"{'VARIANT':<28} {'BUY':>6} {'SELL':>6} {'NO_SIGNAL':>10}")
    print("-" * 110)
    for variant, counts in tally.items():
        print(f"{variant:<28} {counts['BUY']:>6} {counts['SELL']:>6} {counts['NO_SIGNAL']:>10}")
    print("-" * 110)

    print(
        "\nThis is observational only - nothing above was written to the signals\n"
        "table or sent to Telegram. To change the live rule, set SIGNAL_MODE\n"
        "in fx_config.py or via the environment variable."
    )

    conn.close()


if __name__ == "__main__":
    main()
