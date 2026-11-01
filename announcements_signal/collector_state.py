"""Small amount of shared state for the Bursa announcements collector.

The state file lives next to ``news.db`` and is deliberately tiny:

* ``last_success_at`` — heartbeat: last run that fetched *and stored* page 1;
* ``last_run``        — measured outcome of the most recent run;
* ``request_times``   — rolling one-hour API request budget, shared by cron runs.

No existing SQLite table is changed.  Writes are atomic (temp file + rename).
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - the production box is Linux
    fcntl = None

STATE_SCHEMA_VERSION = 1
MYT = timezone(timedelta(hours=8))


def state_path_for(db_path: Path | str) -> Path:
    """``news.db`` -> ``news.db.collector.json`` (covered by ``*.db.*`` ignore)."""
    db_path = Path(db_path)
    return db_path.with_name(db_path.name + ".collector.json")


def empty_state() -> dict:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "last_success_at": None,
        "last_run_at": None,
        "last_run": None,
        "request_times": [],
    }


def load_state(path: Path | str) -> dict:
    """Read state; a missing/corrupt file starts empty (preflight reports it)."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return empty_state()
        base = empty_state()
        base.update(data)
        if not isinstance(base.get("request_times"), list):
            base["request_times"] = []
        return base
    except (OSError, ValueError):
        return empty_state()


def save_state(path: Path | str, state: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, sort_keys=True, separators=(",", ":"))
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


@contextmanager
def collector_lock(path: Path | str):
    """Non-blocking cross-process lock.  Yields True if acquired, else False."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    fh = open(lock_path, "a+", encoding="utf-8")
    acquired = False
    try:
        if fcntl is not None:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                acquired = False
        else:  # pragma: no cover
            acquired = True
        yield acquired
    finally:
        if acquired and fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


class RequestBudget:
    """Rolling hourly + per-run cap.  Reservations are persisted before fetch."""

    def __init__(self, state: dict, path: Path | str, *, per_run: int,
                 per_hour: int, clock=time.time):
        self.state = state
        self.path = Path(path)
        self.per_run = max(1, int(per_run))
        self.per_hour = max(1, int(per_hour))
        self.clock = clock
        self.run_requests = 0

    def _prune(self, now: float) -> None:
        cutoff = now - 3600.0
        self.state["request_times"] = [
            float(t) for t in self.state.get("request_times", [])
            if isinstance(t, (int, float)) and t > cutoff
        ]

    def reserve(self) -> bool:
        now = float(self.clock())
        self._prune(now)
        if self.run_requests >= self.per_run:
            return False
        if len(self.state["request_times"]) >= self.per_hour:
            return False
        self.state["request_times"].append(now)
        self.run_requests += 1
        save_state(self.path, self.state)
        return True

    def remaining(self) -> int:
        self._prune(float(self.clock()))
        return max(0, min(self.per_run - self.run_requests,
                          self.per_hour - len(self.state["request_times"])))


# ── Business-day helpers (weekends + configurable Bursa holidays) ──────────

def load_holidays(directory: Path | str | None = None) -> set[date]:
    """Load shipped holidays plus ``BURSA_HOLIDAYS=YYYY-MM-DD,...`` overrides.

    The JSON file is a small, auditable calendar; update it annually from the
    Bursa trading calendar.  Unknown future holidays simply fall back to
    weekday/weekend logic and can be added by env without a code change.
    """
    directory = Path(directory) if directory is not None else Path(__file__).parent
    holidays: set[date] = set()
    path = directory / "bursa_holidays.json"
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        for item in data.get("holidays", []):
            try:
                holidays.add(date.fromisoformat(str(item)))
            except ValueError:
                continue
    except (OSError, ValueError, AttributeError):
        pass
    for item in os.environ.get("BURSA_HOLIDAYS", "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            holidays.add(date.fromisoformat(item))
        except ValueError:
            continue
    return holidays


def is_business_day(day: date, holidays: set[date] | None = None) -> bool:
    holidays = holidays if holidays is not None else load_holidays()
    return day.weekday() < 5 and day not in holidays


def previous_business_day(day: date, holidays: set[date] | None = None) -> date:
    holidays = holidays if holidays is not None else load_holidays()
    day -= timedelta(days=1)
    while not is_business_day(day, holidays):
        day -= timedelta(days=1)
    return day


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def assess_heartbeat(state: dict, *, now: datetime | None = None,
                     stale_minutes: float = 60.0,
                     holidays: set[date] | None = None) -> tuple[bool, str]:
    """Heartbeat is the primary health signal.

    Cron runs every day, so a successful *empty* fetch still refreshes the
    heartbeat.  Weekends/holidays therefore do not excuse a dead collector;
    they only explain why ``published_date`` may be old (secondary info).
    """
    now = now or datetime.now(timezone.utc)
    holidays = holidays if holidays is not None else load_holidays()
    local_day = now.astimezone(MYT).date()
    closed = "" if is_business_day(local_day, holidays) else " Market closed today."

    last_run = state.get("last_run") or {}
    if last_run.get("outcome") == "failed":
        return False, (f"last run failed at {state.get('last_run_at')}: "
                       f"{last_run.get('error') or 'page 1 unavailable'}.{closed}")

    success = parse_utc(state.get("last_success_at"))
    if success is None:
        return False, f"no successful page-1 collection recorded yet.{closed}"
    age_min = (now - success).total_seconds() / 60.0
    if age_min < -5:
        return False, f"last_success_at is in the future ({state.get('last_success_at')})."
    if age_min > stale_minutes:
        return False, (f"last successful collection was {age_min:.0f} min ago "
                       f"(threshold {stale_minutes:.0f} min).{closed}")
    return True, (f"last successful collection {age_min:.0f} min ago "
                  f"({state.get('last_success_at')}).{closed}")


def describe_last_run(state: dict) -> str:
    run = state.get("last_run")
    if not isinstance(run, dict):
        return "no collector run recorded yet"
    pages = ",".join(str(p) for p in run.get("pages_ok", [])) or "none"
    return (f"{run.get('outcome', 'unknown')} at {state.get('last_run_at')}; "
            f"page1={'ok' if run.get('page1_ok') else 'FAILED'} "
            f"attempts={run.get('page1_attempts', 0)}; "
            f"requests={run.get('requests', 0)}; pages_ok={pages}; "
            f"parsed={run.get('parsed', 0)}; new={run.get('new_rows', 0)}")
