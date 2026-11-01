#!/usr/bin/env python3
"""
Top-mover tab — ONE stock, the highest (heuristic) probability of a big move.

Where /movers ranks *insider activity* and shows five charts, this tab
answers a narrower question: among the stocks something is happening in,
which single name is most likely to make a large move next session?

How the pick is made
--------------------
1. Universe (cheap, database only — no Yahoo calls):
   - every stock the insider signal engine scored in the window, and
   - every stock mentioned in recently scored headlines (the scorer's
     `tickers` column, mapped to a Bursa code through the registry).
2. Interest ranking picks a shortlist (`TOP_MOVER_SHORTLIST`, default 12)
   so the shared Yahoo quota is not burned on the whole market.
3. Prices for the shortlist come from the SAME cached fetcher as the
   /movers tab (`insider_movers.PRICE_CACHE`), so a name shown on both
   tabs is downloaded once.
4. Probability: each stock's own 1-year history gives the empirical
   frequency of a ≥3% day (close vs previous close). Current conditions
   scale that base rate: volume expansion, volatility regime, position in
   the 52-week range, news mentions and insider conviction.

So the number is not a black box — it is "this stock moves ≥3% on about
9% of days, and today's setup makes that ~2.1× more likely → ~19%".
It is a heuristic, NOT a calibrated/back-tested model. The tab says so.

    python3 top_mover.py                    # human-readable run
    python3 top_mover.py --shortlist 20 --no-prices
    python3 top_mover.py --json

Env knobs (see .env.example): TOP_MOVER_SHORTLIST, TOP_MOVER_THRESHOLD_PCT,
TOP_MOVER_INSIDER_DAYS, TOP_MOVER_NEWS_DAYS.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import insider_movers as im

_REPO_ROOT = Path(__file__).resolve().parent
DB_PATH = _REPO_ROOT / "news.db"

log = logging.getLogger("top_mover")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# ─── Config ────────────────────────────────────────────────────────

DEFAULT_INSIDER_DAYS = 10
MAX_INSIDER_DAYS = 90
DEFAULT_NEWS_DAYS = 2
MAX_NEWS_DAYS = 14
DEFAULT_SHORTLIST = 12
MIN_SHORTLIST = 3
MAX_SHORTLIST = 24          # every name is one more Yahoo request per TTL
DEFAULT_RUNNERS = 4

# The event we quote a probability for: |close / previous close - 1| ≥ this.
MOVE_THRESHOLD_PCT = _env_float("TOP_MOVER_THRESHOLD_PCT", 3.0)

# Fewer bars than this and the stock's own base rate is meaningless — use
# the prior instead of quoting a number off 4 observations.
MIN_HISTORY_BARS = 30

# Configuration: the pick is a single stock, so the numbers must be honest.
DISCLAIMER = (
    "Heuristic probability, not a calibrated or back-tested model. It is the "
    "stock's own recent frequency of large daily moves, scaled by today's "
    "volume, volatility, range position, news and insider activity. "
    "Informational only — not investment advice."
)


def _config() -> dict:
    return {
        "shortlist": max(MIN_SHORTLIST, min(MAX_SHORTLIST,
                                            _env_int("TOP_MOVER_SHORTLIST", DEFAULT_SHORTLIST))),
        "insider_days": max(1, min(MAX_INSIDER_DAYS,
                                   _env_int("TOP_MOVER_INSIDER_DAYS", DEFAULT_INSIDER_DAYS))),
        "news_days": max(1, min(MAX_NEWS_DAYS,
                                _env_int("TOP_MOVER_NEWS_DAYS", DEFAULT_NEWS_DAYS))),
        "threshold_pct": MOVE_THRESHOLD_PCT,
    }


# ─── Universe: insider + news candidates (database only) ──────────

def _registry():
    """Company registry, or None if the data files are unavailable."""
    try:
        from tickers import get_registry
        return get_registry()
    except Exception as exc:  # noqa: BLE001 — the tab must still work
        log.info("company registry unavailable: %s", exc)
        return None


def _pct_change(prev: float | None, now: float | None) -> float | None:
    if not prev or now is None:
        return None
    return (now / prev - 1.0) * 100.0


def load_insider_candidates(conn: sqlite3.Connection, days: int) -> tuple[dict, str | None]:
    """{code: summary} for every scored insider stock in the window."""
    try:
        leaders = im.load_leaders(conn, days=days, limit=500, rank="score")
    except sqlite3.OperationalError as exc:
        return {}, f"insider tables not available ({exc})"
    if leaders.get("error"):
        return {}, leaders["error"]
    return {s["stock_code"]: s for s in leaders["stocks"]}, None


def _dedupe_names(names: list[str]) -> list[str]:
    """
    One headline can list a parent and its subsidiary ("Petronas",
    "Petronas Chemicals"). Count the most specific name only, so the
    headline does not inflate two stocks' news heat.
    """
    uniq = sorted({n.strip() for n in names if n.strip()}, key=len, reverse=True)
    kept: list[str] = []
    for name in uniq:
        if any(re.search(r"\b" + re.escape(name) + r"\b", other, re.IGNORECASE)
               for other in kept):
            continue
        kept.append(name)
    return kept


def load_news_candidates(conn: sqlite3.Connection,
                         days: int) -> tuple[dict, str | None]:
    """
    {canonical_name: {"mentions": n, "last_scored": iso}}
    from the scorer's ticker column. Empty (not an error) when the scorer
    has not produced any malaysia_direct rows yet.
    """
    try:
        rows = conn.execute(
            """
            SELECT tickers, scored_at
            FROM scored_headlines
            WHERE scored_at >= datetime('now', ?)
              AND relevance = 'malaysia_direct'
              AND tickers IS NOT NULL AND tickers != ''
            ORDER BY scored_at DESC
            LIMIT 2000
            """,
            (f"-{int(days)} days",),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}, "scored_headlines not available yet (scorer has not run)"

    out: dict[str, dict] = {}
    for tickers, scored_at in rows:
        for name in _dedupe_names(str(tickers).split(",")):
            entry = out.setdefault(name, {"mentions": 0, "last_scored": None})
            entry["mentions"] += 1
            if scored_at and (not entry["last_scored"] or scored_at > entry["last_scored"]):
                entry["last_scored"] = scored_at
    return out, None


def build_universe(conn: sqlite3.Connection, config: dict, registry=None) -> dict:
    """
    Merge insider and news candidates into one per-stock dict, ranked by a
    cheap interest score, trimmed to the shortlist.
    """
    registry = registry if registry is not None else _registry()
    insider, insider_error = load_insider_candidates(conn, config["insider_days"])
    news, news_error = load_news_candidates(conn, config["news_days"])

    candidates: dict[str, dict] = {}

    def slot(code: str, company: str | None) -> dict:
        entry = candidates.setdefault(code, {
            "stock_code": code,
            "company_name": company,
            "sector": None,
            "insider": None,
            "news_mentions": 0,
            "news_last_scored": None,
        })
        if not entry.get("company_name") and company:
            entry["company_name"] = company
        return entry

    for code, summary in insider.items():
        entry = slot(code, summary.get("company_name"))
        entry["insider"] = summary

    news_only = 0
    for canonical, info in news.items():
        company = registry.company_by_name(canonical) if registry else None
        if company is None or not company.code:
            continue
        entry = slot(company.code, company.name)
        entry["sector"] = company.sector
        entry["news_mentions"] += info["mentions"]
        if info["last_scored"] and (not entry["news_last_scored"]
                                    or info["last_scored"] > entry["news_last_scored"]):
            entry["news_last_scored"] = info["last_scored"]
        if entry["insider"] is None:
            news_only += 1

    if registry:
        for entry in candidates.values():
            company = registry.company_by_code(entry["stock_code"])
            if company:
                entry["sector"] = entry["sector"] or company.sector
                entry["company_name"] = entry["company_name"] or company.name

    def interest(entry: dict) -> tuple:
        summary = entry["insider"] or {}
        score = summary.get("score") or 0
        filings = summary.get("n_filings") or 0
        both = 1 if (summary and entry["news_mentions"]) else 0
        return (
            score * 2 + filings + entry["news_mentions"] * 1.5 + both * 2,
            entry["news_mentions"],
            score,
            entry["stock_code"],
        )

    ranked = sorted(candidates.values(), key=interest, reverse=True)
    shortlist = ranked[: config["shortlist"]]
    return {
        "candidates": candidates,
        "shortlist": shortlist,
        "counts": {
            "insider": len(insider),
            "news_names": len(news),
            "news_only": news_only,
            "total": len(candidates),
            "shortlist": len(shortlist),
        },
        "warnings": [w for w in (insider_error, news_error) if w],
        "insider_error": insider_error,
        "news_error": news_error,
    }


# ─── Movement features from daily bars ────────────────────────────

def _safe_ratio(a: float | None, b: float | None) -> float | None:
    if not a or not b:
        return None
    return a / b


def movement_features(bars: list[dict], threshold_pct: float = MOVE_THRESHOLD_PCT) -> dict:
    """
    Everything the probability needs, computed from one stock's daily bars
    (oldest → newest). No look-ahead: every value uses bars up to `last`.
    """
    closes = [b["c"] for b in bars]
    last = bars[-1]
    prev = bars[-2] if len(bars) > 1 else None

    # Empirical base rate: how often this stock makes a ≥ threshold% day.
    moves = [
        abs(closes[i] / closes[i - 1] - 1.0) * 100.0
        for i in range(1, len(closes)) if closes[i - 1]
    ]
    hits = sum(1 for m in moves if m >= threshold_pct)
    # Beta-ish smoothing keeps a short history from claiming 0% or 100%.
    has_history = len(bars) >= MIN_HISTORY_BARS
    base_rate = (hits + 1.0) / (len(moves) + 20.0) if (moves and has_history) else None
    mean_abs = sum(moves) / len(moves) if moves else None

    # True range in % of price, for volatility regime.
    trs = []
    for i in range(1, len(bars)):
        high, low, prev_close = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    atr14 = sum(trs[-14:]) / len(trs[-14:]) if trs else None
    atr5 = sum(trs[-5:]) / len(trs[-5:]) if trs else None
    atr_pct = (atr14 / last["c"] * 100.0) if atr14 else None
    atr5_pct = (atr5 / last["c"] * 100.0) if atr5 else None
    atr_1y_median = None
    if trs:
        ordered = sorted(t / last["c"] * 100.0 for t in trs)
        atr_1y_median = ordered[len(ordered) // 2]

    vols = [b["v"] for b in bars]
    avg20 = sum(vols[-21:-1]) / 20 if len(vols) >= 21 else None
    avg5 = sum(vols[-5:]) / 5 if len(vols) >= 5 else None
    prior20 = vols[-25:-5]
    prior20_avg = sum(prior20) / len(prior20) if len(prior20) >= 10 else None

    hi = max(b["h"] for b in bars)
    lo = min(b["l"] for b in bars)
    range_pos = ((last["c"] - lo) / (hi - lo) * 100.0) if hi > lo else None

    return {
        "bars": len(bars),
        "last": last["c"],
        "as_of": last["t"],
        "prev_close": prev["c"] if prev else None,
        "day_chg_pct": _pct_change(prev["c"], last["c"]) if prev else None,
        "gap_pct": _pct_change(prev["c"], last["o"]) if prev else None,
        "chg_5d_pct": _pct_change(closes[-6], last["c"]) if len(closes) > 6 else None,
        "chg_20d_pct": _pct_change(closes[-21], last["c"]) if len(closes) > 21 else None,
        "base_rate": base_rate,
        "base_rate_pct": round(base_rate * 100.0, 1) if base_rate is not None else None,
        "mean_abs_move_pct": round(mean_abs, 2) if mean_abs is not None else None,
        "atr_pct": round(atr_pct, 2) if atr_pct is not None else None,
        "atr5_pct": round(atr5_pct, 2) if atr5_pct is not None else None,
        "atr_regime": round(_safe_ratio(atr5_pct, atr_1y_median), 2)
        if (atr5_pct and atr_1y_median) else None,
        "vol_last_ratio": round(_safe_ratio(last["v"], avg20), 2) if avg20 else None,
        "vol_avg20": round(avg20) if avg20 else None,
        "vol_ratio_5d": round(_safe_ratio(avg5, prior20_avg), 2)
        if (avg5 and prior20_avg) else None,
        "high_1y": hi,
        "low_1y": lo,
        "range_pos_pct": round(range_pos, 1) if range_pos is not None else None,
        "to_1y_high_pct": round((last["c"] / hi - 1.0) * 100.0, 2) if hi else None,
    }


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def score_candidate(features: dict, entry: dict, threshold_pct: float) -> dict:
    """
    Turn features + context into a probability for "moves ≥ threshold% next
    session". Returns the probability, the total multiplier, and the list of
    reasons so the UI can explain itself.
    """
    base = features.get("base_rate")
    if base is None:
        # No usable history: fall back to a market-ish prior (Bursa large
        # caps make a ≥3% day roughly 8-12% of the time).
        base = 0.10
        base_source = "prior (too little price history)"
    else:
        base_source = "own 1y history"

    reasons: list[dict] = []
    factors: dict[str, float] = {}

    vol_ratio = features.get("vol_ratio_5d") or 1.0
    vol_last = features.get("vol_last_ratio") or 1.0
    volume_factor = 1.0 + 0.35 * _clamp(math.log2(max(vol_ratio, 1.0)), 0.0, 2.0) \
        + 0.20 * _clamp(math.log2(max(vol_last, 1.0)), 0.0, 2.0)
    factors["volume"] = round(volume_factor, 3)
    if volume_factor > 1.05:
        reasons.append({
            "text": f"volume running {vol_ratio:.1f}× its prior 20-day average",
            "factor": factors["volume"],
        })

    regime = features.get("atr_regime")
    if regime:
        vol_factor = 1.0 + 0.45 * _clamp(regime - 1.0, 0.0, 1.5)
        factors["volatility"] = round(vol_factor, 3)
        if vol_factor > 1.05:
            reasons.append({
                "text": f"volatility {regime:.1f}× its 1-year median "
                        f"(ATR {features.get('atr5_pct'):.1f}% of price)",
                "factor": factors["volatility"],
            })

    pos = features.get("range_pos_pct")
    to_high = features.get("to_1y_high_pct")
    if pos is not None and to_high is not None:
        breakout = 1.0 + 0.22 * _clamp(pos / 100.0, 0.0, 1.0)
        if to_high >= -3.0:
            breakout += 0.15
        factors["range"] = round(breakout, 3)
        if breakout > 1.05:
            where = ("at its 52-week high" if to_high >= -0.5
                     else f"{abs(to_high):.1f}% below its 52-week high")
            reasons.append({"text": f"trading {where}", "factor": factors["range"]})

    mentions = entry.get("news_mentions") or 0
    if mentions:
        news_factor = 1.0 + 0.18 * min(mentions, 4)
        factors["news"] = round(news_factor, 3)
        reasons.append({
            "text": f"{mentions} headline{'s' if mentions != 1 else ''} in the "
                    f"last {entry.get('news_days', DEFAULT_NEWS_DAYS)} day(s)",
            "factor": factors["news"],
        })

    summary = entry.get("insider") or {}
    score = summary.get("score") or 0
    if summary:
        insider_factor = 1.0 + 0.04 * min(int(score), 12)
        # Conviction relative to how much the stock normally trades: 1× the
        # 20-day average volume in insider net flow is a real position.
        net_vs_adv = summary.get("net_vs_adv")
        if not net_vs_adv and features.get("vol_avg20"):
            net_vs_adv = abs(summary.get("net_shares") or 0) / features["vol_avg20"]
        if net_vs_adv:
            insider_factor += 0.12 * _clamp(abs(net_vs_adv), 0.0, 2.0)
        factors["insider"] = round(insider_factor, 3)
        direction = summary.get("direction")
        reasons.append({
            "text": f"insider signal {score}/10 — net {direction} "
                    f"({summary.get('n_filings', 0)} filing(s))",
            "factor": factors["insider"],
        })

    day = features.get("day_chg_pct")
    if day is not None:
        move_factor = 1.0 + 0.12 * _clamp(abs(day) / max(threshold_pct, 0.5), 0.0, 2.0)
        factors["already_moving"] = round(move_factor, 3)
        if move_factor > 1.05:
            reasons.append({
                "text": f"already moving {day:+.1f}% today",
                "factor": factors["already_moving"],
            })

    multiplier = 1.0
    for value in factors.values():
        multiplier *= value
    multiplier = _clamp(multiplier, 0.60, 4.00)

    probability = _clamp(base * multiplier, 0.01, 0.95)
    mean_abs = features.get("mean_abs_move_pct")
    expected = mean_abs * multiplier if mean_abs else None

    bars = features.get("bars") or 0
    confidence = ("high" if bars >= 150 and (mentions or summary)
                  else "medium" if bars >= 60
                  else "low")

    return {
        "probability": round(probability, 4),
        "probability_pct": round(probability * 100.0, 1),
        "base_rate_pct": round(base * 100.0, 1),
        "base_rate_source": base_source,
        "multiplier": round(multiplier, 2),
        "expected_abs_move_pct": round(expected, 2) if expected is not None else None,
        "confidence": confidence,
        "factors": factors,
        "reasons": sorted(reasons, key=lambda r: r["factor"], reverse=True),
    }


# ─── Payload ───────────────────────────────────────────────────────

def parse_params(args) -> dict:
    config = _config()

    def as_int(name: str, default: int, lo: int, hi: int) -> int:
        try:
            value = int(args.get(name, default))
        except (TypeError, ValueError):
            value = default
        return max(lo, min(hi, value))

    config["insider_days"] = as_int("days", config["insider_days"], 1, MAX_INSIDER_DAYS)
    config["news_days"] = as_int("news_days", config["news_days"], 1, MAX_NEWS_DAYS)
    config["shortlist"] = as_int("shortlist", config["shortlist"], MIN_SHORTLIST, MAX_SHORTLIST)
    config["limit"] = as_int("runners", DEFAULT_RUNNERS, 0, 10)
    return config


def _candidate_payload(entry: dict, view: dict | None, config: dict,
                       include_bars: bool = False) -> dict | None:
    if not view or not view.get("bars"):
        return None
    features = movement_features(view["bars"], config["threshold_pct"])
    entry_ctx = dict(entry, news_days=config["news_days"])
    scoring = score_candidate(features, entry_ctx, config["threshold_pct"])

    summary = entry.get("insider") or {}
    payload = {
        "stock_code": entry["stock_code"],
        "company_name": entry.get("company_name"),
        "sector": entry.get("sector"),
        "symbol": im.yahoo_symbol(entry["stock_code"]),
        "price_status": view.get("status"),
        "price_error": view.get("error"),
        "price_fetched_at": view.get("fetched_at"),
        "news_mentions": entry.get("news_mentions") or 0,
        "news_last_scored": entry.get("news_last_scored"),
        "insider": {
            "score": summary.get("score"),
            "n_filings": summary.get("n_filings"),
            "direction": summary.get("direction"),
            "net_shares": summary.get("net_shares"),
            "gross_shares": summary.get("gross_shares"),
            "last_trade_date": summary.get("last_trade_date"),
            "last_published": summary.get("last_published"),
            "trades": (summary.get("trades") or [])[:12],
        } if summary else None,
        "features": features,
        **scoring,
    }
    if include_bars:
        payload["bars"] = view["bars"]
    return payload


def build_payload(conn: sqlite3.Connection, config: dict | None = None,
                  cache=None, with_prices: bool = True, registry=None) -> dict:
    config = config or _config()
    cache = cache if cache is not None else im.PRICE_CACHE

    universe = build_universe(conn, config, registry)
    shortlist = universe["shortlist"]

    views: dict[str, dict] = {}
    if with_prices and shortlist:
        views = cache.get_many([entry["stock_code"] for entry in shortlist])

    ranked: list[dict] = []
    no_price: list[dict] = []
    for entry in shortlist:
        candidate = _candidate_payload(entry, views.get(entry["stock_code"]),
                                       config, include_bars=True)
        if candidate is None:
            view = views.get(entry["stock_code"]) or {}
            no_price.append({
                "stock_code": entry["stock_code"],
                "company_name": entry.get("company_name"),
                "status": view.get("status") or "skipped",
                "reason": view.get("error") or ("prices disabled" if not with_prices
                                                else "no price data"),
            })
            continue
        ranked.append(candidate)

    ranked.sort(
        key=lambda c: (
            c["probability"],
            c.get("expected_abs_move_pct") or 0,
            abs(c["features"].get("day_chg_pct") or 0),
            c["stock_code"],
        ),
        reverse=True,
    )

    pick = ranked[0] if ranked else None
    runners = ranked[1: 1 + config.get("limit", DEFAULT_RUNNERS)]

    price_feed = {}
    if hasattr(cache, "feed_status"):
        try:
            price_feed = cache.feed_status()
        except Exception:  # noqa: BLE001
            price_feed = {}

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "params": {
            "insider_days": config["insider_days"],
            "news_days": config["news_days"],
            "shortlist": config["shortlist"],
            "threshold_pct": config["threshold_pct"],
        },
        "universe": universe["counts"],
        "warnings": universe["warnings"],
        "price_feed": price_feed,
        "threshold_pct": config["threshold_pct"],
        "disclaimer": DISCLAIMER,
        "pick": pick,
        "runners": [{k: v for k, v in c.items() if k != "bars"} for c in runners],
        "ranked": [{k: v for k, v in c.items() if k != "bars"} for c in ranked],
        "no_price": no_price,
    }


# ─── CLI ───────────────────────────────────────────────────────────

def _fmt_pct(value: float | None) -> str:
    return f"{value:+.1f}%" if value is not None else "—"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Single highest-probability mover among insider/news-active stocks")
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--days", type=int, help="insider window (default 10)")
    parser.add_argument("--news-days", type=int, help="headline window (default 2)")
    parser.add_argument("--shortlist", type=int, help="stocks to price (default 12)")
    parser.add_argument("--runners", type=int, default=DEFAULT_RUNNERS)
    parser.add_argument("--no-prices", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    config = parse_params(vars(args))

    conn = sqlite3.connect(args.db)
    try:
        payload = build_payload(conn, config, with_prices=not args.no_prices)
    finally:
        conn.close()

    if args.json:
        print(json.dumps(payload, indent=2))
        return

    counts = payload["universe"]
    print(f"\nUniverse: {counts['insider']} insider-active · {counts['news_names']} "
          f"news-mentioned ({counts['news_only']} news-only) → "
          f"shortlist {counts['shortlist']} of {counts['total']}")
    for warning in payload["warnings"]:
        print(f"  [WARN] {warning}")
    pick = payload["pick"]
    if not pick:
        print("\nNo candidate had price data — nothing to rank.")
        return

    print(f"\nTOP PICK  {pick['stock_code']} {pick['company_name'] or ''} "
          f"[{pick['sector'] or '—'}]")
    print(f"  probability of a ≥{payload['threshold_pct']:.1f}% move next session: "
          f"{pick['probability_pct']:.1f}%  (base {pick['base_rate_pct']:.1f}% × "
          f"{pick['multiplier']:.2f} conditions, confidence {pick['confidence']})")
    if pick.get("expected_abs_move_pct") is not None:
        print(f"  expected absolute daily move: ±{pick['expected_abs_move_pct']:.2f}%")
    f = pick["features"]
    print(f"  last {f['last']:.3f} ({_fmt_pct(f['day_chg_pct'])}) · "
          f"5d {_fmt_pct(f['chg_5d_pct'])} · vol {f['vol_ratio_5d'] or '—'}× · "
          f"range pos {f['range_pos_pct']}%")
    for reason in pick["reasons"]:
        print(f"    + {reason['text']}  ({reason['factor']:.2f}×)")

    if payload["runners"]:
        print("\nRunners-up:")
        for c in payload["runners"]:
            print(f"  {c['stock_code']:<5} {str(c['company_name'] or '')[:28]:<28} "
                  f"{c['probability_pct']:>5.1f}%  "
                  f"(base {c['base_rate_pct']:>4.1f}% × {c['multiplier']:.2f})")
    if payload["no_price"]:
        print(f"\nSkipped (no price): {len(payload['no_price'])} stock(s)")
    if payload["params"]["insider_days"]:
        print(f"\n{payload['disclaimer']}")


if __name__ == "__main__":
    main()
