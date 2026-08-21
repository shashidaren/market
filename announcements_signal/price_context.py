#!/usr/bin/env python3
"""
Price context enrichment for Bursa insider alerts.

Given a stock_code and filing/trade date, returns:
    - Price on trade date (or nearest trading day)
    - Current price
    - % move since trade
    - Human-readable verdict ("FRESH", "LATE", "DIP", etc.)

Used by telegram_bot.py to append price context to alerts so users
know how stale the signal is by the time they see it.

Uses yfinance. Bursa tickers map to Yahoo as `{4-digit-code}.KL`.
Handles ISO and Bursa human date formats gracefully.
"""

import logging
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import yfinance as yf

log = logging.getLogger("price_context")

# Cache to avoid hammering Yahoo on repeated lookups within same run
_price_cache: dict[str, dict] = {}


def _bursa_to_yahoo(stock_code: str) -> str:
    """
    Convert Bursa stock code to Yahoo Finance ticker.
    Bursa codes are 4-digit zero-padded + '.KL' suffix.
    Examples: '5352' → '5352.KL', '208' → '0208.KL'
    """
    return f"{str(stock_code).zfill(4)}.KL"


def _parse_date_flexible(raw: str) -> datetime | None:
    """
    Parse a date string in either ISO ('2026-08-13') or
    Bursa human format ('13 Aug 2026'). Returns None if unparseable.
    """
    if not raw:
        return None
    raw = str(raw).strip()
    for fmt in ("%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(raw, fmt)
        except (ValueError, TypeError):
            continue
    return None


def get_price_context(
    stock_code: str,
    trade_date: str,
    lookback_buffer: int = 5,
) -> dict | None:
    """
    Returns price context for a stock relative to a trade/filing date.

    Args:
        stock_code:      Bursa 4-digit code (e.g. '5352')
        trade_date:      Date string in ISO or Bursa human format
        lookback_buffer: Extra days to look back (handles weekends/holidays)

    Returns:
        dict with keys:
            - ticker            : Yahoo symbol used
            - price_at_trade    : Close price on/near trade_date
            - current_price     : Most recent close
            - pct_move          : % change from trade to now
            - verdict           : Short label ("FRESH", "LATE", etc.)
            - verdict_emoji     : Traffic-light emoji
        Returns None if data unavailable.
    """
    ticker = _bursa_to_yahoo(stock_code)
    cache_key = f"{ticker}:{trade_date}"

    if cache_key in _price_cache:
        return _price_cache[cache_key]

    trade_dt = _parse_date_flexible(trade_date)
    if not trade_dt:
        log.warning("Invalid trade_date: %s", trade_date)
        return None

    # Fetch a window: buffer days before trade_date → today
    start = trade_dt - timedelta(days=lookback_buffer)
    end   = datetime.now() + timedelta(days=1)

    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
        )
    except Exception:
        log.exception("yfinance failed for %s", ticker)
        return None

    if hist.empty or len(hist) < 2:
        log.warning("No price data for %s around %s", ticker, trade_date)
        return None

    # Find price on trade_date, or nearest trading day AT OR AFTER
    trade_target = trade_dt.strftime("%Y-%m-%d")
    on_or_after = hist[hist.index >= trade_target]

    if on_or_after.empty:
        # Trade date is in the future / no bars → fall back to first available
        price_at_trade = float(hist['Close'].iloc[0])
    else:
        price_at_trade = float(on_or_after['Close'].iloc[0])

    current_price = float(hist['Close'].iloc[-1])

    if price_at_trade <= 0:
        return None

    pct_move = ((current_price - price_at_trade) / price_at_trade) * 100

    verdict, emoji = _classify_move(pct_move)

    result = {
        "ticker":         ticker,
        "price_at_trade": price_at_trade,
        "current_price":  current_price,
        "pct_move":       round(pct_move, 2),
        "verdict":        verdict,
        "verdict_emoji":  emoji,
    }
    _price_cache[cache_key] = result
    return result


def _classify_move(pct: float) -> tuple[str, str]:
    """
    Turn a % move into a trader-friendly verdict.
    Thresholds tuned for Malaysian small/mid-caps.
    """
    if pct >= 10:
        return ("VERY LATE — big move already", "🔴")
    if pct >= 5:
        return (f"LATE — up {pct:.1f}% since trade", "🔴")
    if pct >= 2:
        return (f"PARTIAL — up {pct:.1f}%, some edge left", "🟠")
    if pct >= -2:
        return ("FRESH — price barely moved", "🟢")
    if pct >= -5:
        return (f"SOFT — down {abs(pct):.1f}%, watch reversal", "🟡")
    return (f"DIP — down {abs(pct):.1f}% since trade (contrarian?)", "🔵")


def format_price_block(ctx: dict | None) -> str:
    """
    Turn a price context dict into a Telegram HTML block.
    Returns empty string if ctx is None (gracefully hides on failure).
    """
    if not ctx:
        return ""

    return (
        "\n💰 <b>Price Context</b>\n"
        f"  • At trade: RM<code>{ctx['price_at_trade']:.3f}</code>\n"
        f"  • Now:      RM<code>{ctx['current_price']:.3f}</code>\n"
        f"  • Move:     <b>{ctx['pct_move']:+.2f}%</b>\n"
        f"  • {ctx['verdict_emoji']} <i>{ctx['verdict']}</i>"
    )


if __name__ == "__main__":
    # Quick manual test
    logging.basicConfig(level=logging.INFO)
    test_cases = [
        ("5352", "2026-08-13"),
        ("4677", "2026-08-10"),
        ("7595", "2026-08-15"),
        ("8419", "2026-08-05"),
        ("5352", "13 Aug 2026"),  # Bursa human format test
    ]
    for code, dt in test_cases:
        ctx = get_price_context(code, dt)
        print(f"\n=== {code} (trade date {dt}) ===")
        if ctx:
            print(format_price_block(ctx))
        else:
            print("  No data")
