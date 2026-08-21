#!/usr/bin/env python3
"""
Scores unscored rows in raw_headlines and writes results to
scored_headlines. Run hourly via cron, after the collector.

    */60 * * * * cd /opt/market && /usr/bin/python3 scorer.py >> /var/log/webscrap-scorer.log 2>&1

First run will download the FinBERT model (~440MB) from Hugging Face -
this needs huggingface.co reachable. After that it's fully offline.
"""

import logging
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from rapidfuzz import fuzz

from tickers_config import BURSA_TICKERS, MACRO_KEYWORDS

DB_PATH = Path(__file__).parent / "news.db"
BATCH_LIMIT = 200
CLUSTER_FUZZY_THRESHOLD = 80
CLUSTER_WINDOW_HOURS = 48

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("scorer")


def init_scored_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scored_headlines (
            id             TEXT PRIMARY KEY,
            relevance      TEXT,
            sector         TEXT,
            tickers        TEXT,
            sentiment      REAL,
            impact_horizon TEXT,
            confidence     REAL,
            cluster_key    TEXT,
            rationale      TEXT,
            scored_at      TEXT,
            FOREIGN KEY(id) REFERENCES raw_headlines(id)
        )
        """
    )
    conn.commit()


def _compile_patterns(lookup: dict) -> list:
    """Builds (compiled_regex, value) pairs with word-boundary matching,
    so short aliases/keywords (e.g. 'Fed', 'TM', 'MISC') can't match as
    substrings inside unrelated words (e.g. 'Federal', 'Estimate')."""
    patterns = []
    for key, value in lookup.items():
        pattern = re.compile(r"\b" + re.escape(key) + r"\b", re.IGNORECASE)
        patterns.append((pattern, value))
    return patterns


_TICKER_PATTERNS = _compile_patterns(BURSA_TICKERS)
_MACRO_PATTERNS = _compile_patterns(MACRO_KEYWORDS)


def classify_relevance_and_sector(text: str):
    """Rule-based relevance/sector/ticker detection.

    Checks ticker aliases first (more specific), falls back to macro
    keywords, defaults to noise. Word-boundary matched to avoid false
    positives like 'Fed' matching inside 'Federal'. Returns
    (relevance, sector, tickers) where tickers are canonical names,
    deduplicated even if multiple aliases for the same company matched.
    """
    matched_tickers = {}  # canonical_name -> sector
    for pattern, (canonical, sector) in _TICKER_PATTERNS:
        if pattern.search(text):
            matched_tickers[canonical] = sector

    if matched_tickers:
        sector = next(iter(matched_tickers.values()))
        return "malaysia_direct", sector, list(matched_tickers.keys())

    for pattern, sector in _MACRO_PATTERNS:
        if pattern.search(text):
            return "macro_indirect", sector, []

    return "noise", None, []


_sentiment_pipeline = None


def get_sentiment_pipeline():
    """Lazy-load FinBERT so rule-only testing doesn't require torch."""
    global _sentiment_pipeline
    if _sentiment_pipeline is None:
        log.info("Loading FinBERT model (first run downloads ~440MB)...")
        from transformers import pipeline
        _sentiment_pipeline = pipeline(
            "sentiment-analysis", model="ProsusAI/finbert"
        )
        log.info("FinBERT loaded.")
    return _sentiment_pipeline


def score_sentiment(text: str):
    """Returns (sentiment_float, confidence_float)."""
    finbert = get_sentiment_pipeline()
    result = finbert(text[:512])[0]
    label_map = {"positive": 1, "negative": -1, "neutral": 0}
    sentiment = label_map[result["label"]] * result["score"]
    return round(sentiment, 3), round(result["score"], 3)


def slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len]


def get_cluster_key(title: str, tickers: list, recent: list) -> str:
    """recent: list of (title, tickers_str, cluster_key) from already-scored
    recent rows. Two headlines cluster together if either their titles are
    fuzzy-similar, or they share at least one matched ticker (a much
    stronger signal than title wording, since wire coverage of the same
    event is often phrased completely differently across outlets)."""
    ticker_set = set(tickers)
    for existing_title, existing_tickers_str, existing_key in recent:
        if ticker_set and existing_tickers_str:
            existing_set = set(existing_tickers_str.split(","))
            if ticker_set & existing_set:
                return existing_key
        if fuzz.token_sort_ratio(title, existing_title) > CLUSTER_FUZZY_THRESHOLD:
            return existing_key
    return slugify(title)


def get_impact_horizon(relevance: str, published_at: str | None) -> str:
    if not published_at:
        return "background"
    try:
        pub_time = datetime.fromisoformat(published_at)
        if pub_time.tzinfo is None:
            pub_time = pub_time.replace(tzinfo=timezone.utc)
    except ValueError:
        return "background"

    age_hours = (datetime.now(timezone.utc) - pub_time).total_seconds() / 3600

    if relevance == "malaysia_direct" and age_hours < 1:
        return "immediate"
    if relevance == "macro_indirect" and age_hours < 24:
        return "short_term"
    if relevance == "malaysia_direct" and age_hours < 24:
        return "short_term"
    return "background"


def build_rationale(sector, sentiment, relevance) -> str:
    if relevance == "noise":
        return "No plausible trading relevance identified."
    sentiment_label = "positive" if sentiment > 0.15 else "negative" if sentiment < -0.15 else "neutral"
    sector_label = sector or "Macro"
    return f"{sector_label} headline, {sentiment_label} sentiment ({sentiment:+.2f})."


def fetch_recent_context(conn: sqlite3.Connection) -> list:
    """Recent scored headlines' (title, tickers, cluster_key), for
    clustering new ones. Windowed to CLUSTER_WINDOW_HOURS so an old story
    about the same company doesn't wrongly absorb a new, unrelated one."""
    rows = conn.execute(
        """
        SELECT r.title, s.tickers, s.cluster_key
        FROM scored_headlines s
        JOIN raw_headlines r ON r.id = s.id
        WHERE s.scored_at > datetime('now', ?)
        """,
        (f"-{CLUSTER_WINDOW_HOURS} hours",),
    ).fetchall()
    return [(title, tickers, key) for title, tickers, key in rows]


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    init_scored_table(conn)

    unscored = conn.execute(
        """
        SELECT id, title, snippet, published_at
        FROM raw_headlines
        WHERE scored = 0
        LIMIT ?
        """,
        (BATCH_LIMIT,),
    ).fetchall()

    if not unscored:
        log.info("No unscored headlines. Nothing to do.")
        conn.close()
        return

    log.info("Scoring %d unscored headlines...", len(unscored))

    recent_context = fetch_recent_context(conn)
    now = datetime.now(timezone.utc).isoformat()
    unmatched_seen = set()

    for row_id, title, snippet, published_at in unscored:
        text = f"{title} {snippet or ''}"

        relevance, sector, tickers = classify_relevance_and_sector(text)
        sentiment, confidence = score_sentiment(text)
        cluster_key = get_cluster_key(title, tickers, recent_context)
        recent_context.append((title, ",".join(tickers), cluster_key))
        impact_horizon = get_impact_horizon(relevance, published_at)
        rationale = build_rationale(sector, sentiment, relevance)

        conn.execute(
            """
            INSERT OR REPLACE INTO scored_headlines
                (id, relevance, sector, tickers, sentiment, impact_horizon,
                 confidence, cluster_key, rationale, scored_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row_id, relevance, sector, ",".join(tickers), sentiment,
                impact_horizon, confidence, cluster_key, rationale, now,
            ),
        )
        conn.execute(
            "UPDATE raw_headlines SET scored = 1 WHERE id = ?", (row_id,)
        )

        # Track headlines that hit "noise" but contain capitalized
        # multi-word phrases (potential missed company names), so the
        # ticker list can be expanded over time.
        if relevance == "noise":
            candidates = re.findall(r"\b[A-Z][a-zA-Z]+(?:\s[A-Z][a-zA-Z]+)+\b", title)
            unmatched_seen.update(candidates)

    conn.commit()

    counts = conn.execute(
        "SELECT relevance, COUNT(*) FROM scored_headlines GROUP BY relevance"
    ).fetchall()
    log.info("Scoring complete. Totals by relevance: %s", dict(counts))

    if unmatched_seen:
        sample = list(unmatched_seen)[:15]
        log.info(
            "Possible unmatched entities in noise-tagged headlines (review "
            "for BURSA_TICKERS additions): %s", sample
        )

    conn.close()


if __name__ == "__main__":
    main()
