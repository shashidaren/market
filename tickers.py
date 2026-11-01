#!/usr/bin/env python3
"""
Bursa company registry — ONE maintainable file, not a 800-line Python dict.

The old `tickers_config.py` held every alias as its own line:

    "Maybank":                ("Maybank", "Banking"),
    "Malayan Banking":        ("Maybank", "Banking"),
    "Malayan Banking Bhd":    ("Maybank", "Banking"),
    "1155":                   ("Maybank", "Banking"),

Four hand-typed entries for one company. Add a listing and you had to
remember which phrasings to duplicate; forget one and headlines silently
fell through to "noise".

Now each company is ONE record in `data/bursa_tickers.json`:

    {
      "code": "1155",
      "name": "Maybank",
      "legal_name": "Malayan Banking Bhd",
      "sector": "Banking"
    }

and the matcher derives the aliases itself (name, legal name, legal name
without "Bhd"/"Berhad"/…, the stock code). Nicknames that cannot be
derived ("CIMB Group", "HLFG", "KLCI") stay in an `aliases` list.

Why JSON and not a spreadsheet: git shows a readable one-line-per-field
diff, so an edit is reviewable and revertible, and the dashboard, scorer
and Telegram jobs all read the same file with no import order games.

Everyday use
------------
    python3 tickers.py check                 # validate + alias statistics
    python3 tickers.py match "Maybank, CIMB up on OPR hold"
    python3 tickers.py suggest --days 7      # names in real headlines we miss
    python3 tickers.py add --code 8869 --name "Press Metal" \\
        --legal-name "Press Metal Aluminium Holdings Bhd" --sector Materials
    python3 tickers.py alias --code 8869 --add "PMetal"
    python3 tickers.py list --search press

The web dashboard's new "top mover" tab can also show registry health via
`python3 tickers.py check --json`.

Compatibility: `tickers_config.py` re-exports BURSA_TICKERS and
MACRO_KEYWORDS built from this registry, so older code keeps working.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
from dataclasses import dataclass, field
from difflib import get_close_matches
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("MARKET_DATA_DIR", _REPO_ROOT / "data"))
TICKERS_PATH = DATA_DIR / "bursa_tickers.json"
MACRO_PATH = DATA_DIR / "macro_keywords.json"

SCHEMA_VERSION = 1

# Trailing tokens that carry no identifying information. Stripped from a
# legal name to derive the phrasings that actually appear in headlines.
LEGAL_SUFFIXES = (
    "bhd", "berhad", "sdn", "sdn bhd", "ltd", "limited", "plc", "inc",
    "incorporated", "corp", "corporation", "pte ltd", "co",
)

# Bursa codes are four digits (main/ACE market). LEAP codes are also four
# digits but live in their own number ranges — we never enforce a range.
_CODE_RE = re.compile(r"^\d{4}$")

# Aliases this short are prone to false positives even with \b boundaries
# ("TM", "F&N", "IJM"). Kept, but reported by `check` so they can be
# reviewed in context.
SHORT_ALIAS_LEN = 3

# The sector vocabulary. Kept deliberately small and explicit: a typo like
# "Techn" or "OIlGas" silently splits a sector in the dashboard filters, so
# `check` flags anything outside this list instead of accepting it.
# Adding a genuinely new sector is a one-line edit here.
KNOWN_SECTORS = (
    "Aviation", "Banking", "Conglomerate", "Construction", "Consumer",
    "Financial", "GLC", "Gloves", "Healthcare", "Industrial", "Insurance",
    "Macro", "Materials", "OilGas", "Plantation", "Property", "REITs",
    "Tech", "Telco", "Transport", "Utilities", "Auto",
)


# ─── Data model ────────────────────────────────────────────────────

@dataclass
class Company:
    name: str
    sector: str
    code: str | None = None
    legal_name: str | None = None
    aliases: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict) -> "Company":
        return cls(
            name=str(raw.get("name") or "").strip(),
            sector=str(raw.get("sector") or "").strip(),
            code=(str(raw["code"]).strip() if raw.get("code") not in (None, "") else None),
            legal_name=(str(raw["legal_name"]).strip()
                        if raw.get("legal_name") not in (None, "") else None),
            aliases=[str(a).strip() for a in (raw.get("aliases") or []) if str(a).strip()],
        )

    def to_dict(self) -> dict:
        out: dict = {}
        if self.code:
            out["code"] = self.code
        out["name"] = self.name
        if self.legal_name:
            out["legal_name"] = self.legal_name
        out["sector"] = self.sector
        if self.aliases:
            out["aliases"] = list(self.aliases)
        return out

    @property
    def label(self) -> str:
        return f"{self.code} {self.name}" if self.code else self.name


def _strip_legal(name: str) -> str:
    """'Malayan Banking Bhd' -> 'Malayan Banking'. Repeats: 'Foo Sdn Bhd'."""
    out = re.sub(r"[\s,]+", " ", name).strip(" ,.")
    changed = True
    while changed:
        changed = False
        low = out.lower()
        for suffix in LEGAL_SUFFIXES:
            if low.endswith(" " + suffix):
                out = out[: -(len(suffix) + 1)].strip(" ,.")
                changed = True
                break
    return out


def derive_aliases(company: Company) -> list[str]:
    """
    Every phrasing we can work out mechanically. Order matters only for
    readability: the display name wins ties, the code comes last.

    Deliberately NOT derived: dropping "Holdings"/"Group" (too ambiguous —
    "Hong Leong" is three different listed companies) and splitting a name
    into its first token ("Public" is not Public Bank).
    """
    out: list[str] = []

    def add(value: str | None) -> None:
        value = (value or "").strip()
        if not value:
            return
        if value.lower() not in {a.lower() for a in out}:
            out.append(value)

    add(company.name)
    add(_strip_legal(company.name))
    if company.legal_name:
        add(company.legal_name)
        add(_strip_legal(company.legal_name))
    for alias in company.aliases:
        add(alias)
    if company.code:
        add(company.code)
    return out


@dataclass
class Issue:
    level: str          # 'error' | 'warning' | 'info'
    where: str
    message: str

    def as_dict(self) -> dict:
        return {"level": self.level, "where": self.where, "message": self.message}


# ─── Registry ──────────────────────────────────────────────────────

class Registry:
    """
    Loads the JSON data files and exposes the lookups the rest of the repo
    needs. Construct with an explicit path in tests; use `get_registry()`
    everywhere else so the files are parsed once per process.
    """

    def __init__(self, companies: list[Company], macro: dict[str, list[str]],
                 tickers_path: Path | None = None, macro_path: Path | None = None):
        self.companies = companies
        self.macro_sectors = macro
        self.tickers_path = tickers_path
        self.macro_path = macro_path

        # alias -> (canonical, sector) for the aliases we trust.
        self.aliases: dict[str, tuple[str, str]] = {}
        # alias_lower -> companies claiming it (used for ambiguity checks).
        self._claims: dict[str, list[Company]] = {}
        # alias_lower -> [canonical, ...] when more than one company claims it.
        self.ambiguous: dict[str, list[str]] = {}
        self.derived: dict[str, list[str]] = {}      # canonical -> aliases
        self._patterns: list[tuple[re.Pattern, str, str]] = []
        self._macro_patterns: list[tuple[re.Pattern, str, str]] = []

        for company in companies:
            if not company.name or not company.sector:
                continue
            names = derive_aliases(company)
            self.derived[company.name] = names
            for alias in names:
                self._claims.setdefault(alias.lower(), []).append(company)

        for alias_lower, claimants in self._claims.items():
            canonicals = sorted({c.name for c in claimants})
            if len(canonicals) > 1:
                self.ambiguous[alias_lower] = canonicals

        # Build the lookup keeping the original spelling of the first
        # company that used an alias.
        for company in companies:
            for alias in self.derived.get(company.name, []):
                if alias.lower() in self.ambiguous:
                    continue
                self.aliases.setdefault(alias, (company.name, company.sector))

        self.macro: dict[str, str] = {}
        self.macro_ambiguous: dict[str, list[str]] = {}
        claims: dict[str, list[str]] = {}
        for sector, terms in macro.items():
            for term in terms:
                claims.setdefault(term.lower(), []).append(sector)
        for sector, terms in macro.items():
            for term in terms:
                if len(set(claims[term.lower()])) > 1:
                    self.macro_ambiguous[term] = sorted(set(claims[term.lower()]))
                    continue
                self.macro.setdefault(term, sector)

        self._compile()

    # -- matching ---------------------------------------------------
    def _compile(self) -> None:
        # Longest aliases first: a headline containing "Public Bank Berhad"
        # should attribute to that alias, and we want deterministic output
        # order regardless of insertion quirks.
        for alias in sorted(self.aliases, key=lambda a: (-len(a), a.lower())):
            self._patterns.append((
                re.compile(r"\b" + re.escape(alias) + r"\b", re.IGNORECASE),
                self.aliases[alias][0],
                self.aliases[alias][1],
            ))
        self._macro_patterns = [
            (re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE), term, sector)
            for term, sector in sorted(self.macro.items(),
                                       key=lambda kv: (-len(kv[0]), kv[0].lower()))
        ]

    def match(self, text: str) -> dict[str, str]:
        """{canonical_name: sector} for every company mentioned in `text`."""
        found: dict[str, str] = {}
        for pattern, canonical, sector in self._patterns:
            if canonical not in found and pattern.search(text or ""):
                found[canonical] = sector
        return found

    def matched_aliases(self, text: str) -> list[str]:
        return [canonical for canonical, _ in self._alias_hits(text)]

    def _alias_hits(self, text: str) -> list[tuple[str, str]]:
        hits = []
        for pattern, canonical, sector in self._patterns:
            if pattern.search(text or ""):
                hits.append((canonical, sector))
        return hits

    def match_macro(self, text: str) -> tuple[str, str] | None:
        for pattern, term, sector in self._macro_patterns:
            if pattern.search(text or ""):
                return term, sector
        return None

    def classify(self, text: str) -> tuple[str, str | None, list[str]]:
        """
        Drop-in replacement for scorer.classify_relevance_and_sector().
        Returns (relevance, sector, [canonical names]).
        """
        found = self.match(text)
        if found:
            first = next(iter(found.items()))
            return "malaysia_direct", first[1], list(found)
        macro = self.match_macro(text)
        if macro:
            return "macro_indirect", macro[1], []
        return "noise", None, []

    # -- introspection ----------------------------------------------
    def company_by_code(self, code: str) -> Company | None:
        code = str(code).strip()
        return next((c for c in self.companies if c.code == code), None)

    def company_by_name(self, name: str) -> Company | None:
        low = (name or "").strip().lower()
        return next((c for c in self.companies if c.name.lower() == low), None)

    def sectors(self) -> list[str]:
        return sorted({c.sector for c in self.companies})

    def validate(self) -> list[Issue]:
        issues: list[Issue] = []
        known_sectors = set(KNOWN_SECTORS) | {c.sector for c in self.companies}
        known_lower = {s.lower(): s for s in known_sectors}

        seen_codes: dict[str, Company] = {}
        seen_names: dict[str, Company] = {}
        for c in self.companies:
            where = c.label
            if not c.name:
                issues.append(Issue("error", "(unnamed)", "company has no name"))
                continue
            if not c.sector:
                issues.append(Issue("error", where, "company has no sector"))
            if c.name.lower() in seen_names:
                issues.append(Issue("error", where, "duplicate name"))
            seen_names[c.name.lower()] = c

            if c.code is not None:
                if not _CODE_RE.match(c.code):
                    issues.append(Issue("warning", where,
                                        f"code {c.code!r} is not four digits"))
                if c.code in seen_codes:
                    issues.append(Issue("error", where,
                                        f"code also used by {seen_codes[c.code].name}"))
                seen_codes[c.code] = c

            if c.sector and c.sector not in KNOWN_SECTORS:
                suggestions = get_close_matches(c.sector, sorted(KNOWN_SECTORS), n=1)
                hint = (f" — did you mean {suggestions[0]!r}?" if suggestions
                        else " — add it to KNOWN_SECTORS in tickers.py if intended")
                issues.append(Issue("warning", where,
                                    f"unknown sector {c.sector!r}{hint}"))

            if not c.code:
                issues.append(Issue("info", where,
                                    "no Bursa code (foreign/macro name — fine)"))

        for alias, owners in sorted(self.ambiguous.items()):
            issues.append(Issue("warning", alias,
                                "alias claimed by several companies, ignored: "
                                + ", ".join(owners)))
        for alias, sectors in sorted(self.macro_ambiguous.items()):
            issues.append(Issue("warning", alias,
                                "macro keyword has several sectors, ignored: "
                                + ", ".join(sectors)))
        for alias in sorted(self.aliases):
            if not alias.isdigit() and len(alias) <= SHORT_ALIAS_LEN:
                issues.append(Issue("info", alias,
                                    "short alias — check it in real headlines "
                                    "(`tickers.py match`) before trusting it"))
        return issues

    def stats(self) -> dict:
        explicit = {a.lower() for c in self.companies for a in c.aliases}
        return {
            "companies": len(self.companies),
            "with_code": sum(1 for c in self.companies if c.code),
            "without_code": sum(1 for c in self.companies if not c.code),
            "aliases": len(self.aliases),
            "explicit_aliases": sum(1 for a in self.aliases if a.lower() in explicit),
            "generated_aliases": sum(
                1 for a in self.aliases if a.lower() not in explicit
            ),
            "ambiguous_aliases": len(self.ambiguous),
            "macro_terms": len(self.macro),
            "sectors": len({c.sector for c in self.companies}),
        }


# ─── Loading / saving ──────────────────────────────────────────────

def load_registry(tickers_path: Path | str | None = None,
                  macro_path: Path | str | None = None) -> Registry:
    tickers_path = Path(tickers_path or TICKERS_PATH)
    macro_path = Path(macro_path or MACRO_PATH)

    raw = json.loads(tickers_path.read_text(encoding="utf-8"))
    companies = [Company.from_dict(c) for c in raw.get("companies", [])]
    macro_doc = json.loads(macro_path.read_text(encoding="utf-8")) if macro_path.exists() else {}
    macro = {sector: list(terms) for sector, terms in (macro_doc.get("sectors") or {}).items()}
    return Registry(companies, macro, tickers_path, macro_path)


_REGISTRY: Registry | None = None


def get_registry() -> Registry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = load_registry()
    return _REGISTRY


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def save_companies(companies: list[Company], path: Path | str | None = None) -> None:
    """Write companies back, keeping a stable order and the file's metadata."""
    path = Path(path or TICKERS_PATH)
    payload: dict = {"schema": SCHEMA_VERSION}
    if path.exists():
        old = json.loads(path.read_text(encoding="utf-8"))
        for key in ("about", "updated"):
            if key in old:
                payload[key] = old[key]
    payload.setdefault("about", (
        "ONE entry per company. 'code' = Bursa stock code (null for foreign/macro "
        "companies tracked because they move Malaysian headlines). 'name' is the "
        "display name; 'legal_name' is optional and expands automatically. "
        "'aliases' are extra phrasings seen in real headlines."
    ))
    payload["updated"] = _today()
    payload["companies"] = [
        c.to_dict() for c in sorted(
            companies, key=lambda c: (c.code is None, c.code or "", c.name.lower())
        )
    ]
    _atomic_write(path, payload)


def _today() -> str:
    from datetime import date
    return date.today().isoformat()


# ─── Suggestion engine (find names we are missing) ─────────────────

_PHRASE_RE = re.compile(r"\b[A-Z][A-Za-z&.'-]+(?:\s+[A-Z][A-Za-z&.'-]+){1,4}\b")

# Phrases that are overwhelmingly NOT company names.
_STOP_PHRASES = {
    "wall street", "federal reserve", "prime minister", "united states",
    "new york", "hong kong", "kuala lumpur", "southeast asia", "bank negara",
    "bursa malaysia", "petaling jaya", "sri lanka", "south china",
    "straits times", "the edge", "star media",
}


def suggest_names(db_path: Path | str, days: int = 7, limit: int = 20,
                  registry: Registry | None = None) -> list[dict]:
    """
    Candidate company names harvested from headlines the scorer called
    'noise'. This is the maintenance shortcut: run it weekly, add the real
    ones, ignore the rest.
    """
    registry = registry or get_registry()
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"{db_path} not found — run the collector first")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT r.title, r.snippet, COALESCE(r.published_at, r.collected_at) AS seen
            FROM raw_headlines r
            LEFT JOIN scored_headlines s ON s.id = r.id
            WHERE (s.id IS NULL OR s.relevance = 'noise')
              AND COALESCE(r.published_at, r.collected_at) >= datetime('now', ?)
            ORDER BY seen DESC
            LIMIT 5000
            """,
            (f"-{int(days)} days",),
        ).fetchall()
    finally:
        conn.close()

    counts: dict[str, int] = {}
    examples: dict[str, str] = {}
    for row in rows:
        text = f"{row['title']} {row['snippet'] or ''}"
        if registry.match(text):
            continue
        for phrase in _PHRASE_RE.findall(row["title"] or ""):
            phrase = phrase.strip(" .,'")
            if len(phrase) < 5 or phrase.lower() in _STOP_PHRASES:
                continue
            # All-caps phrases are usually acronyms we may still want
            # ("MARC", "RAM"), keep them but rank by frequency.
            counts[phrase] = counts.get(phrase, 0) + 1
            examples.setdefault(phrase, row["title"])

    out = [
        {"name": name, "count": count, "example": examples[name]}
        for name, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))
    ]
    return out[: max(1, int(limit))]


# ─── CLI ───────────────────────────────────────────────────────────

def _cmd_check(args) -> int:
    registry = load_registry(args.data_dir / "bursa_tickers.json" if args.data_dir else None,
                             args.data_dir / "macro_keywords.json" if args.data_dir else None)
    issues = registry.validate()
    stats = registry.stats()
    if args.json:
        print(json.dumps({"stats": stats,
                          "issues": [i.as_dict() for i in issues]}, indent=2))
    elif args.brief:
        print(f"[ OK ] company registry — {stats['companies']} companies, "
              f"{stats['aliases']} aliases, {stats['macro_terms']} macro keywords")
    else:
        print(f"registry: {registry.tickers_path}")
        print(f"  {stats['companies']} companies ({stats['with_code']} with a Bursa code, "
              f"{stats['without_code']} foreign/macro)")
        print(f"  {stats['aliases']} usable aliases "
              f"({stats['generated_aliases']} derived from names/codes, "
              f"{stats['ambiguous_aliases']} ambiguous ignored)")
        print(f"  {stats['macro_terms']} macro keywords across {stats['sectors']} sectors")
        for level in ("error", "warning"):
            group = [i for i in issues if i.level == level]
            if not group:
                continue
            print(f"\n{level.upper()} ({len(group)}):")
            for issue in group[: args.max_issues]:
                print(f"  - {issue.where}: {issue.message}")
            if len(group) > args.max_issues:
                print(f"  … {len(group) - args.max_issues} more (raise --max-issues)")
        infos = [i for i in issues if i.level == "info"]
        if infos and args.verbose:
            print(f"\nINFO ({len(infos)}):")
            for issue in infos[: args.max_issues]:
                print(f"  - {issue.where}: {issue.message}")
            infos = infos[args.max_issues:]
        if infos and not args.verbose:
            print(f"\n{len(infos)} informational note(s) — rerun with --verbose to see them")
        if not [i for i in issues if i.level == "error"]:
            print("\n[ OK ] registry is valid")
    errors = [i for i in issues if i.level == "error"]
    warnings = [i for i in issues if i.level == "warning"]
    if errors or (args.strict and warnings):
        return 1
    return 0


def _cmd_list(args) -> int:
    registry = get_registry()
    rows = registry.companies
    if args.sector:
        rows = [c for c in rows if c.sector.lower() == args.sector.lower()]
    if args.search:
        needle = args.search.lower()
        rows = [c for c in rows if needle in (c.name + " " + (c.code or "") + " "
                                              + (c.legal_name or "") + " "
                                              + " ".join(c.aliases)).lower()]
    if args.json:
        print(json.dumps([{**c.to_dict(),
                           "derived_aliases": registry.derived.get(c.name, [])}
                          for c in rows], indent=2))
        return 0
    for c in rows:
        aliases = [a for a in registry.derived.get(c.name, []) if a != c.name]
        print(f"{c.code or '----':<5} {c.name:<32} {c.sector:<14} "
              f"{', '.join(aliases)}")
    print(f"\n{len(rows)} compan{'y' if len(rows) == 1 else 'ies'}")
    return 0


def _cmd_match(args) -> int:
    registry = get_registry()
    text = " ".join(args.text)
    found = registry.match(text)
    if args.json:
        print(json.dumps({
            "relevance": registry.classify(text)[0],
            "matches": found,
            "ambiguity": {k: v for k, v in registry.ambiguous.items()
                          if re.search(r"\b" + re.escape(k) + r"\b", text, re.IGNORECASE)},
        }, indent=2))
        return 0
    print(f"text: {text}")
    if found:
        for canonical, sector in found.items():
            company = registry.company_by_name(canonical)
            print(f"  [OK] {canonical} ({sector})"
                  f"{'  code ' + company.code if company and company.code else ''}")
    else:
        print("  no company matched")
    macro = registry.match_macro(text)
    if macro:
        print(f"  [MACRO] {macro[0]} → {macro[1]}")
    for alias, owners in registry.ambiguous.items():
        if re.search(r"\b" + re.escape(alias) + r"\b", text, re.IGNORECASE):
            print(f"  [AMB] {alias!r} is claimed by {', '.join(owners)} — ignored")
    print(f"  → classify(): {registry.classify(text)}")
    return 0


def _cmd_add(args) -> int:
    tickers_path = (args.data_dir / "bursa_tickers.json") if args.data_dir else None
    path = Path(tickers_path or TICKERS_PATH)
    raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    companies = [Company.from_dict(c) for c in raw.get("companies", [])]

    target = None
    if args.code:
        target = next((c for c in companies if c.code == args.code), None)
    if target is None:
        target = next((c for c in companies if c.name.lower() == args.name.lower()), None)
    if target is None:
        if not args.sector:
            print("[FAIL] --sector is required when adding a new company "
                  f"(known sectors: {', '.join(sorted({c.sector for c in companies}))})",
                  file=sys.stderr)
            return 2
        target = Company(name=args.name, sector=args.sector)
        companies.append(target)
    if args.sector:
        target.sector = args.sector
    if args.legal_name:
        target.legal_name = args.legal_name
    if args.code:
        target.code = args.code
    for alias in args.alias or []:
        if alias.lower() not in {a.lower() for a in target.aliases} \
                and alias.lower() != target.name.lower():
            target.aliases.append(alias)

    save_companies(companies, path)
    registry = load_registry(path, path.parent / "macro_keywords.json")
    derived = registry.derived.get(target.name, [])
    print(f"saved {target.label} ({target.sector}) to {path}")
    print(f"  aliases: {', '.join(derived)}")
    clashes = [a for a in derived if a.lower() in registry.ambiguous]
    if clashes:
        print(f"  [WARN] ignored because another company claims them: {', '.join(clashes)}")
    return 0


def _cmd_alias(args) -> int:
    path = Path((args.data_dir / "bursa_tickers.json") if args.data_dir else TICKERS_PATH)
    raw = json.loads(path.read_text(encoding="utf-8"))
    companies = [Company.from_dict(c) for c in raw.get("companies", [])]
    target = next((c for c in companies if c.code == args.code), None) if args.code else None
    if target is None and args.name:
        target = next((c for c in companies if c.name.lower() == args.name.lower()), None)
    if target is None:
        print(f"no company with code {args.code!r} / name {args.name!r}", file=sys.stderr)
        return 1
    for alias in args.add or []:
        if alias.lower() not in {a.lower() for a in target.aliases}:
            target.aliases.append(alias)
    for alias in args.remove or []:
        target.aliases = [a for a in target.aliases if a.lower() != alias.lower()]
    save_companies(companies, path)
    print(f"saved {target.label}: aliases now {', '.join(target.aliases) or '(none)'}")
    return 0


def _cmd_suggest(args) -> int:
    db = Path(args.db or _REPO_ROOT / "news.db")
    try:
        rows = suggest_names(db, days=args.days, limit=args.limit)
    except FileNotFoundError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("no candidates — either the registry already covers the headlines, "
              "or the scorer has not run yet")
        return 0
    print(f"{len(rows)} name(s) in recent unexplained headlines "
          f"(last {args.days}d) — add the real ones:\n")
    for row in rows:
        print(f"  {row['count']:>3}x  {row['name']}")
        print(f"        e.g. {row['example'][:110]}")
    print("\nAdd with:  python3 tickers.py add --code 1234 --name \"...\" "
          "--legal-name \"... Bhd\" --sector <sector>")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bursa company registry: validate, search, add, and find gaps.")
    parser.add_argument("--data-dir", type=Path, default=None,
                        help="directory holding bursa_tickers.json / macro_keywords.json")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="validate the registry and print statistics")
    check.add_argument("--strict", action="store_true", help="exit 1 on warnings too")
    check.add_argument("--json", action="store_true")
    check.add_argument("--brief", action="store_true", help="one line, for check_all.sh")
    check.add_argument("--verbose", action="store_true", help="also list informational notes")
    check.add_argument("--max-issues", type=int, default=25,
                       help="0 = print every issue")
    check.set_defaults(fn=_cmd_check)

    ls = sub.add_parser("list", help="list companies and their derived aliases")
    ls.add_argument("--sector")
    ls.add_argument("--search")
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(fn=_cmd_list)

    match = sub.add_parser("match", help="show what a headline matches")
    match.add_argument("text", nargs="+")
    match.add_argument("--json", action="store_true")
    match.set_defaults(fn=_cmd_match)

    add = sub.add_parser("add", help="add or update one company")
    add.add_argument("--code", help="Bursa stock code (omit for foreign names)")
    add.add_argument("--name", required=True, help="display name, e.g. 'Press Metal'")
    add.add_argument("--legal-name", help="e.g. 'Press Metal Aluminium Holdings Bhd'")
    add.add_argument("--sector", default="", help="e.g. Materials")
    add.add_argument("--alias", action="append", help="extra phrasing (repeatable)")
    add.set_defaults(fn=_cmd_add)

    alias = sub.add_parser("alias", help="add/remove aliases for an existing company")
    alias.add_argument("--code")
    alias.add_argument("--name")
    alias.add_argument("--add", action="append")
    alias.add_argument("--remove", action="append")
    alias.set_defaults(fn=_cmd_alias)

    suggest = sub.add_parser("suggest", help="names in recent noise headlines we miss")
    suggest.add_argument("--db", help="path to news.db (default: repo news.db)")
    suggest.add_argument("--days", type=int, default=7)
    suggest.add_argument("--limit", type=int, default=20)
    suggest.add_argument("--json", action="store_true")
    suggest.set_defaults(fn=_cmd_suggest)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "max_issues", 1) == 0:
        args.max_issues = 10 ** 9
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
