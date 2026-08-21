#!/usr/bin/env python3
"""
Signal scoring engine for insider trades.

Scores each stock's daily insider activity and marks only
HIGH-VALUE alerts as ready for Telegram delivery.

Scoring factors (higher = more important):
    + Multiple filings same day     (coordination)
    + Multi-day streak              (persistent activity)
    + Large absolute share volume   (institutional flows)
    + Large % of shares outstanding (control implications)
    + One-sided direction           (net buying/selling)
    - Warrants/derivatives          (mechanical noise)
    - Small filings                 (individual noise)
    - Filing lag                    (stale trades = less edge)

Streaks are based on ACTUAL TRANSACTION DATES (date_of_change),
not published dates, to avoid false streaks from delayed filings.
"""

import argparse
import logging
import sqlite3
import sys
from datetime import date, timedelta

from collector import DB_PATH

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("signals_engine")


# ─── Thresholds ────────────────────────────────────────────────────

# Suppress these entirely - mostly mechanical
SKIP_STOCK_SUFFIXES = ("WA", "WB", "WC", "WD", "WE")  # warrants
SKIP_MIN_SHARES     = 100_000  # ignore tiny filings

# Score thresholds
ALERT_MIN_SCORE     = 7   # only send Telegram if score >= this
HIGH_PRIORITY_SCORE = 10  # 🔥 emoji if >= this


# ─── DB ────────────────────────────────────────────────────────────

def ensure_score_column(conn: sqlite3.Connection) -> None:
    cols = [r[1] for r in conn.execute(
        "PRAGMA table_info(bursa_insider_details)"
    ).fetchall()]
    if "signal_score" not in cols:
        conn.execute(
            "ALTER TABLE bursa_insider_details "
            "ADD COLUMN signal_score INTEGER DEFAULT 0"
        )
    if "alert_ready" not in cols:
        conn.execute(
            "ALTER TABLE bursa_insider_details "
            "ADD COLUMN alert_ready INTEGER DEFAULT 0"
        )
    conn.commit()


# ─── Scoring ───────────────────────────────────────────────────────

def score_stock_activity(conn: sqlite3.Connection, stock_code: str) -> dict:
    """
    Compute activity score for a stock based on recent filings.
    Returns dict with score breakdown.
    """
    result = {
        "stock_code":       stock_code,
        "score":            0,
        "streak_days":      0,
        "avg_lag_days":     0.0,
        "total_bought":     0,
        "total_sold":       0,
        "net_shares":       0,
        "n_filings":        0,
        "is_warrant":       False,
        "reasons":          [],
    }

    # ── Skip warrants entirely ────────────────────────────
    if any(stock_code.upper().endswith(sfx) for sfx in SKIP_STOCK_SUFFIXES):
        result["is_warrant"] = True
        result["reasons"].append("SKIP: warrant/derivative")
        return result

    # ── Get last 5 days of FILING activity ────────────────
    # We use published_date for the window because we only want to
    # score things we just found out about. But inside the window,
    # we use date_of_change for streaks and lag penalties.
    cutoff = (date.today() - timedelta(days=5)).isoformat()
    rows = conn.execute("""
        SELECT
            bid.published_date,
            bid.date_of_change,
            bid.transaction_type,
            bid.shares_transacted,
            bid.direct_pct_after,
            bid.indirect_pct_after
        FROM bursa_insider_details bid
        JOIN bursa_announcements ba ON bid.ann_id = ba.ann_id
        WHERE ba.stock_code = ?
        AND ba.subcategory IN ('DIRECTOR_S219', 'SUBSTANTIAL_S138')
        AND bid.published_date >= ?
        ORDER BY bid.published_date DESC
    """, (stock_code, cutoff)).fetchall()

    if not rows:
        return result

    result["n_filings"] = len(rows)
    unique_trade_dates = set()
    total_lag_days = 0
    lag_count = 0

    for pub_date, date_of_change, tx, shares, d_pct, i_pct in rows:
        # Use actual trade date for streaks; fallback to published date
        trade_date = date_of_change or pub_date
        if trade_date:
            unique_trade_dates.add(trade_date)

        # Calculate filing lag (published minus trade date)
        if date_of_change and pub_date:
            try:
                p = date.fromisoformat(pub_date)
                c = date.fromisoformat(date_of_change)
                lag = (p - c).days
                if lag > 0:
                    total_lag_days += lag
                    lag_count += 1
            except Exception:
                pass

        if tx == "ACQUISITION" and shares:
            result["total_bought"] += shares
        elif tx == "DISPOSAL" and shares:
            result["total_sold"] += shares

    result["net_shares"]  = result["total_bought"] - result["total_sold"]
    result["streak_days"] = len(unique_trade_dates)
    result["avg_lag_days"] = total_lag_days / lag_count if lag_count > 0 else 0.0

    # ── SCORING ──────────────────────────────────────────
    score = 0

    # 1. Streak scoring (persistence signal) — uses TRADE dates
    if result["streak_days"] >= 5:
        score += 5
        result["reasons"].append(f"+5 streak: {result['streak_days']} trade days")
    elif result["streak_days"] >= 3:
        score += 3
        result["reasons"].append(f"+3 streak: {result['streak_days']} trade days")
    elif result["streak_days"] >= 2:
        score += 1
        result["reasons"].append(f"+1 streak: {result['streak_days']} trade days")

    # 2. Filing frequency (coordination signal)
    if result["n_filings"] >= 10:
        score += 4
        result["reasons"].append(f"+4 volume: {result['n_filings']} filings")
    elif result["n_filings"] >= 5:
        score += 2
        result["reasons"].append(f"+2 volume: {result['n_filings']} filings")
    elif result["n_filings"] >= 3:
        score += 1
        result["reasons"].append(f"+1 volume: {result['n_filings']} filings")

    # 3. Absolute share volume (institutional flow)
    max_side = max(result["total_bought"], result["total_sold"])
    if max_side >= 50_000_000:
        score += 6
        result["reasons"].append(f"+6 mega volume: {max_side:,}")
    elif max_side >= 10_000_000:
        score += 4
        result["reasons"].append(f"+4 large volume: {max_side:,}")
    elif max_side >= 1_000_000:
        score += 2
        result["reasons"].append(f"+2 med volume: {max_side:,}")

    # 4. Direction strength (one-sided = stronger signal)
    if result["total_bought"] > 0 and result["total_sold"] == 0:
        score += 2
        result["reasons"].append("+2 pure buying")
    elif result["total_sold"] > 0 and result["total_bought"] == 0:
        score += 2
        result["reasons"].append("+2 pure selling")
    elif max_side > 0:
        ratio = max_side / (result["total_bought"] + result["total_sold"])
        if ratio >= 0.8:
            score += 1
            result["reasons"].append("+1 directional (80%+ one side)")

    # 5. Filing lag penalty (stale trades = less edge)
    avg_lag = result["avg_lag_days"]
    if avg_lag > 4:
        score -= 4
        result["reasons"].append(f"-4 stale: {avg_lag:.0f}d avg lag")
    elif avg_lag > 2:
        score -= 2
        result["reasons"].append(f"-2 delayed: {avg_lag:.0f}d avg lag")

    # 6. Skip if tiny
    if max_side < SKIP_MIN_SHARES:
        score = 0
        result["reasons"] = ["SKIP: too small"]

    result["score"] = max(0, score)
    return result


def update_scores(conn: sqlite3.Connection, days: int = 2) -> int:
    """
    Score all recent insider trades and mark alert_ready flag.
    Returns count of newly alert-ready records.
    """
    ensure_score_column(conn)

    cutoff = (date.today() - timedelta(days=days)).isoformat()

    # Get distinct stocks with undelivered filings
    stocks = conn.execute("""
        SELECT DISTINCT bid.stock_code
        FROM bursa_insider_details bid
        WHERE bid.delivered = 0
        AND bid.published_date >= ?
    """, (cutoff,)).fetchall()

    log.info("Scoring %d stocks with undelivered filings", len(stocks))

    alert_ready_count = 0
    skipped_count = 0

    for (stock_code,) in stocks:
        result = score_stock_activity(conn, stock_code)
        score = result["score"]
        alert_ready = 1 if score >= ALERT_MIN_SCORE else 0

        # Update all undelivered rows for this stock
        conn.execute("""
            UPDATE bursa_insider_details
            SET signal_score = ?, alert_ready = ?
            WHERE stock_code = ?
            AND delivered = 0
        """, (score, alert_ready, stock_code))

        if alert_ready:
            alert_ready_count += 1
            log.info(
                "🔥 %s score=%d: %s",
                stock_code, score, "; ".join(result["reasons"])
            )
        else:
            skipped_count += 1
            log.debug(
                "  %s score=%d (skip): %s",
                stock_code, score, "; ".join(result["reasons"]) or "no activity"
            )

    conn.commit()
    log.info(
        "Done. %d stocks ready to alert, %d suppressed",
        alert_ready_count, skipped_count
    )
    return alert_ready_count


# ─── Report ────────────────────────────────────────────────────────

def print_top_signals(conn: sqlite3.Connection, days: int = 2) -> None:
    """Print top-scored stocks to console."""
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    rows = conn.execute("""
        SELECT DISTINCT
            bid.stock_code,
            bid.company_name,
            bid.signal_score,
            bid.alert_ready
        FROM bursa_insider_details bid
        WHERE bid.published_date >= ?
        AND bid.signal_score > 0
        GROUP BY bid.stock_code
        ORDER BY bid.signal_score DESC
        LIMIT 30
    """, (cutoff,)).fetchall()

    print(f"\n{'='*90}")
    print(f"  TOP INSIDER SIGNALS (last {days} days)")
    print(f"{'='*90}")
    print(f"  {'Score':>5}  {'Alert':>5}  {'Code':<10} {'Company':<50}")
    print(f"  {'-'*5}  {'-'*5}  {'-'*10} {'-'*50}")
    for stock_code, company, score, alert_ready in rows:
        flag = "🔥" if alert_ready else "  "
        emoji = "✅" if score >= ALERT_MIN_SCORE else "🔕"
        print(f"  {score:>5}  {flag} {emoji}  {stock_code:<10} {str(company)[:50]}")
    print(f"{'='*90}\n")


# ─── Main ──────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Score insider trade signals")
    parser.add_argument("--days", type=int, default=2, help="Days to look back")
    parser.add_argument("--report", action="store_true", help="Show top signals")
    parser.add_argument(
        "--reset", action="store_true",
        help="Reset all alert_ready flags before scoring"
    )
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    ensure_score_column(conn)

    if args.reset:
        log.info("Resetting all alert_ready flags")
        conn.execute("UPDATE bursa_insider_details SET alert_ready = 0")
        conn.commit()

    update_scores(conn, days=args.days)

    if args.report:
        print_top_signals(conn, days=args.days)

    conn.close()


if __name__ == "__main__":
    main()
