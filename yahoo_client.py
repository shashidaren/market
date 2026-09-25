#!/usr/bin/env python3
"""
yahoo_client.py — one shared circuit breaker for every Yahoo job.

Everything in this stack that reads Yahoo Finance (fx_signal collector,
indices_signal collector, announcements price-context for the Telegram
alerts, the dashboard's movers tab) shares one public IP. When Yahoo
rate-limits that IP, each job used to find out on its own and keep
hammering — the movers tab had an in-process cooldown, but the FX cron
(every minute) and the others never saw it.

This module makes the "Yahoo is angry with us" state SHARED through a tiny
JSON file on disk:

    /tmp/market-yahoo-circuit.json      (override: YAHOO_CIRCUIT_PATH)

Rules
-----
    OPEN      any Yahoo rate-limit reply trips the circuit immediately.
    OPEN      FX_EMPTY_STREAK_TRIPS (default 3) consecutive empty replies
              recorded by the FX/indices collectors trip it too — Yahoo's
              "silent ban" shows up there first (their pairs are never
              legitimately empty).
    COOLDOWN  stays open for YAHOO_COOLDOWN_SECS (default 900 s), then lets
              calls through again (half-open). The first success closes it;
              the first failure re-trips it for the full cooldown.
    NO_DATA   Bursa-specific lookups (movers tab, insider price-context)
              deliberately do NOT count towards the empty streak:
              LEAP-market / freshly listed KL symbols are legitimately
              missing on Yahoo, and one open movers tab must not be able
              to pause the FX pipeline.

Every accessor is best-effort: a missing or corrupt state file just means
"not tripped", and unwritable paths are logged and ignored — this module
never raises into the caller.

Usage
-----
    import yahoo_client as yc

    if yc.is_open():                      # before touching Yahoo
        ...skip the fetch / serve cached data...
    yc.record_success()                   # after a good reply
    yc.record_empty("EURUSD=X")           # after an empty reply (FX/indices)
    yc.record_rate_limit("EURUSD=X")      # after a 429 / too-many-requests

Tests (and anyone who wants a private state file) can build their own:

    c = yc.Circuit(path="/tmp/test-circuit.json", clock=fake_time)

Env knobs (see .env.example):
    YAHOO_CIRCUIT_PATH       state file location
    YAHOO_COOLDOWN_SECS      how long a trip pauses everyone (900)
    FX_EMPTY_STREAK_TRIPS    empty replies before a silent-ban trip (3)
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("yahoo_client")

# Knobs live in the central /opt/market/.env like everything else (existing
# process env always wins — see env_loader.py).
try:
    from env_loader import load_env  # repo root (normal import)

    load_env()
except Exception:  # pragma: no cover - e.g. imported by file path
    try:
        _here = Path(__file__).resolve().parent
        import importlib.util as _ilu

        _spec = _ilu.spec_from_file_location("_yc_env_loader", _here / "env_loader.py")
        _mod = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        _mod.load_env()
    except Exception:
        pass


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


COOLDOWN_SECS = _env_int("YAHOO_COOLDOWN_SECS", 15 * 60)
# Kept the FX_ name as the fallback — it is the key .env.example has shipped
# since PR #3.
EMPTY_STREAK_TRIPS = _env_int("YAHOO_EMPTY_STREAK_TRIPS",
                              _env_int("FX_EMPTY_STREAK_TRIPS", 3))
CIRCUIT_PATH = os.environ.get("YAHOO_CIRCUIT_PATH", "/tmp/market-yahoo-circuit.json")


def _iso(epoch: float | None) -> str | None:
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


class Circuit:
    """
    One shared on-disk circuit-breaker state.

    `path`    where the JSON state lives (shared between processes).
    `clock`   injectable for tests; default time.time.
    """

    def __init__(self, path: str | Path | None = None, clock=time.time):
        self.path = Path(path) if path is not None else Path(CIRCUIT_PATH)
        self._clock = clock
        self._lock_path = self.path.with_name(self.path.name + ".lock")

    # ── state I/O (best-effort) ────────────────────────────────────
    def _read_unsafe(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write_unsafe(self, state: dict) -> None:
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent),
                                   prefix=self.path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh)
            os.replace(tmp, self.path)
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def _update(self, mutate) -> dict:
        """
        Read-modify-write under an exclusive flock. Races between the cron
        jobs are unlikely but possible; the lock makes streak counting exact
        on Linux, and any filesystem error degrades to "not tripped".
        """
        state: dict = {}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._lock_path, "w") as lock_fh:
                fcntl.flock(lock_fh, fcntl.LOCK_EX)
                state = self._read_unsafe()
                if not isinstance(state, dict):
                    state = {}
                mutate(state)
                self._write_unsafe(state)
        except OSError as exc:
            log.debug("yahoo circuit state unavailable (%s) — ignoring", exc)
        return state

    # ── queries ─────────────────────────────────────────────────────
    def state(self) -> dict:
        """Snapshot for logs/status pages. Never raises."""
        now = self._clock()
        raw: dict = {}
        try:
            raw = self._read_unsafe()
            if not isinstance(raw, dict):
                raw = {}
        except Exception:  # pragma: no cover - _read_unsafe already guards
            raw = {}
        until = float(raw.get("until") or 0)
        return {
            "path": str(self.path),
            "is_open": now < until,
            "until": until or None,
            "until_iso": _iso(until) or None,
            "remaining_secs": int(until - now) if now < until else 0,
            "tripped_at": raw.get("tripped_at"),
            "tripped_at_iso": _iso(raw.get("tripped_at")),
            "reason": raw.get("reason"),
            "by": raw.get("by"),
            "empty_streak": int(raw.get("empty_streak") or 0),
            "empty_streak_trips": EMPTY_STREAK_TRIPS,
            "cooldown_secs": COOLDOWN_SECS,
        }

    def is_open(self) -> bool:
        until = self._read_unsafe().get("until") or 0
        try:
            return self._clock() < float(until)
        except (TypeError, ValueError):
            return False

    def empty_streak(self) -> int:
        try:
            return int(self._read_unsafe().get("empty_streak") or 0)
        except (TypeError, ValueError):
            return 0

    # ── recording ───────────────────────────────────────────────────
    def record_success(self) -> dict:
        """A good reply: close the circuit and forget the empty streak."""
        def mutate(state: dict) -> None:
            state["empty_streak"] = 0
            state.pop("until", None)
            state.pop("tripped_at", None)
            state.pop("reason", None)
            state.pop("by", None)

        return self._update(mutate)

    def record_empty(self, symbol: str = "") -> dict:
        """
        An empty reply from a feed that should never be empty (FX/indices
        pairs). Trips the circuit after EMPTY_STREAK_TRIPS in a row.
        """
        def mutate(state: dict) -> None:
            streak = int(state.get("empty_streak") or 0) + 1
            state["empty_streak"] = streak
            if streak >= EMPTY_STREAK_TRIPS:
                self._trip(state, reason=f"{streak} empty replies in a row", by=symbol)

        return self._update(mutate)

    def record_rate_limit(self, symbol: str = "",
                          cooldown_secs: int | None = None) -> dict:
        """A 429 / too-many-requests reply: trip immediately."""
        def mutate(state: dict) -> None:
            self._trip(state, reason="Yahoo rate-limit reply", by=symbol,
                       cooldown_secs=cooldown_secs)

        return self._update(mutate)

    # -- helpers (caller holds the state under _update) --------------
    def _trip(self, state: dict, *, reason: str, by: str = "",
              cooldown_secs: int | None = None) -> None:
        now = self._clock()
        secs = COOLDOWN_SECS if cooldown_secs is None else cooldown_secs
        state["tripped_at"] = now
        state["until"] = now + secs
        state["reason"] = reason
        state["by"] = by
        state["empty_streak"] = 0
        log.warning("Yahoo circuit OPEN until %s (%s%s)",
                    _iso(state["until"]), reason, f", by {by}" if by else "")


# Module-level convenience wrappers around the one shared instance that all
# jobs in this repo use.
SHARED = Circuit()


def state() -> dict:
    return SHARED.state()


def is_open() -> bool:
    return SHARED.is_open()


def empty_streak() -> int:
    return SHARED.empty_streak()


def record_success() -> dict:
    return SHARED.record_success()


def record_empty(symbol: str = "") -> dict:
    return SHARED.record_empty(symbol)


def record_rate_limit(symbol: str = "", cooldown_secs: int | None = None) -> dict:
    return SHARED.record_rate_limit(symbol, cooldown_secs)


if __name__ == "__main__":
    # Manual peek: python3 yahoo_client.py
    st = state()
    print(f"circuit {st['path']}")
    print(f"  open: {st['is_open']}"
          + (f" until {st['until_iso']} ({st['remaining_secs']}s left)" if st["is_open"] else ""))
    print(f"  reason: {st['reason'] or '—'} · by: {st['by'] or '—'}")
    print(f"  empty streak: {st['empty_streak']}/{st['empty_streak_trips']}")
