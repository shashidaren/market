#!/usr/bin/env python3
"""
i_report filter bridge for announcements_signal.

Runs a Bursa stock through the i_report intelligence engine (technical,
volume, support/resistance, risk → final score → decision) WITHOUT
generating a report file, so it has no dependency on i_report's
settings/.env or report directories.

Used by telegram_bot.py to decide whether an insider alert is worth
sending:

    STRONG_BUY / BUY / WATCH_BREAKOUT  → alert passes
    anything else                      → alert dropped (unless the stock
                                         is in portfolio.txt)

Fail-open by design: if analysis fails (Yahoo down, unknown symbol,
missing deps), analyze_stock() returns None and the caller should send
the alert anyway rather than silently losing signal.
"""

import logging
import sys
from pathlib import Path

log = logging.getLogger("ireport_filter")

# i_report lives next to announcements_signal in the repo
_IREPORT_DIR = Path(__file__).resolve().parent.parent / "i_report"

# Decisions considered actionable enough to alert on
ALLOWED_DECISIONS = {"STRONG_BUY", "BUY", "WATCH_BREAKOUT"}

_engine_loaded = False
_load_error = None


def _load_engine():
    """Import i_report analysis modules lazily (heavy: pandas/yfinance)."""
    global _engine_loaded, _load_error
    global YahooProvider, analyze_technical, analyze_volume
    global calculate_levels, analyze_risk, calculate_final_score, make_decision

    if _engine_loaded:
        return True
    if _load_error is not None:
        return False

    try:
        if str(_IREPORT_DIR) not in sys.path:
            sys.path.insert(0, str(_IREPORT_DIR))

        from app.providers.yahoo import YahooProvider
        from app.analysis.technical import analyze_technical
        from app.analysis.volume import analyze_volume
        from app.analysis.support_resistance import calculate_levels
        from app.analysis.risk import analyze_risk
        from app.engine.scoring import calculate_final_score
        from app.engine.decision import make_decision

        _engine_loaded = True
        return True
    except Exception as e:  # pragma: no cover
        _load_error = e
        log.error("i_report engine unavailable: %s", e)
        return False


def analyze_stock(stock_code: str):
    """
    Run i_report analysis for a Bursa stock code (e.g. "5323").

    Returns a dict:
        {score, decision, confidence, price, trade_support, trade_resistance}
    or None if analysis is not possible (caller should fail open).
    """
    if not _load_engine():
        return None

    try:
        provider = YahooProvider()
        snapshot = provider.get_market_data(stock_code)
        df = snapshot.dataframe

        technical = analyze_technical(df)
        volume = analyze_volume(df)
        levels = calculate_levels(df)
        risk = analyze_risk(
            price=snapshot.price,
            trade_support=levels["trade_support"],
            trade_resistance=levels["trade_resistance"],
            major_support=levels["major_support"],
            major_resistance=levels["major_resistance"],
        )

        # Pullback risk/reward — mirrors i_report orchestrator logic
        pullback_rr_t1 = None
        pullback_rr_t2 = None
        ts, tr = levels.get("trade_support"), levels.get("trade_resistance")
        mr, atr = levels.get("major_resistance"), levels.get("atr")
        if None not in (ts, tr, mr, atr) and atr > 0:
            pullback_entry = ts + atr * 0.5
            pullback_stop = ts - atr
            pullback_risk = pullback_entry - pullback_stop
            if pullback_risk > 0:
                if tr > pullback_entry:
                    pullback_rr_t1 = (tr - pullback_entry) / pullback_risk
                if mr > pullback_entry:
                    pullback_rr_t2 = (mr - pullback_entry) / pullback_risk

        results = {"technical": technical, "volume": volume, "risk": risk}
        final_score = calculate_final_score(results)
        decision = make_decision(
            score=final_score,
            technical=technical,
            volume=volume,
            risk=risk,
            pullback_rr_t1=pullback_rr_t1,
            pullback_rr_t2=pullback_rr_t2,
        )

        return {
            "score": final_score,
            "decision": decision.get("decision"),
            "confidence": decision.get("confidence"),
            "price": snapshot.price,
            "trade_support": levels.get("trade_support"),
            "trade_resistance": levels.get("trade_resistance"),
        }
    except Exception as e:
        log.warning("i_report analysis failed for %s: %s", stock_code, e)
        return None


def load_portfolio(path=None):
    """
    Read portfolio.txt (one stock code per line, # comments allowed).
    Stocks listed there BYPASS the filter — you always want to know about
    insider changes in stocks you own.
    """
    p = Path(path) if path else Path(__file__).parent / "portfolio.txt"
    if not p.exists():
        return set()
    codes = set()
    for line in p.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            codes.add(line.upper())
    return codes


if __name__ == "__main__":
    # Manual test: python3 ireport_filter.py 5323
    logging.basicConfig(level=logging.INFO)
    code = sys.argv[1] if len(sys.argv) > 1 else "5323"
    print(f"portfolio: {sorted(load_portfolio()) or '(empty / no portfolio.txt)'}")
    res = analyze_stock(code)
    if res is None:
        print(f"{code}: analysis unavailable (alert would FAIL OPEN → sent)")
    else:
        passes = res["decision"] in ALLOWED_DECISIONS
        print(f"{code}: score={res['score']} decision={res['decision']} "
              f"confidence={res['confidence']} → "
              + ("PASSES filter ✅" if passes else "FILTERED OUT ❌"))
