#!/usr/bin/env python3
"""
Telegram delivery for FX signals — MARKET EXECUTION MODEL.

Reads undelivered signals and pushes them to Telegram with enriched
context: technical score, signal health, confidence %, live R:R,
drift, and news risk from the economic calendar.

Gates applied before delivery:
    1. Signal not expired   (expires_at)
    2. Adverse drift within tolerance  (STALE_SUPPRESS_PIPS)
    3. Live R:R >= MIN_LIVE_RR
    4. News risk check via calendar_checker

KEY FIXES vs previous version:
    - fast_info.get() replaced with attribute access (was always None)
    - Drift suppression now directional — favourable pullbacks no longer
      trigger ⛔ DO NOT EXECUTE
    - Live price fetched inside atomic block (as late as possible)
    - delivered_at timestamp recorded for pipeline lag auditing
    - Suppressed signals stay in queue for retry (not marked delivered)
    - Technical score, signal health, confidence added to message
"""

import html
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import yfinance as yf

from fx_config import (
    FX_PAIRS,
    MIN_BARS_FOR_STATS,
    MIN_LIVE_RR,
    RSI_OVERBOUGHT,
    RSI_OVERSOLD,
    STALE_SUPPRESS_PIPS,
    STALE_WARN_PIPS,
)

try:
    from calendar_checker import get_upcoming_events, summarise_news_risk
    CALENDAR_AVAILABLE = True
except ImportError:
    CALENDAR_AVAILABLE = False

PRICES_DB_PATH        = Path(__file__).parent / "prices.db"
TELEGRAM_API_BASE     = "https://api.telegram.org"
REQUEST_TIMEOUT_SECS  = 10

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("telegram_bot")


# ------------------------------------------------------------------
# DB queries
# ------------------------------------------------------------------

def get_undelivered_signals(conn: sqlite3.Connection) -> list:
    """
    Returns all signals that:
      - have not been delivered
      - have not expired
      - are not currently suppressed (suppressed=1 re-evaluates each run)
    Suppressed rows re-enter the queue automatically here.
    """
    rows = conn.execute(
        """
        SELECT id, pair, direction, entry, stop_loss, take_profit,
               atr_1h, rsi_1h, rationale, generated_at
        FROM signals
        WHERE delivered = 0
          AND (expires_at IS NULL OR expires_at > datetime('now'))
        ORDER BY id ASC
        """
    ).fetchall()
    return rows


def get_indicator_rows(
    conn: sqlite3.Connection, pair: str
) -> tuple:
    """Fetch latest 1h and 4h rows for scoring."""
    conn.row_factory = sqlite3.Row

    row_1h = conn.execute(
        """
        SELECT ema_fast, ema_slow, rsi, macd, macd_signal, atr, bar_time, close
        FROM price_signals
        WHERE pair = ? AND timeframe = '1h'
        ORDER BY bar_time DESC LIMIT 1
        """,
        (pair,),
    ).fetchone()

    row_4h = conn.execute(
        """
        SELECT ema_fast, ema_slow
        FROM price_signals
        WHERE pair = ? AND timeframe = '4h'
        ORDER BY bar_time DESC LIMIT 1
        """,
        (pair,),
    ).fetchone()

    return row_1h, row_4h


def get_historical_stats(
    conn: sqlite3.Connection, pair: str, direction: str
) -> dict | None:
    """
    Returns TP rate and median minutes-to-exit for the pair/direction
    IF there are enough resolved outcomes. Returns None otherwise.
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT outcome, minutes_to_exit
        FROM signal_outcomes
        WHERE pair = ?
          AND direction = ?
          AND outcome IS NOT NULL
          AND minutes_to_exit IS NOT NULL
        """,
        (pair, direction),
    ).fetchall()

    if len(rows) < MIN_BARS_FOR_STATS:
        return None

    tp_hits = sum(1 for r in rows if r["outcome"] == "TP_HIT")
    tp_rate = tp_hits / len(rows)

    minutes = sorted(r["minutes_to_exit"] for r in rows if r["outcome"] == "TP_HIT")
    if minutes:
        mid = len(minutes) // 2
        median_min = (
            minutes[mid]
            if len(minutes) % 2
            else (minutes[mid - 1] + minutes[mid]) / 2
        )
    else:
        median_min = None

    return {
        "sample_size":  len(rows),
        "tp_rate":      tp_rate,
        "median_minutes": median_min,
    }


# ------------------------------------------------------------------
# Live price
# ------------------------------------------------------------------

def get_live_price(yf_symbol: str) -> float | None:
    """
    Fetches the most recent price via yfinance fast_info.

    fast_info is a FastInfo OBJECT — it does NOT support .get().
    We use getattr() with a fallback chain.
    """
    try:
        ticker = yf.Ticker(yf_symbol)
        fi     = ticker.fast_info

        price = getattr(fi, "last_price", None)
        if price is None:
            price = getattr(fi, "previous_close", None)
            if price is not None:
                log.warning(
                    "%s: last_price unavailable, using previous_close=%.5f",
                    yf_symbol, price,
                )
        if price is None:
            log.warning("%s: no live price from fast_info", yf_symbol)
            return None

        return float(price)
    except Exception:
        log.exception("Failed to fetch live price for %s", yf_symbol)
        return None


# ------------------------------------------------------------------
# Scoring
# ------------------------------------------------------------------

def compute_technical_score(row_1h, row_4h, direction: str) -> int:
    """
    0–100 score derived entirely from existing indicator data.

    Component weights:
        EMA alignment 1h        20 pts
        EMA alignment 4h        20 pts
        MACD confirmation       15 pts
        RSI positioning         25 pts
        EMA separation/ATR      20 pts
    """
    if row_1h is None:
        return 0

    score       = 0
    ema_fast    = row_1h["ema_fast"]
    ema_slow    = row_1h["ema_slow"]
    rsi         = row_1h["rsi"]
    macd        = row_1h["macd"]
    macd_sig    = row_1h["macd_signal"]
    atr         = row_1h["atr"]

    # ── 1h EMA alignment (20 pts) ────────────────────────────────
    if direction == "BUY"  and ema_fast > ema_slow: score += 20
    if direction == "SELL" and ema_fast < ema_slow: score += 20

    # ── 4h EMA alignment (20 pts) ────────────────────────────────
    if row_4h:
        if direction == "BUY"  and row_4h["ema_fast"] > row_4h["ema_slow"]: score += 20
        if direction == "SELL" and row_4h["ema_fast"] < row_4h["ema_slow"]: score += 20

    # ── MACD (15 pts) ────────────────────────────────────────────
    if direction == "BUY"  and macd > macd_sig: score += 15
    if direction == "SELL" and macd < macd_sig: score += 15

    # ── RSI zone (25 pts) ────────────────────────────────────────
    if direction == "BUY":
        if   40 <= rsi <= 60: score += 25   # momentum building, not extended
        elif 30 <= rsi <  40: score += 15   # recovering from oversold — ok
        elif 60 <  rsi <  70: score += 10   # stretching — caution
        # rsi >= 70: 0 pts — overbought
    else:  # SELL
        if   40 <= rsi <= 60: score += 25
        elif 60 <  rsi <= 70: score += 15
        elif 30 <  rsi <  40: score += 10
        # rsi <= 30: 0 pts — oversold

    # ── EMA separation relative to ATR (20 pts) ──────────────────
    if atr and atr > 0:
        separation = abs(ema_fast - ema_slow) / atr
        if   separation >= 0.5: score += 20
        elif separation >= 0.3: score += 12
        elif separation >= 0.1: score += 6

    return min(score, 100)


def compute_signal_health(
    bar_age_minutes: float,
    drift_pips: float | None,
    live_rr: float | None,
    is_4h_aligned: bool,
) -> int:
    """
    0–100 pipeline quality score.
    Measures delivery conditions, independent of market direction.
    """
    score = 100

    # Bar freshness
    if   bar_age_minutes > 75: score -= 40
    elif bar_age_minutes > 60: score -= 25
    elif bar_age_minutes > 45: score -= 10

    # Adverse drift only — pullbacks improve the setup
    if drift_pips is not None:
        adverse = max(drift_pips, 0.0)
        if   adverse >= 15: score -= 35
        elif adverse >= 10: score -= 20
        elif adverse >= 5:  score -= 10

    # Live R:R quality
    if live_rr is not None:
        if   live_rr < 1.0: score -= 30
        elif live_rr < 1.5: score -= 15
        elif live_rr < 2.0: score -= 5

    # 4h alignment bonus
    if not is_4h_aligned:
        score -= 15

    return max(score, 0)


def compute_confidence(technical_score: int, signal_health: int) -> int:
    """Weighted blend — direction quality 65%, delivery quality 35%."""
    return round(technical_score * 0.65 + signal_health * 0.35)


# ------------------------------------------------------------------
# Live metrics / gating
# ------------------------------------------------------------------

def compute_live_metrics(
    pair: str,
    direction: str,
    entry: float,
    stop_loss: float,
    take_profit: float,
    live_price: float,
) -> tuple[float, float, bool, bool, bool]:
    """
    Returns (drift_pips, live_rr, is_warn, is_suppress, is_untradeable).

    drift_pips:
        Signed — positive = adverse (price moved against entry),
                 negative = favourable (price pulled back — better fill).
        Only POSITIVE drift triggers warnings/suppression.

    live_rr:
        Executable reward:risk from live_price to fixed SL/TP.
    """
    cfg      = FX_PAIRS[pair]
    pip_size = cfg["pip_size"]
    raw_diff = live_price - entry

    drift_pips = (raw_diff / pip_size) if direction == "BUY" else (-raw_diff / pip_size)

    if direction == "BUY":
        live_risk   = abs(live_price - stop_loss)
        live_reward = abs(take_profit - live_price)
    else:
        live_risk   = abs(stop_loss - live_price)
        live_reward = abs(live_price - take_profit)

    live_rr = (live_reward / live_risk) if live_risk else 0.0

    # Only adverse (positive) drift triggers suppression
    adverse_drift  = max(drift_pips, 0.0)
    is_warn        = adverse_drift >= STALE_WARN_PIPS
    is_suppress    = adverse_drift >= STALE_SUPPRESS_PIPS
    is_untradeable = live_rr < MIN_LIVE_RR

    return drift_pips, live_rr, is_warn, is_suppress, is_untradeable


# ------------------------------------------------------------------
# Message formatting
# ------------------------------------------------------------------

def format_message(
    row: tuple,
    live_price: float | None,
    drift_pips: float | None,
    live_rr: float | None,
    is_warn: bool,
    is_suppress: bool,
    is_untradeable: bool,
    technical_score: int,
    signal_health: int,
    confidence: int,
    bar_age_minutes: float,
    news_events: list,
    historical_stats: dict | None,
) -> str:
    (signal_id, pair, direction, entry, stop_loss, take_profit,
     atr_1h, rsi_1h, rationale, generated_at) = row

    # Reference R:R
    ref_risk   = abs(entry - stop_loss)
    ref_reward = abs(take_profit - entry)
    ref_rr     = ref_reward / ref_risk if ref_risk else 0.0

    emoji = "🟢" if direction == "BUY" else "🔴"

    # ── Action banner ─────────────────────────────────────────────
    if is_suppress or is_untradeable:
        action_banner = (
            f"⛔ <b>DO NOT EXECUTE — {direction} SIGNAL INVALIDATED</b>\n"
            f"<i>Price moved too far from reference or R:R collapsed "
            f"below {MIN_LIVE_RR}.</i>"
        )
    elif is_warn:
        action_banner = (
            f"⚠️ <b>{direction} SIGNAL — EXECUTE WITH CAUTION</b>\n"
            f"<i>Adverse drift detected. Check live R:R before entry.</i>"
        )
    else:
        action_banner = f"🚀 <b>EXECUTE: MARKET {direction} NOW</b>"

    # ── Scores ───────────────────────────────────────────────────
    score_lines = [
        f"Technical:     <code>{technical_score}/100</code>",
        f"Signal Health: <code>{signal_health}/100</code>",
    ]

    # ── Levels ───────────────────────────────────────────────────
    level_lines = [
        f"Entry (ref): <code>{entry:.5f}</code>",
    ]
    if live_price is not None:
        level_lines.append(f"Current:     <code>{live_price:.5f}</code>")
    level_lines += [
        f"TP:          <code>{take_profit:.5f}</code>  (fixed)",
        f"SL:          <code>{stop_loss:.5f}</code>  (fixed)",
        f"Ref R:R:     <code>{ref_rr:.2f}</code>",
    ]

    # ── Drift / live R:R ─────────────────────────────────────────
    drift_lines = []
    if live_price is not None and drift_pips is not None:
        if drift_pips > 0:
            drift_lines.append(
                f"Drift: <code>{drift_pips:.1f} pips ADVERSE</code>"
            )
        else:
            drift_lines.append(
                f"Drift: <code>{abs(drift_pips):.1f} pips FAVOURABLE ✅</code>"
            )

        if is_suppress:
            drift_lines.append(f"❌ Drift exceeds {STALE_SUPPRESS_PIPS} pip limit — setup broken")
        elif is_warn:
            drift_lines.append(f"⚠️ Drift exceeds {STALE_WARN_PIPS} pip warning threshold")
        else:
            drift_lines.append("✅ Price close to reference")

        if live_rr is not None:
            rr_icon = "✅" if live_rr >= MIN_LIVE_RR else "❌"
            drift_lines.append(
                f"Live R:R: <code>{live_rr:.2f}</code> {rr_icon}"
            )
    else:
        drift_lines.append(
            "<i>⚠️ Live price unavailable — using reference entry only.</i>"
        )

    # ── RSI / ATR ────────────────────────────────────────────────
    indicator_lines = [
        f"RSI(1h): <code>{rsi_1h:.1f}</code>  "
        f"ATR(1h): <code>{atr_1h:.5f}</code>",
        f"Bar age: <code>{bar_age_minutes:.0f} min</code>",
    ]

    # ── News risk ─────────────────────────────────────────────────
    news_lines = []
    if news_events:
        news_lines.append("📅 <b>Upcoming High-Impact Events:</b>")
        for ev in news_events:
            news_lines.append(
                f"  ⚠️ {ev['currency']} — {html.escape(ev['event'])} "
                f"in <code>{ev['minutes_away']} min</code>"
            )
        soonest = min(ev["minutes_away"] for ev in news_events)
        if soonest <= 15:
            news_lines.append("🚫 <b>Recommendation: WAIT — event imminent</b>")
        elif soonest <= 30:
            news_lines.append("⚠️ <b>Recommendation: WAIT or reduce size</b>")
        else:
            news_lines.append("ℹ️ Event approaching — monitor closely")
    else:
        news_lines.append("📅 News risk: <code>CLEAR</code> ✅")

    # ── Historical stats (only if enough data) ───────────────────
    stats_lines = []
    if historical_stats:
        tp_pct   = historical_stats["tp_rate"] * 100
        n        = historical_stats["sample_size"]
        med_min  = historical_stats.get("median_minutes")
        stats_lines.append(
            f"📊 Historical TP rate: <code>{tp_pct:.0f}%</code> "
            f"({n} trades)"
        )
        if med_min is not None:
            hours, mins = divmod(int(med_min), 60)
            time_str = f"{hours}h {mins}m" if hours else f"{mins}m"
            stats_lines.append(
                f"⏱ Median time to TP: <code>{time_str}</code>"
            )

    # ── Rationale ────────────────────────────────────────────────
    safe_rationale = html.escape(rationale) if rationale else ""

    # ── Assemble ─────────────────────────────────────────────────
    sections = [
        # Header
        [f"{emoji} <b>{direction} {pair}</b>  —  <b>{confidence}% confidence</b>"],
        [""],
        [action_banner],
        [""],
        score_lines,
        [""],
        level_lines,
        [""],
        drift_lines,
        [""],
        indicator_lines,
    ]

    if stats_lines:
        sections += [[""], stats_lines]

    sections += [
        [""],
        news_lines,
        [""],
        [f"<i>{safe_rationale}</i>"],
        [""],
        [
            f"Signal ID: <code>{signal_id}</code>  |  "
            f"Generated: <code>{generated_at}</code>"
        ],
    ]

    lines = []
    for section in sections:
        lines.extend(section)

    return "\n".join(lines)


# ------------------------------------------------------------------
# Telegram send
# ------------------------------------------------------------------

def send_telegram_message(token: str, chat_id: str, text: str) -> bool:
    url     = f"{TELEGRAM_API_BASE}/bot{token}/sendMessage"
    payload = {
        "chat_id":    chat_id,
        "text":       text,
        "parse_mode": "HTML",
    }
    try:
        response = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT_SECS)
    except requests.RequestException:
        log.exception("Network error calling Telegram API")
        return False

    if response.status_code != 200:
        log.error(
            "Telegram HTTP %s: %s", response.status_code, response.text[:200]
        )
        return False

    data = response.json()
    if not data.get("ok"):
        log.error("Telegram API error: %s", data.get("description"))
        return False

    return True


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def main() -> None:
    token   = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        log.error("TELEGRAM_BOT_TOKEN and/or TELEGRAM_CHAT_ID not set.")
        sys.exit(1)

    conn = sqlite3.connect(PRICES_DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")

    signals = get_undelivered_signals(conn)
    if not signals:
        log.info("No undelivered signals.")
        conn.close()
        return

    log.info("Found %d undelivered signal(s).", len(signals))

    for row in signals:
        (signal_id, pair, direction, entry,
         stop_loss, take_profit, atr_1h, rsi_1h,
         rationale, generated_at) = row

        yf_symbol = FX_PAIRS.get(pair, {}).get("yf_symbol")

        # ── Pre-score data ────────────────────────────────────────
        conn.row_factory = sqlite3.Row
        row_1h, row_4h = get_indicator_rows(conn, pair)

        # Bar age for health score
        bar_age_minutes = 0.0
        if row_1h and row_1h["bar_time"]:
            try:
                bt = datetime.fromisoformat(row_1h["bar_time"])
                if bt.tzinfo is None:
                    bt = bt.replace(tzinfo=timezone.utc)
                bar_age_minutes = (
                    datetime.now(timezone.utc) - bt
                ).total_seconds() / 60
            except (ValueError, TypeError):
                pass

        is_4h_aligned = (
            row_4h is not None
            and row_4h["ema_fast"] is not None
            and row_4h["ema_slow"] is not None
            and row_4h["ema_fast"] > row_4h["ema_slow"]
            if direction == "BUY"
            else (
                row_4h is not None
                and row_4h["ema_fast"] is not None
                and row_4h["ema_slow"] is not None
                and row_4h["ema_fast"] < row_4h["ema_slow"]
            )
        )

        # ── News risk ─────────────────────────────────────────────
        news_events = []
        if CALENDAR_AVAILABLE:
            try:
                news_events = get_upcoming_events(pair)
            except Exception:
                log.exception("Calendar check failed for %s, continuing", pair)

        # ── Historical stats ──────────────────────────────────────
        conn.row_factory = None
        historical_stats = get_historical_stats(conn, pair, direction)

        # ── Atomic block — live price + delivery ─────────────────
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Re-check delivered status inside lock
            status = conn.execute(
                "SELECT delivered FROM signals WHERE id = ?",
                (signal_id,),
            ).fetchone()
            if status is None or status[0] != 0:
                log.info("Signal id=%d already delivered, skipping", signal_id)
                conn.execute("ROLLBACK")
                continue

            # Fetch live price as late as possible — inside the lock
            live_price   = get_live_price(yf_symbol) if yf_symbol else None
            drift_pips   = None
            live_rr      = None
            is_warn      = False
            is_suppress  = False
            is_untradeable = False

            if live_price is not None:
                (drift_pips, live_rr,
                 is_warn, is_suppress, is_untradeable) = compute_live_metrics(
                    pair, direction, entry,
                    stop_loss, take_profit, live_price,
                )

            # Scores
            technical_score = compute_technical_score(row_1h, row_4h, direction)
            signal_health   = compute_signal_health(
                bar_age_minutes, drift_pips, live_rr, is_4h_aligned
            )
            confidence      = compute_confidence(technical_score, signal_health)

            # Gate logging
            if is_suppress:
                log.warning(
                    "%s %s id=%d: SUPPRESSED — adverse drift %.1f pips",
                    direction, pair, signal_id, drift_pips,
                )
            elif is_untradeable:
                log.warning(
                    "%s %s id=%d: SUPPRESSED — live R:R %.2f < %.2f",
                    direction, pair, signal_id, live_rr, MIN_LIVE_RR,
                )
            elif is_warn:
                log.warning(
                    "%s %s id=%d: WARNING — adverse drift %.1f pips",
                    direction, pair, signal_id, drift_pips,
                )

            # Build message
            text = format_message(
                row,
                live_price, drift_pips, live_rr,
                is_warn, is_suppress, is_untradeable,
                technical_score, signal_health, confidence,
                bar_age_minutes,
                news_events,
                historical_stats,
            )

            # Handle suppressed signals — keep in queue for retry
            if is_suppress or is_untradeable:
                conn.execute(
                    "UPDATE signals SET suppressed = 1 WHERE id = ?",
                    (signal_id,),
                )
                conn.commit()
                # Still send the ⛔ message so you know it fired
                send_telegram_message(token, chat_id, text)
                log.info(
                    "Signal id=%d marked suppressed — will re-evaluate next run",
                    signal_id,
                )
                continue

            # Clear suppressed flag if price has recovered
            conn.execute(
                "UPDATE signals SET suppressed = 0 WHERE id = ?",
                (signal_id,),
            )

            success = send_telegram_message(token, chat_id, text)
            if success:
                delivered_at = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    """
                    UPDATE signals
                    SET delivered = 1, delivered_at = ?, suppressed = 0
                    WHERE id = ?
                    """,
                    (delivered_at, signal_id),
                )
                conn.commit()
                log.info(
                    "Delivered signal id=%d (%s %s) confidence=%d%%",
                    signal_id, direction, pair, confidence,
                )
            else:
                conn.execute("ROLLBACK")
                log.warning(
                    "Send failed for signal id=%d, will retry next run",
                    signal_id,
                )

        except Exception:
            conn.execute("ROLLBACK")
            raise

        time.sleep(0.3)

    conn.close()


if __name__ == "__main__":
    main()
