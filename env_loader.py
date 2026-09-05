#!/usr/bin/env python3
"""
Shared environment loader for the market stack.

Single source of truth on the server:

    /opt/market/.env

(repo-root `.env` relative to this file when developing elsewhere).

Rules:
  - Values already present in the process environment always win
    (cron / systemd / manual export take precedence).
  - Root `.env` is loaded next via setdefault.
  - Optional module-local `.env` (fx_signal/.env etc.) is loaded last
    as a migration fallback only — prefer putting everything in the root
    file going forward.

Usage (Python):

    from env_loader import load_env
    load_env()                    # root + optional local next to caller
    load_env(local_dir="/path")   # also try /path/.env

Shell / cron:

    BASH_ENV=/opt/market/.env /opt/market/fx_signal/run_pipeline.sh

Never commit real secrets. Templates live in `.env.example` files.
"""

from __future__ import annotations

import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent
_LOADED = False


def _parse_and_apply(path: Path) -> int:
    """Read KEY=VALUE lines; setdefault so existing env wins. Returns count applied."""
    if not path.is_file():
        return 0
    applied = 0
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if not key:
            continue
        val = val.strip().strip("'\"")
        if key not in os.environ:
            os.environ[key] = val
            applied += 1
    return applied


def load_env(local_dir: str | Path | None = None, *, force: bool = False) -> Path:
    """
    Load environment variables from the central root `.env`.

    Returns the path of the root `.env` that was considered (may not exist).
    Idempotent within a process unless force=True.
    """
    global _LOADED
    root_env = _REPO_ROOT / ".env"

    if _LOADED and not force:
        return root_env

    _parse_and_apply(root_env)

    if local_dir is not None:
        local = Path(local_dir) / ".env"
        if local.resolve() != root_env.resolve():
            _parse_and_apply(local)

    _LOADED = True
    return root_env


def root_env_path() -> Path:
    """Absolute path to the canonical secrets file."""
    return _REPO_ROOT / ".env"


if __name__ == "__main__":
    p = load_env(force=True)
    keys = sorted(
        k
        for k in os.environ
        if k
        in (
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHAT_ID",
            "FINNHUB_API_KEY",
            "SIGNAL_MODE",
            "IREPORT_FILTER",
        )
    )
    print(f"root .env: {p}  exists={p.is_file()}")
    for k in keys:
        v = os.environ.get(k, "")
        shown = (v[:4] + "…" + v[-4:]) if len(v) > 12 else ("(set)" if v else "(empty)")
        print(f"  {k}={shown}")
