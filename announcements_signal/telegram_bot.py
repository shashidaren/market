#!/usr/bin/env python3
"""
Telegram delivery for Bursa insider trade alerts.

Only sends alerts for trades marked `alert_ready = 1` by signals_engine.py.
Includes BOTH director (S219) and substantial shareholder (S138) filings,
with automatic deduplication to avoid duplicate messages for the same
transaction.

Score tiers:
    10+  → 🔥🔥🔥 MEGA SIGNAL   (streak + mega volume)
     7-9 → 🔥     Strong signal (large volume or persistent activity)
    <7   → suppressed (not sent to Telegram, stays in DB for website)

Deduplication logic:
    When the same person files both S219 (Director) and S138 (Substantial
    Shareholder) for the same transaction, we prefer the S219 filing because
    it includes the price (consideration).

Cron:
    */10 * * * *    cd /opt/market/announcements_signal && python3 collector.py       >> /var/log/webscrap-bursa-collector.log 2>&1
    2-59/10 * * * * cd /opt/market/announcements_signal && python3 parse_detail.py    --days 1 >> /var/log/webscrap-bursa-parse.log     2>&1
    3-59/10 * * * * cd /opt/market/announcements_signal && python3 signals_engine.py  --days 2 >> /var/log/webscrap-bursa-scorer.log    2>&1
    4-59/10 * * * * cd /opt/market/announcements_signal && python3 telegram_bot.py             >> /var/log/webscrap-bursa-telegram.log  2>&1
"""

import html
import logging
import os
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta

import requests

from collector import DB_PATH
from price_context import get_price_context, format_price_block
from ireport_filter import analyze_stock, load_portfolio, ALLOWED_DECISIONS

TELEGRAM_API_BASE = "https://api.telegram.org"
REQUEST_TIMEOUT   = 10

# ── i_report filter ────────────────────────────────────────
# Set IREPORT_FILTER=off in .env to disable filtering entirely.
# Stocks listed in portfolio.txt always alert (📌 HOLDING).
IREPORT_FILTER_ENABLED = os.environ.get(
    "IREPORT_FILTER", "on").lower() not in ("off", "0", "false", "no")
PORTFOLIO = load_portfolio()

# Score tiers for display (must align with signals_engine.py thresholds)
MEGA_SIGNAL_SCORE   = 10
STRONG_SIGNAL_SCORE = 7

# Filter Thresholds
HEAVY_SELLING_THRESHOLD = 5000000  # 5 Million shares
FUTURE_DATE_BUFFER_DAYS = 1        # Allow 1 day ahead for timezone quirks, reject beyond

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("insider_telegram")


# ─── Date parsing helper ───────────────────────────────────────────

def _parse_flexible_date(raw: str) -> date | None:
    """
    Parse a date string in either ISO ('2026-08-13') or
    Bursa human format ('13 Aug 2026'). Returns None if unparseable.
    """
    if not raw:
        return None
    raw = raw.strip()

    # Fast path: ISO format
    try:
        return date.fromisoformat(raw)
    except (ValueError, TypeError):
        pass

    # Bursa human formats
    for fmt in ("%d %b %Y", "%d %B %Y", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except (ValueError, TypeError):
            continue

    log.warning("Could not parse date: %r", raw)
    return None


# ─── DB ────────────────────────────────────────────────────────────

def ensure_delivered_column(conn: sqlite3.Connection) -> None:
    """Safe migration — add delivered column if missing."""
    cols = [r[1] for r in conn.execute(
        "PRAGMA table_info(bursa_insider_details)"
    ).fetchall()]
    if "delivered" not in cols:
        conn.execute(
            "ALTER TABLE bursa_insider_details "
            "ADD COLUMN delivered INTEGER DEFAULT 0"
        )
        conn.commit()


def get_undelivered(conn: sqlite3.Connection) -> list[dict]:
    """
    Fetch parsed insider trades that:
      - Have not been delivered to Telegram yet
      - Have been marked alert_ready by signals_engine.py
      - Are from the last 2 days (avoids backfill flooding)
      - Are S219 director OR S138 substantial shareholder filings
    """
    cutoff = (date.today() - timedelta(days=2)).isoformat()
    rows = conn.execute("""
        SELECT
            bid.ann_id,
            bid.stock_code,
            bid.company_name,
            bid.published_date,
            bid.date_of_change,
            bid.person_name,
            bid.transaction_type,
            bid.nature_of_interest,
            bid.shares_transacted,
            bid.consideration,
            bid.direct_units_after,
            bid.direct_pct_after,
            bid.indirect_units_after,
            bid.indirect_pct_after,
            bid.circumstances,
            ba.subcategory,
            bid.signal_score
        FROM bursa_insider_details bid
        JOIN bursa_announcements ba ON bid.ann_id = ba.ann_id
        WHERE bid.delivered = 0
        AND bid.alert_ready = 1
        AND bid.published_date >= ?
        AND bid.transaction_type IS NOT NULL
        AND ba.subcategory IN ('DIRECTOR_S219', 'SUBSTANTIAL_S138')
        ORDER BY bid.signal_score DESC, bid.stock_code, bid.published_date DESC
    """, (cutoff,)).fetchall()

    cols = [
        "ann_id", "stock_code", "company_name", "published_date",
        "date_of_change", "person_name", "transaction_type", "nature_of_interest",
        "shares_transacted", "consideration",
        "direct_units_after", "direct_pct_after",
        "indirect_units_after", "indirect_pct_after",
        "circumstances", "subcategory", "signal_score"
    ]
    return [dict(zip(cols, r)) for r in rows]


def get_ownership_snapshot(conn: sqlite3.Connection, stock_code: str) -> list[dict]:
    """
    Latest ownership % per person for this stock.
    Uses date_of_change (actual trade date) for recency, falls back to published_date.
    Includes both directors (S219) and substantial shareholders (S138).
    """
    rows = conn.execute("""
        SELECT
            bid.person_name,
            bid.direct_pct_after,
            bid.indirect_pct_after
        FROM bursa_insider_details bid
        JOIN bursa_announcements ba ON bid.ann_id = ba.ann_id
        WHERE ba.stock_code = ?
        AND ba.subcategory IN ('DIRECTOR_S219', 'SUBSTANTIAL_S138')
        AND COALESCE(bid.date_of_change, bid.published_date) = (
            SELECT MAX(COALESCE(bid2.date_of_change, bid2.published_date))
            FROM bursa_insider_details bid2
            JOIN bursa_announcements ba2 ON bid2.ann_id = ba2.ann_id
            WHERE ba2.stock_code = ?
            AND ba2.subcategory IN ('DIRECTOR_S219', 'SUBSTANTIAL_S138')
            AND bid2.person_name = bid.person_name
        )
        GROUP BY bid.person_name
        ORDER BY (COALESCE(bid.direct_pct_after, 0) +
                  COALESCE(bid.indirect_pct_after, 0)) DESC
    """, (stock_code, stock_code)).fetchall()

    return [
        {
            "person_name":  r[0],
            "direct_pct":   r[1] or 0.0,
            "indirect_pct": r[2] or 0.0,
            "total_pct":    (r[1] or 0.0) + (r[2] or 0.0),
        }
        for r in rows
    ]


def get_activity_streak(conn: sqlite3.Connection, stock_code: str) -> int:
    """
    How many consecutive trading days has this stock had insider filings?
    Counts DISTINCT transaction dates (date_of_change), not filing dates.
    Falls back to published_date if transaction date is missing.
    Handles both ISO ('2026-08-13') and Bursa human ('13 Aug 2026') formats.
    """
    rows = conn.execute("""
        SELECT DISTINCT activity_date
        FROM (
            SELECT COALESCE(bid.date_of_change, bid.published_date) as activity_date
            FROM bursa_insider_details bid
            JOIN bursa_announcements ba ON bid.ann_id = ba.ann_id
            WHERE ba.stock_code = ?
            AND ba.subcategory IN ('DIRECTOR_S219', 'SUBSTANTIAL_S138')
        )
        WHERE activity_date IS NOT NULL
        ORDER BY activity_date DESC
        LIMIT 30
    """, (stock_code,)).fetchall()

    if not rows:
        return 0

    # Normalize all dates to ISO — skip un-parseable ones
    parsed_dates = []
    for r in rows:
        d = _parse_flexible_date(r[0])
        if d:
            parsed_dates.append(d)

    if not parsed_dates:
        return 0

    # Re-sort after normalization (most recent first)
    parsed_dates.sort(reverse=True)

    streak = 1
    for i in range(1, len(parsed_dates)):
        diff = (parsed_dates[i - 1] - parsed_dates[i]).days
        if diff <= 3:  # allow weekends
            streak += 1
        else:
            break
    return streak


def mark_delivered(conn: sqlite3.Connection, ann_ids: list[int]) -> None:
    conn.executemany(
        "UPDATE bursa_insider_details SET delivered = 1 WHERE ann_id = ?",
        [(i,) for i in ann_ids]
    )
    conn.commit()


def mark_delivered_all_for_stock(
    conn: sqlite3.Connection,
    stock_code: str,
    published_dates: list[str],
) -> None:
    """
    Mark ALL rows for this stock's affected dates as delivered,
    including the ones we suppressed via dedup.
    Prevents them from appearing in subsequent runs.
    """
    if not published_dates:
        return
    placeholders = ",".join("?" * len(published_dates))
    conn.execute(f"""
        UPDATE bursa_insider_details
        SET delivered = 1
        WHERE stock_code = ?
        AND published_date IN ({placeholders})
        AND alert_ready = 1
    """, [stock_code, *published_dates])
    conn.commit()


# ─── Formatting ────────────────────────────────────────────────────

def format_stock_alert(
    stock_code: str,
    trades: list[dict],
    ownership: list[dict],
    streak: int,
) -> str | None:
    """
    Build one Telegram HTML message per stock.
    Shows latest day's trades + ownership snapshot + signal score.
    Includes trade-vs-filed lag so users know how stale the signal is.
    Enriched with live price context via price_context.py.

    RETURNS:
        str: The formatted message
        None: If the alert should be suppressed (e.g., stagnant price + selling)
    """
    if not trades:
        return None

    company      = trades[0].get("company_name") or f"Stock {stock_code}"
    pub_date_raw = trades[0]["published_date"]
    txn_date_raw = trades[0].get("date_of_change") or pub_date_raw
    signal_score = trades[0].get("signal_score") or 0

    # ── Filing lag ───────────────────────────────────────
    lag_days = 0
    pub_d = _parse_flexible_date(pub_date_raw)
    txn_d = _parse_flexible_date(txn_date_raw)
    if pub_d and txn_d:
        lag_days = (pub_d - txn_d).days

    lag_str = f" <i>(filed {lag_days}d later)</i>" if lag_days > 0 else ""

    # Display dates in ISO for consistency
    txn_display = txn_d.isoformat() if txn_d else txn_date_raw
    pub_display = pub_d.isoformat() if pub_d else pub_date_raw

    # ── Calculate Net Flow Early (for Filters & Header) ───────────
    buy_shares  = 0
    sell_shares = 0

    for t in trades:
        tx     = t["transaction_type"]
        shares = t["shares_transacted"]
        if tx == "ACQUISITION" and shares:
            buy_shares += shares
        elif tx == "DISPOSAL" and shares:
            sell_shares += shares
    
    net_flow = buy_shares - sell_shares

    # ── Price Context & Stagnant Filter ──────────────────────────
    trade_date_iso = txn_d.isoformat() if txn_d else None
    price_ctx = None
    price_move_pct = 0.0

    if trade_date_iso:
        try:
            price_ctx = get_price_context(stock_code, trade_date_iso)
            if price_ctx:
                # Attempt to extract move percentage. 
                # Adjust key ('change_pct', 'move_pct', etc.) based on your price_context.py return structure
                price_move_pct = price_ctx.get('change_pct', 0.0) if isinstance(price_ctx, dict) else getattr(price_ctx, 'change_pct', 0.0)
        except Exception:
            log.exception("Price context failed for %s", stock_code)

    # FILTER: If price is stagnant (0.00%) AND insiders are selling, SUPPRESS alert
    if price_move_pct == 0.0 and net_flow < 0:
        log.info(f"⚠️ Suppressed {stock_code}: Stagnant price (0.00%) + Insider Selling")
        return None

    # ── Score-based header tier (WITH BEARISH OVERRIDE) ───────────
    # Massive selling trumps volume score
    if net_flow < -HEAVY_SELLING_THRESHOLD:
        score_emoji = "️"
        score_label = "INSIDER DUMPING"
        display_score = f"{signal_score} (Bearish)"
    elif signal_score >= MEGA_SIGNAL_SCORE:
        score_emoji = "🔥🔥"
        score_label = "MEGA SIGNAL"
        display_score = signal_score
    elif signal_score >= STRONG_SIGNAL_SCORE:
        score_emoji = "🔥"
        score_label = "Strong signal"
        display_score = signal_score
    else:
        score_emoji = ""
        score_label = "Signal"
        display_score = signal_score

    # ── Header ───────────────────────────────────────────
    lines = [
        f"{score_emoji} <b>{html.escape(company)} ({stock_code})</b>",
        f"📊 {score_label} — Score: <b>{display_score}</b>",
        f"📅 Trade: {txn_display}{lag_str}",
        f"📢 Filed: {pub_display}",
        "",
    ]

    # ─ Streak warning ───────────────────────────────────
    if streak >= 5:
        lines.append(f"🔥 <b>ACTIVE {streak} CONSECUTIVE TRADING DAYS</b>")
        lines.append("")
    elif streak >= 3:
        lines.append(f"⚡ Active {streak} trading days in a row")
        lines.append("")

    # ── Trades ───────────────────────────────────────────
    lines.append("📋 <b>Latest Insider Trades</b>")

    for t in trades:
        tx      = t["transaction_type"]
        person  = html.escape(t["person_name"] or "Unknown")
        shares  = t["shares_transacted"]
        price   = t["consideration"]
        nature  = t["nature_of_interest"] or ""
        subcat  = t.get("subcategory", "")

        icon  = "✅" if tx == "ACQUISITION" else ""
        s_str = f"{shares:,}" if shares else "?"
        p_str = price if price else "-"
        verb  = "Bought" if tx == "ACQUISITION" else "Sold"

        # Show filing type tag (D=Director, S=Substantial Shareholder)
        tag = "D" if subcat == "DIRECTOR_S219" else "S"

        lines.append(
            f"  {icon} {person} <code>[{tag}]</code>\n"
            f"     {verb} <code>{s_str}</code> shares @ {p_str} "
            f"<i>({nature})</i>"
        )

    # ── Net summary ──────────────────────────────────────
    lines.append("")
    if net_flow > 0:
        lines.append(f" Net: <b>+{net_flow:,} shares BOUGHT</b> 🚀")
    elif net_flow < -1000000:  # More than 1M sold
        lines.append(f"📉 Net: <b>{net_flow:,} shares SOLD</b> ️ <b>HEAVY SELLING</b>")
    elif net_flow < 0:
        lines.append(f"📉 Net: <b>{net_flow:,} shares SOLD</b> ️")
    else:
        lines.append(f"➡️ Net: <b>Neutral</b>")

    # ── Price context (Display) ──────────────────────────────
    if price_ctx:
        try:
            price_block = format_price_block(price_ctx)
            if price_block:
                lines.append(price_block)
        except Exception:
            log.exception("Price block format failed for %s", stock_code)

    # ── Ownership snapshot ───────────────────────────────
    if ownership:
        lines.append("")
        lines.append("👥 <b>Current Insider Ownership</b>")
        total_pct = 0.0
        # Show top 5 largest holders only to avoid huge messages
        for o in ownership[:5]:
            person = html.escape(o["person_name"] or "?")[:40]
            d_pct  = o["direct_pct"]
            i_pct  = o["indirect_pct"]
            t_pct  = o["total_pct"]
            total_pct += t_pct

            pct_str = f"{t_pct:.2f}%"
            detail  = []
            if d_pct:
                detail.append(f"D:{d_pct:.2f}%")
            if i_pct:
                detail.append(f"I:{i_pct:.2f}%")
            detail_str = f" ({', '.join(detail)})" if detail else ""

            lines.append(f"  • {person}: <code>{pct_str}</code>{detail_str}")

        # If more than 5, note that
        if len(ownership) > 5:
            lines.append(f"  <i>+ {len(ownership) - 5} more holders...</i>")

    # ── Footer ───────────────────────────────────────────
    lines += [
        "",
        f"🔗 <a href='https://www.bursamalaysia.com/trade/trading_resources/"
        f"listed_securities/listed_securities_search?"
        f"stock_code={stock_code}'>Bursa: {stock_code}</a>",
    ]

    return "\n".join(lines)


# ─── Telegram ──────────────────────────────────────────────────────

def send_telegram_message(token: str, chat_id: str, text: str) -> bool:
    url = f"{TELEGRAM_API_BASE}/bot{token}/sendMessage"
    payload = {
        "chat_id":                  chat_id,
        "text":                     text,
        "parse_mode":               "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
    except requests.RequestException:
        log.exception("Network error sending Telegram message")
        return False

    if resp.status_code != 200:
        log.error("Telegram HTTP %s: %s", resp.status_code, resp.text)
        return False

    data = resp.json()
    if not data.get("ok"):
        log.error("Telegram API error: %s", data.get("description"))
        return False

    return True


# ─── Main ──────────────────────────────────────────────────────────

def main() -> None:
    token   = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        log.error("TELEGRAM_BOT_TOKEN and/or TELEGRAM_CHAT_ID not set.")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    ensure_delivered_column(conn)

    trades = get_undelivered(conn)

    if not trades:
        log.info("No undelivered insider alerts (alert_ready = 1).")
        conn.close()
        return

    # ── Deduplicate: prefer S219 (has price) over S138 ────
    # for the same (stock, person, date) transaction.
    trades_sorted = sorted(
        trades,
        key=lambda t: (
            t["stock_code"],
            t["published_date"],
            0 if t["subcategory"] == "DIRECTOR_S219" else 1,  # S219 first
        )
    )

    by_stock  = defaultdict(list)
    seen_keys = set()  # (stock_code, person_name, published_date)
    dates_by_stock = defaultdict(set)  # track all dates per stock for dedup marking

    for t in trades_sorted:
        key = (t["stock_code"], t["person_name"], t["published_date"])
        dates_by_stock[t["stock_code"]].add(t["published_date"])

        if key in seen_keys:
            continue
        seen_keys.add(key)
        by_stock[t["stock_code"]].append(t)

    log.info(
        "Found %d undelivered trades → %d unique after dedup → %d stocks",
        len(trades), sum(len(v) for v in by_stock.values()), len(by_stock)
    )

    for stock_code, stock_trades in by_stock.items():
        try:
            # ── DATE SANITY CHECK ───────────────────────────────
            # Prevent alerts for future dates (e.g., 2026 glitches)
            txn_date_raw = stock_trades[0].get("date_of_change") or stock_trades[0]["published_date"]
            txn_d = _parse_flexible_date(txn_date_raw)
            
            if txn_d and txn_d > date.today() + timedelta(days=FUTURE_DATE_BUFFER_DAYS):
                log.warning(f"⚠️ Skipping {stock_code}: Future date detected ({txn_d}). Marking as delivered.")
                mark_delivered_all_for_stock(
                    conn,
                    stock_code,
                    list(dates_by_stock[stock_code]),
                )
                conn.commit()
                continue

            ownership = get_ownership_snapshot(conn, stock_code)
            streak    = get_activity_streak(conn, stock_code)
            
            message = format_stock_alert(
                stock_code, stock_trades, ownership, streak
            )

            # If message is None, it was suppressed by filters (e.g., stagnant price)
            if message is None:
                log.info(f"⚠️ Suppressed alert for {stock_code} due to filters. Marking as delivered.")
                mark_delivered_all_for_stock(
                    conn,
                    stock_code,
                    list(dates_by_stock[stock_code]),
                )
                conn.commit()
                continue

            # ── i_report FILTER ─────────────────────────────────
            # Portfolio stocks always alert (you own them — you want
            # to know about every insider change). Everything else
            # must earn an actionable i_report decision. Fail-open:
            # if analysis is unavailable, the alert still goes out.
            in_portfolio = stock_code.upper() in PORTFOLIO

            analysis = None
            if IREPORT_FILTER_ENABLED:
                analysis = analyze_stock(stock_code)

            if in_portfolio:
                message = "📌 <b>HOLDING</b> — you own this stock\n" + message
            elif IREPORT_FILTER_ENABLED and analysis is not None \
                    and analysis["decision"] not in ALLOWED_DECISIONS:
                log.info(
                    "🔇 i_report filtered %s: score=%s decision=%s. Marking delivered.",
                    stock_code, analysis["score"], analysis["decision"],
                )
                mark_delivered_all_for_stock(
                    conn,
                    stock_code,
                    list(dates_by_stock[stock_code]),
                )
                conn.commit()
                continue

            if analysis is not None:
                message += (
                    f"\n🧠 <b>i_report:</b> {analysis['decision']} "
                    f"(score {analysis['score']}/100, "
                    f"confidence {analysis['confidence']}%)"
                )
            elif IREPORT_FILTER_ENABLED:
                message += "\n🧠 <b>i_report:</b> analysis unavailable (fail-open)"


            log.info(
                "Sending alert for %s (score=%d, %d trades shown, streak=%d)",
                stock_code, stock_trades[0].get('signal_score', 0), len(stock_trades), streak
            )

            # Atomic delivery
            conn.execute("BEGIN IMMEDIATE")
            try:
                success = send_telegram_message(token, chat_id, message)
                if success:
                    # Mark ALL affected rows delivered (including deduplicated ones)
                    mark_delivered_all_for_stock(
                        conn,
                        stock_code,
                        list(dates_by_stock[stock_code]),
                    )
                    conn.commit()
                    log.info("✅ Delivered alert for %s", stock_code)
                else:
                    conn.execute("ROLLBACK")
                    log.warning("❌ Failed to send alert for %s", stock_code)
            except Exception:
                conn.execute("ROLLBACK")
                raise

            time.sleep(0.5)

        except Exception:
            log.exception("Failed to process stock %s, continuing to next", stock_code)
            # Ensure we don't leave a transaction open
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            continue

    conn.close()


if __name__ == "__main__":
    main()
