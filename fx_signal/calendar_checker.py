#!/usr/bin/env python3
"""
Economic news risk checker for FX signals.

Uses Finnhub FREE /news endpoint (no paid plan required) and scans
for high-impact keywords affecting the currencies in a pair.

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

HIGH_IMPACT_KEYWORDS = [
    "FOMC", "Federal Reserve", "Fed decision", "rate decision",
    "interest rate", "NFP", "nonfarm payroll", "non-farm payroll",
    "CPI", "inflation", "GDP", "ECB", "Bank of England", "BOE",
    "Bank of Japan", "BOJ", "Reserve Bank", "PMI", "unemployment",
    "retail sales", "trade balance", "central bank",
]

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
    headline_lower = headline.lower()
    return any(kw.lower() in headline_lower for kw in HIGH_IMPACT_KEYWORDS)


def _is_relevant_to_pair(headline: str, pair: str) -> bool:
    cfg = FX_PAIRS.get(pair, {})
    base  = cfg.get("base", "")
    quote = cfg.get("quote", "")
    base_terms  = CURRENCY_TO_TERMS.get(base,  [base])
    quote_terms = CURRENCY_TO_TERMS.get(quote, [quote])
    all_terms   = base_terms + quote_terms
    headline_lower = headline.lower()
    return any(term.lower() in headline_lower for term in all_terms)


def get_recent_events(
    pair: str,
    window_minutes: int = CALENDAR_WINDOW_MINUTES,
) -> list[dict]:
    """
    Returns high-impact news items affecting the currencies in `pair`
    published within the last `window_minutes`.

    NOTE: this is a LOOK-BACK over already-published news — the free
    Finnhub tier has no forward-looking economic-calendar endpoint. The
    signal bot uses it as "volatility window after a release", NOT as an
    upcoming-events alarm. (The telegram message was previously worded as
    'event imminent', which was backwards — fixed.)

    Each event dict contains:
        event        : str  — news headline (truncated to 80 chars)
        currency     : str  — pair (e.g. EURUSD)
        minutes_away : int  — minutes since published (positive = past)
        impact       : str  — always high (keyword filtered)
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

    now    = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=window_minutes)

    events       = []
    seen         = set()

    for article in articles:
        headline = article.get("headline", "")
        if not headline or headline in seen:
            continue

        published_ts = article.get("datetime")
        if not published_ts:
            continue

        try:
            published_dt = datetime.fromtimestamp(published_ts, tz=timezone.utc)
        except (ValueError, TypeError, OSError):
            continue

        if published_dt < cutoff:
            continue

        if not _is_high_impact(headline):
            continue

        if not _is_relevant_to_pair(headline, pair):
            continue

        seen.add(headline)
        minutes_ago = int((now - published_dt).total_seconds() / 60)

        events.append({
            "event":        headline[:80] + ("..." if len(headline) > 80 else ""),
            "currency":     pair,
            "minutes_away": minutes_ago,
            "impact":       "high",
        })

    events.sort(key=lambda e: e["minutes_away"])
    return events


def summarise_news_risk(events: list[dict]) -> str:
    """
    HIGH     : high-impact news within last 15 min
    ELEVATED : high-impact news within last 60 min
    CLEAR    : nothing found
    """
    if not events:
        return "CLEAR"
    most_recent = min(e["minutes_away"] for e in events)
    if most_recent <= 15:
        return "HIGH"
    return "ELEVATED"
