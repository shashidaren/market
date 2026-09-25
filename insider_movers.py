#!/usr/bin/env python3
"""
Insider movers — price action for the stocks with the HIGHEST insider
activity. Powers the dashboard's /movers tab (see app.py).

How it works
------------
1. Rank stocks by insider activity using the SAME tables the /insider tab
   reads (bursa_insider_details + bursa_announcements, filled by the
   announcements_signal cron pipeline). No new cron job is needed.
2. Keep the top N (default 5, hard cap MAX_LIMIT).
3. Fetch ~1 year of DAILY bars per stock from Yahoo Finance (`XXXX.KL`,
   same mapping as announcements_signal/price_context.py) through an
   in-process cache, so the tab never hammers Yahoo.
4. Compute price metrics relative to the insider activity: move since the
   insiders started trading, move since the latest insider trade (with the
   same FRESH / LATE verdict the Telegram alerts use), price vs the
   insiders' average price, and volume vs normal.

Yahoo budget
------------
At most one request per stock per cache TTL — 10 min during Bursa hours,
60 min outside — no matter how many browsers have the tab open. Failed
lookups are negatively cached. A rate-limit reply (or 3 empty replies in
a row, Yahoo's "silent ban") opens a cooldown (YAHOO_COOLDOWN_SECS,
default 900 s) during which only cached data is served. The cooldown is
also recorded in the SHARED circuit breaker (yahoo_client.py), so the fx
and indices jobs on the same IP pause too — and a trip by one of them
pauses this tab.

Quick check on the server (does Yahoo answer for today's top 5?):

    python3 insider_movers.py
    python3 insider_movers.py --days 14 --rank filings
    python3 insider_movers.py --no-prices        # DB only, no Yahoo calls
"""

from __future__ import annotations

import argparse
import importlib.util
import logging
import os
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("insider_movers")

_REPO_ROOT = Path(__file__).resolve().parent

# Knobs can live in the central /opt/market/.env like everything else
# (existing process env always wins — see env_loader.py).
try:  # pragma: no cover - env_loader always exists in this repo
    from env_loader import load_env

    load_env()
except Exception:  # pragma: no cover
    pass

# Cross-process Yahoo circuit breaker (shared with the fx / indices /
# price-context jobs on the same IP). Optional: everything still works
# with only the in-process cooldown if the import fails.
try:
    import yahoo_client
except Exception:  # pragma: no cover
    yahoo_client = None


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# ─── Config ────────────────────────────────────────────────────────

DB_PATH = _REPO_ROOT / "news.db"

INSIDER_SUBCATS = ("DIRECTOR_S219", "SUBSTANTIAL_S138")
# Same suffixes signals_engine.py skips (warrants are mechanical noise).
SKIP_STOCK_SUFFIXES = ("WA", "WB", "WC", "WD", "WE")

DEFAULT_LIMIT = 5
MAX_LIMIT = 10          # every extra stock = one more Yahoo request per TTL
DEFAULT_DAYS = 7        # same window as the /insider tab
MAX_DAYS = 90
RANK_MODES = ("score", "filings", "shares")

MYT = timezone(timedelta(hours=8))                  # Bursa runs on MYT
TTL_MARKET_SECS = _env_int("MOVERS_PRICE_TTL", 10 * 60)
TTL_OFFHOURS_SECS = _env_int("MOVERS_PRICE_TTL_OFFHOURS", 60 * 60)
TTL_ERROR_SECS = 5 * 60         # network error → retry after 5 min
TTL_NO_DATA_SECS = 30 * 60      # Yahoo has no data for the symbol
COOLDOWN_SECS = _env_int("YAHOO_COOLDOWN_SECS", 15 * 60)
SILENT_BAN_STREAK = 3           # consecutive empty replies → cooldown
ENTRY_MAX_AGE_SECS = 2 * 86400  # forget symbols nobody has asked for in 2 days
FETCH_WAIT_SECS = 20            # max time one API call waits on Yahoo
HISTORY_PERIOD = "1y"

PRICE_SOURCE = "Yahoo Finance daily bars (delayed ~15 min)"


# ─── Small helpers ─────────────────────────────────────────────────

def yahoo_symbol(stock_code: str) -> str:
    """Bursa code → Yahoo symbol. Mirrors price_context._bursa_to_yahoo."""
    return f"{str(stock_code).strip().upper().zfill(4)}.KL"


def _norm_name(name: str | None) -> str:
    return re.sub(r"\s+", " ", (name or "").upper()).strip()


def _pct(base: float | None, now: float | None) -> float | None:
    if base is None or now is None or base <= 0:
        return None
    return round((now - base) / base * 100, 2)


def _iso_utc(epoch: float | None) -> str | None:
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


def bursa_session_open(now: datetime | None = None) -> bool:
    """
    True during Bursa trading hours (Mon–Fri ~08:45–17:30 MYT, padded so
    Yahoo's ~15 min delay still catches the close). Public holidays are
    not modelled — they just get the shorter TTL.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(MYT)
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return 8 * 60 + 45 <= minutes <= 17 * 60 + 30


# ─── Trade method (best-effort, from the filing's circumstances) ───

_METHOD_RULES = (
    # offer FIRST: a take-over acceptance is a corporate event even when the
    # text also mentions the market — these flows must be excluded from the
    # ranking sums (see summarize_stock).
    ("offer", re.compile(
        r"take[\s-]*over|tender\s+(?:offer|documents?)|\boffers?\s+(?:by|from)\b|"
        r"\boffers?\s+to\s+(?:acquire|purchase|merge)\b|"
        r"accept\w*\s+of\s+(?:the\s+|an?\s+|conditional\s+|partial\s+|voluntary\s+)*offer|"
        r"\boffer\s+documents?\b|pursuant\s+to\s+the\s+offer", re.I)),
    # order matters: "off market" must win over "market"
    ("off-mkt", re.compile(r"off[\s-]*market|direct business|married deal", re.I)),
    ("open-mkt", re.compile(r"open[\s-]*market|\bon[\s-]*market|through bursa|via bursa", re.I)),
    ("esos", re.compile(
        r"\besos\b|\bess\b|\besgp?\b|\bsgp\b|\bltip?\b|\brsp\b|\bpsp\b|share option|"
        r"share grant|share award|share scheme|share issuance scheme|vesting", re.I)),
    ("corp-action", re.compile(
        r"bonus|rights|dividend reinvest|\bdrp\b|\bdrs\b|convers|warrant|subscription|"
        r"placement|share split|subdivision|consolidat|capital reduction|allotment|"
        r"offer for sale|\bipo\b", re.I)),
    ("transfer", re.compile(r"transfer|gift|pledg|inherit|in specie|distribution|estate of", re.I)),
)


def classify_method(circumstances: str | None) -> str:
    """
    Rough label for HOW the shares changed hands. Open-market trades are
    the ones that actually move (and signal on) the price; ESOS, transfers
    and corporate actions usually are not.
    """
    text = (circumstances or "").strip()
    if not text:
        return "unknown"
    for label, pattern in _METHOD_RULES:
        if pattern.search(text):
            return label
    return "other"


_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def parse_trade_price(consideration: str | None, price_per_share: float | None) -> float | None:
    """
    Per-share price from the filing. parse_detail.py only fills
    price_per_share for simple values ('RM0.870'); also accept ranges
    ('RM0.85 - RM0.90' → midpoint). Totals ('RM1,250,000') come out as
    absurd per-share prices and are dropped later by the sanity check
    against the market close.
    """
    if price_per_share and price_per_share > 0:
        return float(price_per_share)
    if not consideration:
        return None
    nums = []
    for raw in _NUM_RE.findall(str(consideration)):
        try:
            nums.append(float(raw.replace(",", "")))
        except ValueError:
            continue
    nums = [n for n in nums if n > 0]
    if len(nums) == 1:
        return nums[0]
    if len(nums) == 2 and re.search(r"-|–|\bto\b", str(consideration), re.I):
        return round((nums[0] + nums[1]) / 2, 4)
    return None


# ─── Insider side (DB) ─────────────────────────────────────────────

def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def fetch_insider_rows(conn: sqlite3.Connection, days: int) -> list[dict]:
    """
    All director (S219) / substantial-holder (S138) filings published in
    the last `days` days. Tolerates older DBs where signals_engine.py has
    not added signal_score yet.
    """
    cols = _table_columns(conn, "bursa_insider_details")
    if not cols:
        raise sqlite3.OperationalError("no such table: bursa_insider_details")

    def col(name: str) -> str:
        return f"bid.{name}" if name in cols else f"NULL AS {name}"

    optional = ", ".join(col(c) for c in (
        "date_of_change", "nature_of_interest", "consideration",
        "price_per_share", "circumstances"))
    score = ("COALESCE(bid.signal_score, 0) AS signal_score"
             if "signal_score" in cols else "0 AS signal_score")
    placeholders = ",".join("?" * len(INSIDER_SUBCATS))
    query = f"""
        SELECT bid.ann_id, bid.stock_code, bid.company_name,
               bid.published_date, bid.person_name, bid.transaction_type,
               bid.shares_transacted, {optional}, {score},
               ba.subcategory, ba.url
        FROM bursa_insider_details bid
        JOIN bursa_announcements ba ON bid.ann_id = ba.ann_id
        WHERE bid.published_date >= date('now', ?)
          AND ba.subcategory IN ({placeholders})
          AND bid.stock_code IS NOT NULL
        ORDER BY bid.published_date DESC, bid.ann_id DESC
    """
    cur = conn.execute(query, (f"-{int(days)} days", *INSIDER_SUBCATS))
    names = [d[0] for d in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def dedupe_trades(rows: list[dict]) -> list[dict]:
    """
    One entry per real transaction. A director who is also a substantial
    holder files BOTH an S219 and an S138 for the same trade — keep the
    S219 (it carries the price). Key = (person, trade date, side, shares),
    so genuinely separate trades on the same day are all kept.
    """
    ordered = sorted(
        rows, key=lambda r: (0 if r.get("subcategory") == "DIRECTOR_S219" else 1, r["ann_id"])
    )
    seen: set[tuple] = set()
    trades: list[dict] = []
    for r in ordered:
        tx = r.get("transaction_type")
        if tx not in ("ACQUISITION", "DISPOSAL"):
            continue
        trade_date = r.get("date_of_change") or r.get("published_date")
        shares = int(r.get("shares_transacted") or 0)
        key = (_norm_name(r.get("person_name")), trade_date, tx, shares)
        if key in seen:
            continue
        seen.add(key)

        lag = None
        try:
            if r.get("date_of_change") and r.get("published_date"):
                lag = (datetime.fromisoformat(r["published_date"])
                       - datetime.fromisoformat(r["date_of_change"])).days
        except ValueError:
            lag = None

        trades.append({
            "ann_id": r["ann_id"],
            "trade_date": trade_date,
            "published_date": r.get("published_date"),
            "lag_days": lag,
            "person_name": r.get("person_name"),
            "side": "buy" if tx == "ACQUISITION" else "sell",
            "shares": shares,
            "price": parse_trade_price(r.get("consideration"), r.get("price_per_share")),
            "consideration": r.get("consideration"),
            "nature": r.get("nature_of_interest"),
            "filing": "D" if r.get("subcategory") == "DIRECTOR_S219" else "S",
            "method": classify_method(r.get("circumstances")),
            "url": r.get("url"),
        })
    trades.sort(key=lambda t: (t["trade_date"] or "", t["published_date"] or "", t["ann_id"]),
                reverse=True)
    return trades


def summarize_stock(stock_code: str, rows: list[dict]) -> dict:
    """
    Aggregate one stock's filings in the window into an activity summary.

    The flow numbers (bought / sold / net / gross) count UNIQUE
    transactions — key (trade_date, side, shares) — not per-person sums:
    deemed-interest and group re-filings (holding company, spouse, private
    vehicle…) repeat the SAME trade under many names, which used to
    inflate the numbers massively (MKH showed +317M for one transaction).
    The per-person echoes stay in `trades`, so the table still shows every
    filing. Cost of the rule: two genuinely-separate same-size trades on
    the same day count once — far rarer than re-filings.

    Trades tagged method == "offer" (take-over / tender acceptance) are
    excluded from the flow numbers and reported separately (offer_shares,
    offer_trades, offer_flag): they are corporate events, not signals.
    """
    trades = dedupe_trades(rows)

    unique: dict[tuple, dict] = {}
    for t in trades:
        unique.setdefault((t["trade_date"], t["side"], t["shares"]), t)

    market = [t for t in unique.values() if t["method"] != "offer"]
    offer_txns = [t for t in unique.values() if t["method"] == "offer"]
    offer_shares = sum(t["shares"] for t in offer_txns)
    bought = sum(t["shares"] for t in market if t["side"] == "buy")
    sold = sum(t["shares"] for t in market if t["side"] == "sell")
    gross = bought + sold
    net = bought - sold

    # Flag the card when take-over flows dominate the window's activity
    # (≥ ~20% of all transacted shares).
    offer_flag = bool(offer_txns) and offer_shares * 5 >= offer_shares + gross

    raw_bought = sum(t["shares"] for t in trades if t["side"] == "buy")
    raw_sold = sum(t["shares"] for t in trades if t["side"] == "sell")

    trade_dates = sorted(t["trade_date"] for t in trades if t["trade_date"])
    published = sorted(r["published_date"] for r in rows if r.get("published_date"))
    lags = [t["lag_days"] for t in trades if t["lag_days"] is not None and t["lag_days"] >= 0]
    company = next((r["company_name"] for r in rows if r.get("company_name")), None)

    return {
        "stock_code": stock_code,
        "company_name": company,
        "ticker": yahoo_symbol(stock_code),
        "score": max(int(r.get("signal_score") or 0) for r in rows),
        "n_filings": len({r["ann_id"] for r in rows}),
        "active_days": len(set(published)),
        "insiders": len({_norm_name(t["person_name"]) for t in trades}),
        "bought": bought,
        "sold": sold,
        "net_shares": net,
        "gross_shares": gross,
        "bought_all_persons": raw_bought,
        "sold_all_persons": raw_sold,
        "offer_trades": len(offer_txns),
        "offer_shares": offer_shares,
        "offer_flag": offer_flag,
        "direction": "buy" if net > 0 else "sell" if net < 0 else "flat",
        "first_trade_date": trade_dates[0] if trade_dates else None,
        "last_trade_date": trade_dates[-1] if trade_dates else None,
        "first_published": published[0] if published else None,
        "last_published": published[-1] if published else None,
        "avg_lag_days": round(sum(lags) / len(lags), 1) if lags else None,
        "trades": trades,
    }


_RANK_KEYS = {
    "score": lambda s: (s["score"], s["n_filings"], s["gross_shares"], s["last_published"] or ""),
    "filings": lambda s: (s["n_filings"], s["score"], s["gross_shares"], s["last_published"] or ""),
    "shares": lambda s: (s["gross_shares"], s["score"], s["n_filings"], s["last_published"] or ""),
}


def load_leaders(
    conn: sqlite3.Connection,
    days: int = DEFAULT_DAYS,
    limit: int = DEFAULT_LIMIT,
    rank: str = "score",
) -> dict:
    """
    Top `limit` stocks by insider activity. Only stocks the insider signal
    engine scored > 0 are eligible (same rule as the /insider table), so
    warrants and tiny filings are already filtered out. If nothing is
    scored (engine not running), falls back to every stock with trades.
    """
    rank = rank if rank in _RANK_KEYS else "score"
    try:
        rows = fetch_insider_rows(conn, days)
    except sqlite3.OperationalError as exc:
        return {"stocks": [], "eligible": 0, "scored": False,
                "error": f"insider tables not available ({exc}) — "
                         "run the announcements_signal pipeline first"}

    by_stock: dict[str, list[dict]] = {}
    for r in rows:
        code = str(r["stock_code"]).strip().upper()
        if code.endswith(SKIP_STOCK_SUFFIXES):
            continue
        by_stock.setdefault(code, []).append(r)

    summaries = [summarize_stock(code, rs) for code, rs in by_stock.items()]
    summaries = [s for s in summaries if s["trades"]]
    scored = any(s["score"] > 0 for s in summaries)
    if scored:
        summaries = [s for s in summaries if s["score"] > 0]

    summaries.sort(key=_RANK_KEYS[rank], reverse=True)
    return {
        "stocks": summaries[: max(1, int(limit))],
        "eligible": len(summaries),
        "scored": scored,
        "error": None,
    }


# ─── Price side (Yahoo, cached) ────────────────────────────────────

class PriceFetchError(Exception):
    """Raised by fetchers. kind ∈ {'rate_limit', 'no_data', 'network', 'setup'}."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def _classify_exception(exc: Exception) -> str:
    if isinstance(exc, PriceFetchError):
        return exc.kind
    name = type(exc).__name__
    text = str(exc).lower()
    if name == "YFRateLimitError" or "too many requests" in text or "rate limit" in text \
            or re.search(r"\b429\b", text):
        return "rate_limit"
    if name in ("YFPricesMissingError", "YFTickerMissingError", "YFTzMissingError") \
            or "delisted" in text or "no price data" in text or "no data found" in text:
        return "no_data"
    return "network"


def fetch_daily_bars(symbol: str, period: str = HISTORY_PERIOD) -> list[dict]:
    """
    One Yahoo request: daily OHLCV for `symbol`. Raw (unadjusted) prices,
    so they compare directly with the insiders' filed prices.
    Returns [{t:'YYYY-MM-DD', o, h, l, c, v}, ...] oldest → newest.
    """
    try:
        import yfinance as yf  # lazy: heavy import, optional dependency
    except ImportError as exc:
        raise PriceFetchError(
            "setup", "yfinance is not installed for the dashboard's Python — "
                     "/usr/bin/python3 -m pip install yfinance") from exc

    ticker = yf.Ticker(symbol)
    try:
        # yfinance deprecates history(raise_errors=...) in favour of
        # yf.config.debug.hide_exceptions. Setting it to False keeps errors
        # raising (same behaviour as raise_errors=True) without the
        # DeprecationWarning spamming the service log.
        debug = getattr(getattr(yf, "config", None), "debug", None)
        if getattr(debug, "hide_exceptions", None) is not None:
            debug.hide_exceptions = False
            df = ticker.history(period=period, interval="1d",
                                auto_adjust=False, timeout=10)
        else:
            df = ticker.history(period=period, interval="1d", auto_adjust=False,
                                timeout=10, raise_errors=True)
    except TypeError:  # very old yfinance without timeout/raise_errors
        df = ticker.history(period=period, interval="1d", auto_adjust=False)

    if df is None or df.empty:
        raise PriceFetchError("no_data", f"no price data on Yahoo for {symbol}")

    bars: dict[str, dict] = {}
    for idx, row in df.iterrows():
        close = row.get("Close")
        if close is None or close != close or close <= 0:  # NaN-safe
            continue

        def val(key: str) -> float:
            v = row.get(key)
            return float(v) if v is not None and v == v and v > 0 else float(close)

        vol = row.get("Volume")
        bars[idx.strftime("%Y-%m-%d")] = {
            "t": idx.strftime("%Y-%m-%d"),
            "o": round(val("Open"), 4),
            "h": round(val("High"), 4),
            "l": round(val("Low"), 4),
            "c": round(float(close), 4),
            "v": int(vol) if vol is not None and vol == vol else 0,
        }
    if not bars:
        raise PriceFetchError("no_data", f"no usable bars from Yahoo for {symbol}")
    return [bars[k] for k in sorted(bars)]


class PriceCache:
    """
    Thread-safe TTL cache in front of a bar fetcher (default: Yahoo).

    - fresh hit        → no request
    - stale / missing  → fetched in a small worker pool; the caller waits
                         at most `wait_secs`, then gets whatever is ready
                         (slow fetches finish in the background and land
                         in the cache for the next refresh)
    - failed refresh   → previous bars are kept and served as 'stale'
    - rate limit       → cooldown: no requests at all until it expires

    On top of the in-process cooldown there is the SHARED cross-process
    circuit breaker (yahoo_client.Circuit, default SHARED): while the fx
    or indices jobs have tripped Yahoo on this IP, this cache stops
    requesting too — and its own rate-limit trips pause the other jobs.
    Pass circuit=None to disable (tests).
    """

    def __init__(self, fetcher=None, clock=time.time, max_workers: int = 4,
                 circuit: str = "shared"):
        self._fetcher = fetcher or fetch_daily_bars
        self._clock = clock
        if circuit == "shared":
            circuit = yahoo_client.SHARED if yahoo_client is not None else None
        self._circuit = circuit
        self._lock = threading.Lock()
        self._entries: dict[str, dict] = {}
        self._inflight: dict[str, object] = {}
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="movers-yahoo")
        self._empty_streak = 0
        self.cooldown_until = 0.0
        self.requests_made = 0

    # -- policy ------------------------------------------------------
    def ok_ttl(self, now: float | None = None) -> int:
        now = self._clock() if now is None else now
        dt = datetime.fromtimestamp(now, timezone.utc)
        return TTL_MARKET_SECS if bursa_session_open(dt) else TTL_OFFHOURS_SECS

    def _is_fresh(self, entry: dict | None, now: float) -> bool:
        if not entry:
            return False
        kind = entry.get("error_kind")
        if kind:
            ttl = TTL_NO_DATA_SECS if kind == "no_data" else TTL_ERROR_SECS
            return now - entry["attempted_at"] < ttl
        return now - entry["fetched_at"] < self.ok_ttl(now)

    def in_cooldown(self, now: float | None = None) -> bool:
        now = self._clock() if now is None else now
        return now < self.cooldown_until

    def _shared_open(self) -> bool:
        try:
            return self._circuit is not None and self._circuit.is_open()
        except Exception:  # noqa: BLE001 — the breaker must never break us
            return False

    # -- fetching ----------------------------------------------------
    def _fetch_one(self, symbol: str) -> None:
        started = self._clock()
        try:
            bars = self._fetcher(symbol)
            if not bars:
                raise PriceFetchError("no_data", f"no price data on Yahoo for {symbol}")
        except Exception as exc:  # noqa: BLE001 — every failure is cached
            kind = _classify_exception(exc)
            msg = str(exc).splitlines()[0][:200] if str(exc) else type(exc).__name__
            with self._lock:
                entry = dict(self._entries.get(symbol) or {"symbol": symbol, "bars": None,
                                                           "fetched_at": None})
                entry.update(attempted_at=started, error=msg, error_kind=kind)
                self._entries[symbol] = entry
                if kind == "no_data":
                    self._empty_streak += 1
                if kind == "rate_limit" or self._empty_streak >= SILENT_BAN_STREAK:
                    self.cooldown_until = max(self.cooldown_until, started + COOLDOWN_SECS)
                    self._empty_streak = 0
                    log.warning("Yahoo cooldown until %s (%s: %s)",
                                _iso_utc(self.cooldown_until), symbol, msg)
            # Tell the other jobs on this IP. no_data is deliberately NOT
            # recorded: KL LEAP-market symbols are legitimately missing and
            # a movers tab must not pause the fx pipeline (see yahoo_client).
            if self._circuit is not None and kind == "rate_limit":
                try:
                    self._circuit.record_rate_limit(symbol)
                except Exception:  # noqa: BLE001
                    pass
            log.warning("price fetch failed for %s [%s]: %s", symbol, kind, msg)
        else:
            with self._lock:
                self._entries[symbol] = {"symbol": symbol, "bars": bars, "fetched_at": started,
                                         "attempted_at": started, "error": None,
                                         "error_kind": None}
                self._empty_streak = 0
            if self._circuit is not None:
                try:
                    self._circuit.record_success()
                except Exception:  # noqa: BLE001
                    pass
        finally:
            with self._lock:
                self._inflight.pop(symbol, None)

    def get_many(self, codes: list[str], wait_secs: float = FETCH_WAIT_SECS) -> dict[str, dict]:
        """Return {stock_code: view} — see _view() for the shape."""
        now = self._clock()
        symbols = {code: yahoo_symbol(code) for code in codes}
        futures = []
        with self._lock:
            self._prune(now, keep=set(symbols.values()))
            cooling = now < self.cooldown_until or self._shared_open()
            for sym in dict.fromkeys(symbols.values()):
                if self._is_fresh(self._entries.get(sym), now):
                    continue
                if sym in self._inflight:
                    futures.append(self._inflight[sym])
                    continue
                if cooling:
                    continue
                fut = self._pool.submit(self._fetch_one, sym)
                self._inflight[sym] = fut
                self.requests_made += 1
                futures.append(fut)
        if futures:
            wait(futures, timeout=wait_secs)

        with self._lock:
            return {code: self._view(sym) for code, sym in symbols.items()}

    def _prune(self, now: float, keep: set[str]) -> None:
        """Forget symbols nobody asked for in ENTRY_MAX_AGE_SECS (caller holds the lock)."""
        for sym in [s for s, e in self._entries.items()
                    if s not in keep and s not in self._inflight
                    and now - e.get("attempted_at", now) > ENTRY_MAX_AGE_SECS]:
            del self._entries[sym]

    def _view(self, symbol: str) -> dict:
        now = self._clock()
        entry = self._entries.get(symbol)
        pending = symbol in self._inflight
        bars = entry.get("bars") if entry else None
        error = entry.get("error") if entry else None
        kind = entry.get("error_kind") if entry else None

        if bars and not kind and self._is_fresh(entry, now):
            status = "ok"
        elif bars:
            status = "stale"          # older data kept after a failed / skipped refresh
        elif pending:
            status = "pending"        # still downloading — next refresh will have it
        elif (self.in_cooldown(now) or self._shared_open()) and not entry:
            status = "cooldown"       # local pause, or a trip from another job
        else:
            status = "error"

        if status == "cooldown":
            if self.in_cooldown(now):
                error = f"Yahoo cooldown until {_iso_utc(self.cooldown_until)} (rate-limit protection)"
            else:
                shared = self._circuit.state() if self._circuit is not None else {}
                error = (f"Yahoo paused for all jobs until {shared.get('until_iso')} "
                         f"({shared.get('reason') or 'rate-limit protection'} — shared circuit breaker)")
        elif status == "pending" and not error:
            error = "price download still in progress"

        fetched_at = entry.get("fetched_at") if entry else None
        return {
            "symbol": symbol,
            "status": status,
            "error": error if status != "ok" else None,
            "error_kind": kind if status != "ok" else None,
            "fetched_at": _iso_utc(fetched_at),
            "age_secs": int(now - fetched_at) if fetched_at else None,
            "bars": bars,
        }

    def feed_status(self) -> dict:
        now = self._clock()
        with self._lock:
            status = {
                "source": PRICE_SOURCE,
                "market_open": bursa_session_open(datetime.fromtimestamp(now, timezone.utc)),
                "ttl_secs": self.ok_ttl(now),
                "cooldown_until": _iso_utc(self.cooldown_until) if now < self.cooldown_until
                else None,
                "cached_symbols": len(self._entries),
                "requests_made": self.requests_made,
            }
        if self._circuit is not None:
            try:
                shared = self._circuit.state()
                status["shared_circuit"] = {
                    "path": shared.get("path"),
                    "open": shared.get("is_open"),
                    "until": shared.get("until_iso"),
                    "reason": shared.get("reason"),
                }
            except Exception:  # noqa: BLE001
                pass
        return status


PRICE_CACHE = PriceCache()


# ─── Metrics ───────────────────────────────────────────────────────

_classifier = None
_classifier_loaded = False


def _verdict(pct: float | None):
    """
    Same wording/thresholds as the Telegram alerts: reuse
    announcements_signal/price_context._classify_move (loaded by file path
    so its folder never shadows the root collector.py on sys.path).
    """
    global _classifier, _classifier_loaded
    if pct is None:
        return None
    if not _classifier_loaded:
        _classifier_loaded = True
        path = _REPO_ROOT / "announcements_signal" / "price_context.py"
        try:
            spec = importlib.util.spec_from_file_location("_movers_price_context", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _classifier = module._classify_move
        except Exception as exc:  # noqa: BLE001
            log.info("price_context verdicts unavailable: %s", exc)
            _classifier = None
    if _classifier is None:
        return None
    try:
        label, emoji = _classifier(pct)
        return {"label": label, "emoji": emoji}
    except Exception:  # noqa: BLE001
        return None


def _bar_on_or_after(bars: list[dict], day: str | None) -> dict | None:
    """First bar dated on/after `day` (trade on a holiday → next session)."""
    if not day:
        return None
    for b in bars:
        if b["t"] >= day:
            return b
    return None


def insider_avg_price(trades: list[dict], side: str, bars: list[dict] | None) -> dict | None:
    """
    Share-weighted average price the insiders paid (or received).
    Prices outside 0.5×–2× of that day's close are ignored — they are
    usually totals or typos, not per-share prices.
    """
    total_shares = 0
    total_value = 0.0
    for t in trades:
        if t["side"] != side or not t["price"] or not t["shares"]:
            continue
        price = t["price"]
        ref = _bar_on_or_after(bars, t["trade_date"]) if bars else None
        if ref:
            if not 0.5 * ref["c"] <= price <= 2.0 * ref["c"]:
                continue
        elif price > 1000:
            continue
        total_shares += t["shares"]
        total_value += t["shares"] * price
    if not total_shares:
        return None
    return {"price": round(total_value / total_shares, 4), "shares": total_shares}


def bursa_tick_size(price: float) -> float:
    """
    Bursa Malaysia minimum bid (tick) for securities — one tick on a low-
    priced stock is a big % move (5037 at RM0.020: 0.015 → 0.020 = +33%).

        below RM1.00  → RM0.005
        RM1 – 9.99    → RM0.010
        RM10 – 99.98  → RM0.020
        RM100 & above → RM0.100
    """
    if price < 1.0:
        return 0.005
    if price < 10.0:
        return 0.01
    if price < 100.0:
        return 0.02
    return 0.10


def compute_metrics(bars: list[dict], summary: dict) -> dict:
    """Price metrics for one stock, relative to its insider activity."""
    last = bars[-1]
    prev = bars[-2] if len(bars) > 1 else None

    def back(n: int) -> float | None:
        return bars[-1 - n]["c"] if len(bars) > n else None

    vols = [b["v"] for b in bars]
    avg20 = sum(vols[-21:-1]) / 20 if len(vols) >= 21 else None
    avg5 = sum(vols[-5:]) / 5 if len(vols) >= 5 else None
    prior20 = vols[-25:-5]
    prior20_avg = sum(prior20) / len(prior20) if len(prior20) >= 10 else None

    hi = max(b["h"] for b in bars)
    lo = min(b["l"] for b in bars)

    first_bar = _bar_on_or_after(bars, summary.get("first_trade_date"))
    latest_bar = _bar_on_or_after(bars, summary.get("last_trade_date"))
    since_latest = _pct(latest_bar["c"], last["c"]) if latest_bar else None

    direction = summary.get("direction")
    avg_buy = insider_avg_price(summary["trades"], "buy", bars)
    avg_sell = insider_avg_price(summary["trades"], "sell", bars)
    ref_avg = avg_sell if direction == "sell" else avg_buy

    net = summary.get("net_shares") or 0

    # Penny / tick warning: on sub-RM1 stocks one minimum bid step is a
    # large % move, so "up 33%" can literally be one tick.
    tick = bursa_tick_size(last["c"])
    one_tick_pct = round(tick / last["c"] * 100, 2)

    return {
        "last": last["c"],
        "as_of": last["t"],
        "prev_close": prev["c"] if prev else None,
        "day_chg_pct": _pct(prev["c"], last["c"]) if prev else None,
        "chg_5d_pct": _pct(back(5), last["c"]),
        "chg_1m_pct": _pct(back(21), last["c"]),
        "chg_3m_pct": _pct(back(63), last["c"]),
        "high_1y": hi,
        "low_1y": lo,
        "range_pos_pct": round((last["c"] - lo) / (hi - lo) * 100, 1) if hi > lo else None,
        "vol_last": last["v"],
        "vol_avg20": round(avg20) if avg20 else None,
        "vol_ratio_5d": round(avg5 / prior20_avg, 2) if avg5 and prior20_avg else None,
        "net_vs_adv": round(net / avg20, 2) if avg20 else None,
        "first_trade_close": first_bar["c"] if first_bar else None,
        "first_trade_bar": first_bar["t"] if first_bar else None,
        "since_first_trade_pct": _pct(first_bar["c"], last["c"]) if first_bar else None,
        "latest_trade_close": latest_bar["c"] if latest_bar else None,
        "since_latest_trade_pct": since_latest,
        # Verdict wording is buy-side ("LATE — up x% since trade"), so only
        # attach it when insiders are net BUYING.
        "verdict": _verdict(since_latest) if direction == "buy" else None,
        "avg_buy_price": avg_buy["price"] if avg_buy else None,
        "avg_sell_price": avg_sell["price"] if avg_sell else None,
        "vs_insider_avg_pct": _pct(ref_avg["price"], last["c"]) if ref_avg else None,
        "vs_insider_avg_side": ("sell" if direction == "sell" else "buy") if ref_avg else None,
        "tick_size": tick,
        "one_tick_pct": one_tick_pct,
        "is_penny": last["c"] < 0.10,
    }


# ─── Payload ───────────────────────────────────────────────────────

def parse_params(args) -> tuple[int, int, str]:
    """(days, limit, rank) from a request.args-like mapping, clamped."""
    def as_int(name: str, default: int, lo: int, hi: int) -> int:
        try:
            value = int(args.get(name, default))
        except (TypeError, ValueError):
            value = default
        return max(lo, min(hi, value))

    rank = str(args.get("rank", "score")).lower()
    return (
        as_int("days", DEFAULT_DAYS, 1, MAX_DAYS),
        as_int("limit", DEFAULT_LIMIT, 1, MAX_LIMIT),
        rank if rank in RANK_MODES else "score",
    )


def build_payload(leaders: dict, days: int, limit: int, rank: str,
                  cache: PriceCache | None = None, with_prices: bool = True) -> dict:
    """Attach cached prices + metrics to the leaders from load_leaders()."""
    cache = cache or PRICE_CACHE
    stocks = leaders["stocks"]
    views = cache.get_many([s["stock_code"] for s in stocks]) if (with_prices and stocks) else {}

    for s in stocks:
        view = views.get(s["stock_code"])
        if view is None:
            s["price"] = {"symbol": s["ticker"], "status": "skipped", "error": None, "bars": None}
            s["metrics"] = None
            continue
        s["price"] = view
        s["metrics"] = compute_metrics(view["bars"], s) if view.get("bars") else None

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "params": {"days": days, "limit": limit, "rank": rank},
        "eligible": leaders.get("eligible", 0),
        "scored": leaders.get("scored", False),
        "error": leaders.get("error"),
        "price_feed": cache.feed_status(),
        "stocks": stocks,
    }


# ─── CLI ───────────────────────────────────────────────────────────

def _fmt_pct(v: float | None) -> str:
    return f"{v:+.1f}%" if v is not None else "—"


def main() -> None:
    parser = argparse.ArgumentParser(description="Top insider-activity stocks + price moves")
    parser.add_argument("--db", default=str(DB_PATH), help="path to news.db")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--rank", choices=RANK_MODES, default="score")
    parser.add_argument("--no-prices", action="store_true", help="skip Yahoo")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    days, limit, rank = parse_params(vars(args))

    conn = sqlite3.connect(args.db)
    try:
        leaders = load_leaders(conn, days=days, limit=limit, rank=rank)
    finally:
        conn.close()
    if leaders.get("error"):
        print(leaders["error"])
        sys.exit(1)

    payload = build_payload(leaders, days, limit, rank, with_prices=not args.no_prices)
    print(f"\nTop {limit} by {rank} — last {days}d "
          f"({payload['eligible']} eligible stocks, scored={payload['scored']})")
    print(f"{'#':>2} {'code':<7} {'company':<28} {'score':>5} {'fil':>4} {'net shares':>12} "
          f"{'last':>8} {'day':>7} {'start':>7} {'latest':>7}  verdict / price status")
    for i, s in enumerate(payload["stocks"], 1):
        m = s.get("metrics") or {}
        price = s.get("price") or {}
        tail = (m.get("verdict") or {}).get("label") if m.get("verdict") else ""
        if price.get("status") not in ("ok", "skipped"):
            tail = f"[{price.get('status')}] {price.get('error') or ''}".strip()
        print(f"{i:>2} {s['stock_code']:<7} {str(s['company_name'] or '')[:28]:<28} "
              f"{s['score']:>5} {s['n_filings']:>4} {s['net_shares']:>+12,} "
              f"{(str(round(m['last'], 3)) if m.get('last') else '—'):>8} "
              f"{_fmt_pct(m.get('day_chg_pct')):>7} {_fmt_pct(m.get('since_first_trade_pct')):>7} "
              f"{_fmt_pct(m.get('since_latest_trade_pct')):>7}  {tail}")
    feed = payload["price_feed"]
    print(f"\nprice feed: {feed['source']} · ttl {feed['ttl_secs']}s · "
          f"cooldown {feed['cooldown_until'] or 'none'} · requests {feed['requests_made']}")


if __name__ == "__main__":
    main()
