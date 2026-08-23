#!/usr/bin/env python3
"""
Shared Yahoo Finance client for every module in this repo.

Why this exists
---------------
The unofficial Yahoo chart API rate-limits aggressively, and a single
YFRateLimitError / HTTP 429 usually means the *IP* is in the penalty
box. Further requests from fx_signal, gold-watcher, indices_signal and
announcements will all fail — and, worse, will *extend* the ban.

This module is the single choke point:

  * one shared HTTP session (cookie + crumb negotiated once per process)
  * retry with backoff for transient network errors
  * NO retry on an explicit rate-limit — we trip a file-based circuit
    breaker instead, so every other process on the box fails fast
  * empty DataFrames are returned as None; callers decide whether a
    streak of empties should trip the circuit (FX collector does)

Circuit file : $YAHOO_CIRCUIT_PATH   (default /tmp/market-yahoo-circuit.json)
Cooldown     : $YAHOO_COOLDOWN_SECS  (default 900 = 15 minutes)

Usage from a subfolder script::

    import sys
    from pathlib import Path
    _ROOT = Path(__file__).resolve().parent.parent
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
    import yahoo_client
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

log = logging.getLogger("yahoo_client")

# ------------------------------------------------------------------
# Tunables (env-overridable so you can loosen/tighten without a deploy)
# ------------------------------------------------------------------
CIRCUIT_PATH = Path(os.environ.get("YAHOO_CIRCUIT_PATH", "/tmp/market-yahoo-circuit.json"))
COOLDOWN_SECS = float(os.environ.get("YAHOO_COOLDOWN_SECS", "900"))
MAX_ATTEMPTS = int(os.environ.get("YAHOO_MAX_ATTEMPTS", "3"))
RETRY_BACKOFF_BASE = float(os.environ.get("YAHOO_RETRY_BACKOFF", "2.0"))
PRICE_TTL_SECS = float(os.environ.get("YAHOO_PRICE_TTL", "60"))

# ------------------------------------------------------------------
# Process-local state
# ------------------------------------------------------------------
_SESSION = None          # yfinance session, or False if creation failed
_YF_RATE_LIMIT_EXC = None  # YFRateLimitError class (lazy)
_WARNED_OPEN = False
_PRICE_CACHE: dict[str, tuple[float, float]] = {}  # symbol -> (price, expiry_monotonic)


class YahooCircuitOpen(Exception):
    """Raised only by probe() when the circuit is open — history() returns None."""


# ------------------------------------------------------------------
# Interval / freshness helpers (pure; no yfinance needed)
# ------------------------------------------------------------------
def parse_interval(interval: str) -> timedelta:
    """
    Parse a yfinance-style interval ('1h', '4h', '1d', '15m') into a timedelta.
    """
    if not interval or len(interval) < 2:
        raise ValueError(f"unrecognised interval: {interval!r}")
    unit = interval[-1].lower()
    try:
        n = int(interval[:-1])
    except ValueError as exc:
        raise ValueError(f"unrecognised interval: {interval!r}") from exc
    if n <= 0:
        raise ValueError(f"interval must be positive: {interval!r}")
    if unit == "m":
        return timedelta(minutes=n)
    if unit == "h":
        return timedelta(hours=n)
    if unit == "d":
        return timedelta(days=n)
    if unit == "w":
        return timedelta(weeks=n)
    raise ValueError(f"unrecognised interval unit: {interval!r}")


def is_closed_bar_current(
    bar_time: datetime,
    interval: str,
    now: datetime | None = None,
) -> bool:
    """
    True if `bar_time` is recent enough to be the current *completed* bar
    for `interval`.

    Alignment-agnostic: we only require the stored bar to be newer than
    ``now - 2 * interval``. That covers both UTC-aligned 4h bars
    (00/04/08/…) and session-aligned ones (21/01/05/…) without us
    having to guess Yahoo's bucket edges.

    Examples (interval='1h'):
        now=15:30, bar=14:00  → True   (14:00 is the last closed hour)
        now=15:00:10, bar=13:00 → False  (we still need the 14:00 close)
    """
    if bar_time is None:
        return False
    if now is None:
        now = datetime.now(timezone.utc)
    if bar_time.tzinfo is None:
        bar_time = bar_time.replace(tzinfo=timezone.utc)
    else:
        bar_time = bar_time.astimezone(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    return bar_time >= now - (parse_interval(interval) * 2)


# ------------------------------------------------------------------
# Rate-limit detection
# ------------------------------------------------------------------
def _load_rate_limit_exc():
    global _YF_RATE_LIMIT_EXC
    if _YF_RATE_LIMIT_EXC is False:
        return None
    if _YF_RATE_LIMIT_EXC is not None:
        return _YF_RATE_LIMIT_EXC
    try:
        from yfinance.exceptions import YFRateLimitError
        _YF_RATE_LIMIT_EXC = YFRateLimitError
        return YFRateLimitError
    except Exception:
        _YF_RATE_LIMIT_EXC = False
        return None


def is_rate_limit_error(exc: BaseException) -> bool:
    """True if `exc` is Yahoo saying 'too many requests'."""
    cls = _load_rate_limit_exc()
    if cls is not None and isinstance(exc, cls):
        return True
    response = getattr(exc, "response", None)
    if response is not None and getattr(response, "status_code", None) == 429:
        return True
    msg = str(exc).lower()
    return any(
        needle in msg
        for needle in (
            "too many requests",
            "rate limit",
            "rate-limited",
            "ratelimit",
            "http 429",
            "status code 429",
        )
    )


# ------------------------------------------------------------------
# Circuit breaker (file-based, shared across processes)
# ------------------------------------------------------------------
def _read_circuit() -> dict | None:
    try:
        raw = CIRCUIT_PATH.read_text()
        data = json.loads(raw)
    except FileNotFoundError:
        return None
    except Exception as exc:
        log.warning("ignoring corrupt Yahoo circuit file %s: %s", CIRCUIT_PATH, exc)
        return None
    if not isinstance(data, dict) or "open_until" not in data:
        return None
    return data


def circuit_info() -> dict:
    """
    Snapshot of the circuit. Always safe to call.

    Keys: open (bool), remaining (float secs), reason (str), opened_at (str|None)
    """
    data = _read_circuit()
    if not data:
        return {"open": False, "remaining": 0.0, "reason": "", "opened_at": None}
    try:
        open_until = float(data["open_until"])
    except (TypeError, ValueError):
        return {"open": False, "remaining": 0.0, "reason": "", "opened_at": None}
    remaining = open_until - time.time()
    if remaining <= 0:
        return {"open": False, "remaining": 0.0, "reason": "", "opened_at": None}
    return {
        "open": True,
        "remaining": remaining,
        "reason": str(data.get("reason") or ""),
        "opened_at": data.get("opened_at"),
    }


def is_circuit_open() -> bool:
    return circuit_info()["open"]


def trip_circuit(reason: str, cooldown_secs: float | None = None) -> None:
    """Open the circuit so every process on this box backs off."""
    global _WARNED_OPEN
    cooldown = COOLDOWN_SECS if cooldown_secs is None else float(cooldown_secs)
    payload = {
        "open_until": time.time() + cooldown,
        "reason": reason,
        "opened_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        CIRCUIT_PATH.write_text(json.dumps(payload, indent=2) + "\n")
    except Exception:
        log.exception("failed to write Yahoo circuit file %s", CIRCUIT_PATH)
        return
    _WARNED_OPEN = False  # next fail-fast call should log once
    log.warning(
        "Yahoo circuit OPEN for %.0fs — %s (file=%s)",
        cooldown, reason, CIRCUIT_PATH,
    )


def reset_circuit() -> None:
    """Force-close the circuit (used by tests / manual recovery)."""
    global _WARNED_OPEN
    try:
        CIRCUIT_PATH.unlink()
    except FileNotFoundError:
        pass
    except Exception:
        log.exception("failed to remove Yahoo circuit file %s", CIRCUIT_PATH)
        return
    _WARNED_OPEN = False
    log.info("Yahoo circuit reset (%s removed)", CIRCUIT_PATH)


def _warn_open_once() -> None:
    global _WARNED_OPEN
    if _WARNED_OPEN:
        return
    info = circuit_info()
    log.warning(
        "Yahoo circuit OPEN for %.0fs (%s) — failing fast",
        info["remaining"], info["reason"] or "no reason recorded",
    )
    _WARNED_OPEN = True


# ------------------------------------------------------------------
# yfinance session (lazy — tests never import yfinance)
# ------------------------------------------------------------------
def _get_session():
    """
    One shared HTTP session per process. yfinance negotiates a Yahoo
    cookie + crumb per Ticker unless you pass a session; sharing it
    drops 2 extra requests per symbol.
    """
    global _SESSION
    if _SESSION is False:
        return None
    if _SESSION is not None:
        return _SESSION
    try:
        from yfinance.data import _backend as _yf_backend
        try:
            _SESSION = _yf_backend.Session(impersonate="chrome")
        except TypeError:
            _SESSION = _yf_backend.Session()
        return _SESSION
    except Exception:
        _SESSION = False
        return None


def _yf():
    import yfinance as yf
    return yf


# ------------------------------------------------------------------
# Public fetch API
# ------------------------------------------------------------------
def history(
    symbol: str,
    *,
    interval: str = "1d",
    period: str | None = None,
    start: str | None = None,
    end: str | None = None,
    auto_adjust: bool = True,
):
    """
    Fetch OHLCV for one symbol.

    Returns a DataFrame, or None if the circuit is open / every attempt
    failed / Yahoo returned an empty frame. Never raises on rate-limit —
    the circuit is tripped instead.
    """
    if is_circuit_open():
        _warn_open_once()
        return None

    yf = _yf()
    session = _get_session()
    last_exc: BaseException | None = None
    kwargs: dict = {"interval": interval, "auto_adjust": auto_adjust}
    if period is not None:
        kwargs["period"] = period
    if start is not None:
        kwargs["start"] = start
    if end is not None:
        kwargs["end"] = end

    for attempt in range(1, MAX_ATTEMPTS + 1):
        if is_circuit_open():
            _warn_open_once()
            return None
        try:
            ticker = yf.Ticker(symbol, session=session) if session is not None else yf.Ticker(symbol)
            df = ticker.history(**kwargs)
        except Exception as exc:
            last_exc = exc
            if is_rate_limit_error(exc):
                trip_circuit(f"{type(exc).__name__} on {symbol}: {exc}")
                return None
            if attempt < MAX_ATTEMPTS:
                wait = RETRY_BACKOFF_BASE ** attempt
                log.warning(
                    "%s: request failed (attempt %d/%d): %s — retry in %.1fs",
                    symbol, attempt, MAX_ATTEMPTS, exc, wait,
                )
                time.sleep(wait)
            continue

        if df is None or getattr(df, "empty", True):
            if attempt < MAX_ATTEMPTS:
                wait = RETRY_BACKOFF_BASE ** attempt
                log.warning(
                    "%s: empty DataFrame (attempt %d/%d), retrying in %.1fs",
                    symbol, attempt, MAX_ATTEMPTS, wait,
                )
                time.sleep(wait)
                continue
            log.warning("%s: empty DataFrame returned by yfinance", symbol)
            return None
        return df

    log.error("%s: all %d attempts failed — last error: %s", symbol, MAX_ATTEMPTS, last_exc)
    return None


def download(
    symbols: Iterable[str],
    *,
    interval: str,
    period: str,
    group_by: str = "ticker",
    threads: bool = True,
    auto_adjust: bool = True,
    progress: bool = False,
):
    """
    Multi-symbol yf.download() with the same circuit / retry rules as history().
    Returns a DataFrame or None.
    """
    if is_circuit_open():
        _warn_open_once()
        return None

    yf = _yf()
    session = _get_session()
    last_exc: BaseException | None = None
    syms = list(symbols)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        if is_circuit_open():
            _warn_open_once()
            return None
        try:
            kwargs = dict(
                interval=interval,
                period=period,
                group_by=group_by,
                threads=threads,
                progress=progress,
                auto_adjust=auto_adjust,
            )
            if session is not None:
                kwargs["session"] = session
            df = yf.download(syms, **kwargs)
        except Exception as exc:
            last_exc = exc
            if is_rate_limit_error(exc):
                trip_circuit(f"{type(exc).__name__} on batch {interval}: {exc}")
                return None
            if attempt < MAX_ATTEMPTS:
                wait = RETRY_BACKOFF_BASE ** attempt
                log.warning(
                    "batch %s: request failed (attempt %d/%d): %s — retry in %.1fs",
                    interval, attempt, MAX_ATTEMPTS, exc, wait,
                )
                time.sleep(wait)
            continue

        if df is None or getattr(df, "empty", True):
            if attempt < MAX_ATTEMPTS:
                wait = RETRY_BACKOFF_BASE ** attempt
                log.warning(
                    "batch %s: empty response (attempt %d/%d), retrying in %.1fs",
                    interval, attempt, MAX_ATTEMPTS, wait,
                )
                time.sleep(wait)
                continue
            log.warning("batch %s: empty response", interval)
            return None
        return df

    log.error("batch %s: all %d attempts failed — last error: %s", interval, MAX_ATTEMPTS, last_exc)
    return None


def last_price(symbol: str) -> float | None:
    """
    Latest last_price via yfinance fast_info, cached per process for
    PRICE_TTL_SECS. Honours the circuit breaker.
    """
    now = time.monotonic()
    cached = _PRICE_CACHE.get(symbol)
    if cached is not None and cached[1] > now:
        return cached[0]

    if is_circuit_open():
        _warn_open_once()
        return None

    yf = _yf()
    session = _get_session()
    try:
        ticker = yf.Ticker(symbol, session=session) if session is not None else yf.Ticker(symbol)
        fi = ticker.fast_info
        price = getattr(fi, "last_price", None)
        if price is None:
            price = getattr(fi, "previous_close", None)
            if price is not None:
                log.warning("%s: last_price unavailable, using previous_close=%.5f", symbol, price)
        if price is None:
            log.warning("%s: no live price from fast_info", symbol)
            return None
        value = float(price)
        _PRICE_CACHE[symbol] = (value, now + PRICE_TTL_SECS)
        return value
    except Exception as exc:
        if is_rate_limit_error(exc):
            trip_circuit(f"{type(exc).__name__} on {symbol} fast_info: {exc}")
            return None
        log.exception("fast_info failed for %s", symbol)
        return None


def probe(symbol: str = "EURUSD=X") -> dict:
    """
    One-shot health check used by preflight scripts.

    Returns {ok, symbol, close, error, circuit}.
    """
    info = circuit_info()
    if info["open"]:
        return {
            "ok": False,
            "symbol": symbol,
            "close": None,
            "error": f"circuit open for {info['remaining']:.0f}s ({info['reason']})",
            "circuit": info,
        }
    df = history(symbol, interval="1h", period="1d")
    if df is None or getattr(df, "empty", True):
        return {
            "ok": False,
            "symbol": symbol,
            "close": None,
            "error": "empty / failed response",
            "circuit": circuit_info(),
        }
    try:
        close = float(df["Close"].iloc[-1])
    except Exception as exc:
        return {
            "ok": False,
            "symbol": symbol,
            "close": None,
            "error": str(exc),
            "circuit": circuit_info(),
        }
    return {
        "ok": True,
        "symbol": symbol,
        "close": close,
        "error": None,
        "circuit": circuit_info(),
    }


# ------------------------------------------------------------------
# CLI — run on the server to inspect / reset the circuit
# ------------------------------------------------------------------
def _cli(argv: list[str] | None = None) -> int:
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    ap = argparse.ArgumentParser(description="Yahoo circuit status / probe")
    ap.add_argument("--probe", metavar="SYMBOL", nargs="?", const="EURUSD=X",
                    help="hit Yahoo once (default EURUSD=X) and print the result")
    ap.add_argument("--reset", action="store_true", help="force-close the circuit")
    args = ap.parse_args(argv)

    if args.reset:
        reset_circuit()
        print("circuit reset")
        return 0

    info = circuit_info()
    print(f"circuit file : {CIRCUIT_PATH}")
    print(f"cooldown     : {COOLDOWN_SECS:.0f}s")
    if info["open"]:
        print(f"state        : OPEN  remaining={info['remaining']:.0f}s")
        print(f"reason       : {info['reason']}")
        print(f"opened_at    : {info['opened_at']}")
    else:
        print("state        : closed")

    if args.probe:
        result = probe(args.probe)
        if result["ok"]:
            print(f"probe        : OK  {result['symbol']} close={result['close']}")
            return 0
        print(f"probe        : FAIL  {result['symbol']} — {result['error']}")
        return 1
    return 1 if info["open"] else 0


if __name__ == "__main__":
    raise SystemExit(_cli())
