cat > /opt/market/fx_signal/calendar_checker.py << 'PYEOF'
#!/usr/bin/env python3
"""
Economic news risk checker for FX signals.

Uses Finnhub's FREE /news endpoint (no paid plan required) and scans
for high-impact keywords affecting the currencies in a pair.

This is a proxy for a true economic calendar — it catches major event
coverage (FOMC, NFP, CPI, rate decisions) published in the news feed
within the look-ahead window.

Requires a free Finnhub API key:
    https://finnhub.io/register
    Add to /opt/market/fx_signal/.env:
    FINNHUB_API_KEY=your_key_here

If the key is not set, all functions return empty results gracefully.
"""

import logging
import os
from datetime import datetime, timezone, timedelta

import requests

from fx_config import CALENDAR_WINDOW_MINUTES, FX_PAIRS

log = logging.getLogger("calendar_checker")

FINNHUB_API_BASE = "https://finnhub.io/api/v1"
REQUEST_TIMEOUT  = 10

# High-impact keywords — news containing these near a signal = risk flag
HIGH_IMPACT_KEYWORDS = [
    "FOMC", "Federal Reserve", "Fed decision", "rate decision",
    "interest rate", "NFP", "nonfarm payroll", "non-farm payroll",
    "CPI", "inflation", "GDP", "ECB", "Bank of England", "BOE",
    "Bank of Japan", "BOJ", "Reserve Bank", "PMI", "unemployment",
    "retail sales", "trade balance", "central bank",
]

# Currency to search terms — used to filter news relevance to the pair
CURRENCY_TO_TERMS = {
    "USD": ["USD", "dollar", "Federal Reserve", "FOMC", "Fed"],
    "EUR": ["EUR", "euro", "ECB", "European Central Bank"],
    "GBP": ["GBP", "sterling", "pound", "Bank of England", "BOE"],
    "JPY": ["JPY", "yen", "Bank of Japan", "BOJ"],
    "AUD": ["AUD", "aussie", "Reserve Bank of Australia", "RBA"],
    "CAD": ["CAD", "loonie", "Bank of Canada", "BOC"],
    "CHF": ["CHF", "franc", "Swiss National Bank", "SNB"],
    "NZD": ["NZD", "kiwi", "Reserve Bank of New Zealand", "RBNZ"],
}


def _get_api_key() -> str | None:
    key = os.environ.get("FINNHUB_API_KEY")
    if not key:
        log.debug("FINNHUB_API_KEY not set — calendar checks disabled")
    return key


def _is_high_impact(headline: str) -> bool:
    """Returns True if the headline contains a high-impact keyword."""
    headline_lower = headline.lower()
    return any(kw.lower() in headline_lower for kw in HIGH_IMPACT_KEYWORDS)


def _is_relevant_to_pair(headline: str, pair: str) -> bool:
    """Returns True if the headline mentions currencies in the pair."""
    cfg = FX_PAIRS.get(pair, {})
    base  = cfg.get("base", "")
    quote = cfg.get("quote", "")

    base_terms  = CURRENCY_TO_TERMS.get(base,  [base])
    quote_terms = CURRENCY_TO_TERMS.get(quote, [quote])
    all_terms   = base_terms + quote_terms

    headline_lower = headline.lower()
    return any(term.lower() in headline_lower for term in all_terms)


def get_upcoming_events(
    pair: str,
    window_minutes: int = CALENDAR_WINDOW_MINUTES,
) -> list[dict]:
    """
    Returns a list of high-impact news items affecting the currencies
    in `pair` published within the last `window_minutes`.

    Uses Finnhub /news (free tier) — scans for high-impact keywords
    and filters to news relevant to the pair's currencies.

    Each event dict contains:
        event        : str  — news headline
        currency     : str  — pair (e.g. 'EURUSD')
        minutes_away : int  — minutes since published (negative = past)
        impact       : str  — always 'high' (filtered by keyword)

    Returns empty list if key not set, API fails, or no relevant news.
    """
    api_key = _get_api_key()
    if not api_key:
        return []

    cfg = FX_PAIRS.get(pair)
    if not cfg:
        return []

    try:
        response = requests.get(
            f"{FINNHUB_API_BASE}/news",
            params={
                "token":    api_key,
                "category": "forex",
            },
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        articles = response.json()
    except requests.RequestException:
        log.exception("Finnhub news request failed for %s", pair)
        return []
    except ValueError:
        log.error("Finnhub returned non-JSON response for %s", pair)
        return []

    if not isinstance(articles, list):
        log.warning("Unexpected Finnhub response format: %s", type(articles))
        return []

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=window_minutes)

    events = []
    seen_headlines = set()   # deduplicate

    for article in articles:
        headline = article.get("headline", "")
        if not headline or headline in seen_headlines:
            continue

        # Parse publish timestamp
        published_ts = article.get("datetime")
        if not published_ts:
            continue

        try:
            published_dt = datetime.fromtimestamp(published_ts, tz=timezone.utc)
        except (ValueError, TypeError, OSError):
            continue

        # Only recent articles within the window
        if published_dt < cutoff:
            continue

        # Must contain a high-impact keyword
        if not _is_high_impact(headline):
            continue

        # Must be relevant to this pair's currencies
        if not _is_relevant_to_pair(headline, pair):
            continue

        seen_headlines.add(headline)
        minutes_ago = int((now - published_dt).total_seconds() / 60)

        events.append({
            "event":        headline[:80] + ("..." if len(headline) > 80 else ""),
            "currency":     pair,
            "minutes_away": minutes_ago,   # minutes since published
            "impact":       "high",
        })

    # Sort by most recent first
    events.sort(key=lambda e: e["minutes_away"])
    return events


def summarise_news_risk(events: list[dict]) -> str:
    """
    Returns a one-word risk label based on the news event list.

    HIGH     : high-impact news published within last 15 min
    ELEVATED : high-impact news published within last 60 min
    CLEAR    : no high-impact relevant news in window
    """
    if not events:
        return "CLEAR"
    most_recent = min(e["minutes_away"] for e in events)
    if most_recent <= 15:
        return "HIGH"
    return "ELEVATED"
PYEOF
