"""
COMPATIBILITY SHIM — the data moved to JSON on 2026-11-01.

`data/bursa_tickers.json` is now the single source of truth (one record per
company: code, name, legal_name, sector, aliases). `tickers.py` loads it,
derives the alias variants ("Malayan Banking Bhd" → "Malayan Banking", the
stock code, …) and exposes the matching/validation/CLI tooling.

This module only exists so existing imports keep working:

    from tickers_config import BURSA_TICKERS, MACRO_KEYWORDS   # legacy shape

New code should use:

    from tickers import get_registry
    registry = get_registry()
    relevance, sector, names = registry.classify(headline)

What used to be here (kept in git history if you ever need it) was a
~820-line hand-maintained dict where one company needed four entries:
`"Maybank"`, `"Malayan Banking"`, `"Malayan Banking Bhd"` and `"1155"`.

Maintenance now:
    python3 tickers.py check        # validate
    python3 tickers.py suggest      # names in real headlines we are missing
    python3 tickers.py add ...      # add a listing
"""

from tickers import get_registry

_registry = get_registry()

# alias -> (canonical_name, sector) — the exact shape the scorer expects.
BURSA_TICKERS: dict[str, tuple[str, str]] = dict(_registry.aliases)

# macro keyword -> sector
MACRO_KEYWORDS: dict[str, str] = dict(_registry.macro)

__all__ = ["BURSA_TICKERS", "MACRO_KEYWORDS"]
