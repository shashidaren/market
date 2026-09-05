"""
Categorization rules for Bursa Malaysia announcements.

Each rule maps a keyword pattern (regex, case-insensitive) to a
(category, subcategory, priority) tuple.

Priority scale:
    1 = HIGH   - real alpha signal (insider trades, earnings, contracts)
    2 = MEDIUM - meaningful but slower (dividends, corporate proposals)
    3 = LOW    - mostly noise (boardroom, ETF NAV, routine filings)

Rules are checked in order. First match wins. So put the most specific
patterns first.
"""

# ------------------------------------------------------------------
# Secrets: single source of truth is repo-root .env (/opt/market/.env).
# Module-local .env still accepted as migration fallback.
# ------------------------------------------------------------------
import os as _os
import sys as _sys
from pathlib import Path as _Path

_REPO_ROOT = _Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_REPO_ROOT))
from env_loader import load_env  # noqa: E402

load_env(local_dir=_Path(__file__).resolve().parent)


# Each entry: (regex_pattern, category, subcategory, priority)
CATEGORY_RULES = [
    # ═══════════════════════════════════════════════════════════════
    # PRIORITY 1 - HIGH VALUE (real trading signal)
    # ═══════════════════════════════════════════════════════════════

    # Insider trades - directors buying/selling their own company shares
    (r"Changes in Director'?s?\s+Interest.*Section 219",
     "INSIDER_TRADE", "DIRECTOR_S219", 1),

    # Substantial shareholder changes (5%+ owners buying/selling)
    (r"Changes in Sub\.?\s*S-hldr'?s?\s+Int.*Section 138",
     "INSIDER_TRADE", "SUBSTANTIAL_S138", 1),

    # Quarterly / annual earnings reports
    (r"Quarterly rpt|Quarterly Report|financial period ended",
     "EARNINGS", "QUARTERLY", 1),
    (r"Annual Report|Annual Audited",
     "EARNINGS", "ANNUAL", 1),

    # Material contracts / tenders won
    (r"AWARDED|LETTER OF AWARD|LOA|Contract Award",
     "CONTRACT", "AWARDED", 1),
    (r"MEMORANDUM OF UNDERSTANDING|MOU|Memorandum",
     "CONTRACT", "MOU", 1),
    (r"Joint Venture|JOINT VENTURE|JV Agreement",
     "CONTRACT", "JV", 1),

    # M&A activity
    (r"ACQUISITION|Proposed Acquisition",
     "MA_ACTIVITY", "ACQUISITION", 1),
    (r"DISPOSAL|Proposed Disposal",
     "MA_ACTIVITY", "DISPOSAL", 1),
    (r"MERGER|Take[- ]?over|TAKEOVER",
     "MA_ACTIVITY", "MERGER", 1),

    # Trading concerns
    (r"WINDING[- ]UP|RECEIVER|SPECIAL ADMINISTRATOR|JUDICIAL MANAGEMENT",
     "DISTRESS", "WINDING_UP", 1),
    (r"PN17|GN3|Practice Note 17|Guidance Note 3",
     "DISTRESS", "PN17_GN3", 1),
    (r"Profit Warning|profit warning",
     "DISTRESS", "PROFIT_WARNING", 1),

    # ═══════════════════════════════════════════════════════════════
    # PRIORITY 2 - MEDIUM VALUE
    # ═══════════════════════════════════════════════════════════════

    # Dividends & capital returns
    (r"Entitlement.*Dividend|Dividend.*Entitlement",
     "DIVIDEND", "ENTITLEMENT", 2),
    (r"Interim Dividend|Final Dividend|Special Dividend",
     "DIVIDEND", "DECLARED", 2),
    (r"Bonus Issue|BONUS ISSUE",
     "CAPITAL_RETURN", "BONUS", 2),
    (r"Share Buy[- ]?Back|share buyback",
     "CAPITAL_RETURN", "BUYBACK", 2),
    (r"Rights Issue|RIGHTS ISSUE",
     "CAPITAL_RETURN", "RIGHTS", 2),

    # Corporate proposals
    (r"Proposed.*Placement|Private Placement",
     "CORPORATE_ACTION", "PLACEMENT", 2),
    (r"Circular to Shareholders",
     "CORPORATE_ACTION", "CIRCULAR", 2),

    # General meetings
    (r"Extraordinary General Meeting|EGM",
     "MEETING", "EGM", 2),
    (r"Annual General Meeting|AGM",
     "MEETING", "AGM", 2),

    # Suspensions / halts
    (r"Suspension|SUSPENSION|Trading Halt",
     "TRADING", "SUSPENSION", 2),
    (r"Resumption of Trading",
     "TRADING", "RESUMPTION", 2),

    # Regulatory
    (r"DEALINGS IN LISTED SECURITIES.*Closed Period",
     "REGULATORY", "CLOSED_PERIOD", 2),
    (r"Reprimand|reprimand|Public Censure",
     "REGULATORY", "REPRIMAND", 2),

    # ═══════════════════════════════════════════════════════════════
    # PRIORITY 3 - LOW VALUE (mostly noise)
    # ═══════════════════════════════════════════════════════════════

    # Boardroom changes
    (r"Change in Boardroom",
     "GOVERNANCE", "BOARDROOM", 3),
    (r"Change in Audit Committee",
     "GOVERNANCE", "AUDIT_COMMITTEE", 3),
    (r"Change in.*Committee",
     "GOVERNANCE", "COMMITTEE", 3),
    (r"Change in Company Secretary",
     "GOVERNANCE", "SECRETARY", 3),
    (r"Change in Registered Address",
     "GOVERNANCE", "ADDRESS", 3),

    # ETF NAV updates (routine, high volume)
    (r"NET ASSET VALUE|NAV|Indicative Optimum Portfolio",
     "ROUTINE", "ETF_NAV", 3),

    # Notice / listing circulars
    (r"Notice of Book Closure",
     "ROUTINE", "BOOK_CLOSURE", 3),
    (r"Additional Listing|Listing of Additional",
     "ROUTINE", "ADDITIONAL_LISTING", 3),

    # Very generic - catch-all for regulatory reminders
    (r"REMINDER|Reminder to",
     "ROUTINE", "REMINDER", 3),
]


def categorize(title: str) -> tuple[str, str, int]:
    """
    Match a title against CATEGORY_RULES, return (category, subcategory, priority).
    Returns ('OTHER', 'UNCATEGORIZED', 3) if no rule matches.
    """
    import re
    for pattern, category, subcategory, priority in CATEGORY_RULES:
        if re.search(pattern, title, re.IGNORECASE):
            return category, subcategory, priority
    return "OTHER", "UNCATEGORIZED", 3
