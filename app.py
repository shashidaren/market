#!/usr/bin/env python3
"""
Market news dashboard - Flask app serving raw collected headlines from
news.db. Run alongside the collector cron job.

    python3 app.py
    -> http://192.168.0.141:5000

This is Stage-4-lite: shows raw collected headlines only, since Stage 2
(FinBERT scoring) hasn't been built yet. Once scoring exists, this gets
extended with relevance/sentiment/sector columns and filters.
"""

import sqlite3
from pathlib import Path

from flask import Flask, jsonify, render_template

DB_PATH = Path(__file__).parent / "news.db"

app = Flask(__name__)


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/headlines")
def api_headlines():
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT r.source, r.title, r.url, r.published_at, r.collected_at,
               s.relevance, s.sector, s.tickers, s.sentiment,
               s.impact_horizon, s.confidence, s.cluster_key, s.rationale
        FROM raw_headlines r
        LEFT JOIN scored_headlines s ON s.id = r.id
        ORDER BY COALESCE(r.published_at, r.collected_at) DESC
        LIMIT 300
        """
    ).fetchall()
    conn.close()

    # Collapse duplicate cluster_key rows into one entry with a source
    # count, so wire-service duplicates (e.g. Maybank story picked up
    # by two outlets) show as a single card, matching the dashboard design.
    seen_clusters = {}
    result = []
    for row in rows:
        d = dict(row)
        key = d.get("cluster_key")
        if key and key in seen_clusters:
            seen_clusters[key]["source_count"] += 1
            continue
        d["source_count"] = 1
        if key:
            seen_clusters[key] = d
        result.append(d)

    return jsonify(result)

@app.route("/api/sectors")
def api_sectors():
    conn = get_conn()
    rows = conn.execute(
        "SELECT DISTINCT sector FROM scored_headlines "
        "WHERE sector IS NOT NULL AND sector != '' "
        "ORDER BY sector"
    ).fetchall()
    conn.close()
    return jsonify([r[0] for r in rows])


@app.route("/api/stats")
def api_stats():
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) FROM raw_headlines").fetchone()[0]
    by_source = conn.execute(
        "SELECT source, COUNT(*) as count FROM raw_headlines GROUP BY source ORDER BY count DESC"
    ).fetchall()
    last_collected = conn.execute(
        "SELECT MAX(collected_at) FROM raw_headlines"
    ).fetchone()[0]
    last_scored = conn.execute(
        "SELECT MAX(scored_at) FROM scored_headlines"
    ).fetchone()[0]

    relevance_counts = dict(conn.execute(
        "SELECT relevance, COUNT(*) FROM scored_headlines GROUP BY relevance"
    ).fetchall())

    avg_sentiment_row = conn.execute(
        "SELECT AVG(sentiment) FROM scored_headlines WHERE relevance != 'noise'"
    ).fetchone()
    avg_sentiment = round(avg_sentiment_row[0], 3) if avg_sentiment_row[0] is not None else None

    conn.close()
    return jsonify(
        {
            "total": total,
            "by_source": [dict(row) for row in by_source],
            "last_collected": last_collected,
            "last_scored": last_scored,
            "malaysia_direct_count": relevance_counts.get("malaysia_direct", 0),
            "macro_indirect_count": relevance_counts.get("macro_indirect", 0),
            "noise_count": relevance_counts.get("noise", 0),
            "avg_sentiment": avg_sentiment,
        }
    )



# ═══════════════════════════════════════════════════════════════
# INSIDER SIGNAL ROUTES
# ═══════════════════════════════════════════════════════════════

@app.route("/insider")
def insider_page():
    return render_template("insider.html")


@app.route("/api/insider/signals")
def api_insider_signals():
    """
    Top active insider signals — one row per stock, sorted by score.
    Powers the main signal table.
    """
    conn = get_conn()
    rows = conn.execute("""
        SELECT
            bid.stock_code,
            bid.company_name,
            MAX(bid.signal_score) AS score,
            MAX(bid.published_date) AS last_date,
            COUNT(DISTINCT bid.published_date) AS active_days,
            COUNT(DISTINCT bid.ann_id) AS n_filings,
            SUM(CASE WHEN bid.transaction_type='ACQUISITION'
                     AND ba.subcategory='DIRECTOR_S219'
                     THEN COALESCE(bid.shares_transacted,0) ELSE 0 END) AS bought,
            SUM(CASE WHEN bid.transaction_type='DISPOSAL'
                     AND ba.subcategory='DIRECTOR_S219'
                     THEN COALESCE(bid.shares_transacted,0) ELSE 0 END) AS sold
        FROM bursa_insider_details bid
        JOIN bursa_announcements ba ON bid.ann_id = ba.ann_id
        WHERE bid.signal_score > 0
        AND bid.published_date >= date('now', '-7 days')
        AND ba.subcategory IN ('DIRECTOR_S219','SUBSTANTIAL_S138')
        GROUP BY bid.stock_code
        ORDER BY score DESC, last_date DESC
        LIMIT 100
    """).fetchall()

    result = []
    for r in rows:
        d = dict(r)
        d["net_shares"] = (d["bought"] or 0) - (d["sold"] or 0)
        result.append(d)
    conn.close()
    return jsonify(result)


@app.route("/api/insider/stock/<stock_code>")
def api_insider_stock(stock_code):
    """
    Detail view for one stock — full transaction history
    and current ownership snapshot.
    """
    conn = get_conn()

    # ── Transactions ─────────────────────────────────────
    trades = conn.execute("""
        SELECT
            bid.published_date,
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
            ba.url,
            bid.ann_id
        FROM bursa_insider_details bid
        JOIN bursa_announcements ba ON bid.ann_id = ba.ann_id
        WHERE bid.stock_code = ?
        AND ba.subcategory IN ('DIRECTOR_S219','SUBSTANTIAL_S138')
        AND bid.transaction_type IS NOT NULL
        ORDER BY bid.published_date DESC, bid.person_name
        LIMIT 100
    """, (stock_code,)).fetchall()

    # ── Ownership snapshot (latest per person) ───────────
    ownership = conn.execute("""
        SELECT
            bid.person_name,
            MAX(bid.direct_pct_after) AS direct_pct,
            MAX(bid.indirect_pct_after) AS indirect_pct
        FROM bursa_insider_details bid
        JOIN bursa_announcements ba ON bid.ann_id = ba.ann_id
        WHERE bid.stock_code = ?
        AND ba.subcategory IN ('DIRECTOR_S219','SUBSTANTIAL_S138')
        AND bid.published_date = (
            SELECT MAX(bid2.published_date)
            FROM bursa_insider_details bid2
            JOIN bursa_announcements ba2 ON bid2.ann_id = ba2.ann_id
            WHERE ba2.stock_code = ?
            AND ba2.subcategory IN ('DIRECTOR_S219','SUBSTANTIAL_S138')
            AND bid2.person_name = bid.person_name
        )
        GROUP BY bid.person_name
        HAVING (COALESCE(direct_pct,0) + COALESCE(indirect_pct,0)) > 0
        ORDER BY (COALESCE(direct_pct,0) + COALESCE(indirect_pct,0)) DESC
    """, (stock_code, stock_code)).fetchall()

    # ── Meta ─────────────────────────────────────────────
    meta = conn.execute("""
        SELECT stock_code, company_name, MAX(signal_score) AS score
        FROM bursa_insider_details
        WHERE stock_code = ?
        GROUP BY stock_code
    """, (stock_code,)).fetchone()

    conn.close()
    return jsonify({
        "meta": dict(meta) if meta else {"stock_code": stock_code},
        "trades": [dict(r) for r in trades],
        "ownership": [
            {
                "person_name": r["person_name"],
                "direct_pct": r["direct_pct"] or 0,
                "indirect_pct": r["indirect_pct"] or 0,
                "total_pct": (r["direct_pct"] or 0) + (r["indirect_pct"] or 0),
            }
            for r in ownership
        ],
    })


@app.route("/api/insider/stats")
def api_insider_stats():
    """Summary stats for the insider dashboard header."""
    conn = get_conn()
    total_stocks = conn.execute("""
        SELECT COUNT(DISTINCT stock_code) FROM bursa_insider_details
        WHERE published_date >= date('now', '-7 days')
    """).fetchone()[0]

    mega_signals = conn.execute("""
        SELECT COUNT(DISTINCT stock_code) FROM bursa_insider_details
        WHERE signal_score >= 10
        AND published_date >= date('now', '-7 days')
    """).fetchone()[0]

    strong_signals = conn.execute("""
        SELECT COUNT(DISTINCT stock_code) FROM bursa_insider_details
        WHERE signal_score >= 7 AND signal_score < 10
        AND published_date >= date('now', '-7 days')
    """).fetchone()[0]

    last_updated = conn.execute("""
        SELECT MAX(parsed_at) FROM bursa_insider_details
    """).fetchone()[0]

    conn.close()
    return jsonify({
        "total_stocks": total_stocks,
        "mega_signals": mega_signals,
        "strong_signals": strong_signals,
        "last_updated": last_updated,
    })




if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
