from app.providers.yahoo import YahooProvider

from app.analysis.technical import analyze_technical
from app.analysis.volume import analyze_volume
from app.analysis.support_resistance import calculate_levels
from app.analysis.risk import analyze_risk

from app.engine.scoring import calculate_final_score
from app.engine.decision import make_decision

from app.reports.generator import generate_report


class IntelligenceEngine:

    def __init__(self):

        self.market_provider = YahooProvider()

    def analyze(self, symbol: str):

        # -----------------------------------------------------
        # GET MARKET DATA
        # -----------------------------------------------------

        snapshot = (
            self.market_provider
            .get_market_data(symbol)
        )

        df = snapshot.dataframe

        # -----------------------------------------------------
        # TECHNICAL ANALYSIS
        # -----------------------------------------------------

        technical = analyze_technical(df)

        # -----------------------------------------------------
        # VOLUME ANALYSIS
        # -----------------------------------------------------

        volume = analyze_volume(df)

        # -----------------------------------------------------
        # SUPPORT / RESISTANCE ANALYSIS
        # -----------------------------------------------------

        levels = calculate_levels(df)

        # -----------------------------------------------------
        # CURRENT PRICE RISK / REWARD ANALYSIS
        #
        # Uses the local trade levels for the current entry.
        # -----------------------------------------------------

        risk = analyze_risk(
            price=snapshot.price,

            trade_support=levels["trade_support"],
            trade_resistance=levels["trade_resistance"],

            major_support=levels["major_support"],
            major_resistance=levels["major_resistance"],
        )

        # -----------------------------------------------------
        # PULLBACK SETUP ANALYSIS
        #
        # Preferred entry zone:
        #
        # Lower = trade support
        # Upper = trade support + 0.5 ATR
        #
        # Use the upper boundary as the conservative
        # pullback entry reference.
        # -----------------------------------------------------

        pullback_entry = None
        pullback_stop = None

        pullback_rr_t1 = None
        pullback_rr_t2 = None

        trade_support = levels.get(
            "trade_support"
        )

        trade_resistance = levels.get(
            "trade_resistance"
        )

        major_resistance = levels.get(
            "major_resistance"
        )

        atr = levels.get(
            "atr"
        )

        if (
            trade_support is not None
            and trade_resistance is not None
            and major_resistance is not None
            and atr is not None
            and atr > 0
        ):

            # Conservative entry:
            # upper edge of pullback zone.

            pullback_entry = (
                trade_support
                + (atr * 0.5)
            )

            # One ATR below support.

            pullback_stop = (
                trade_support
                - atr
            )

            pullback_risk = (
                pullback_entry
                - pullback_stop
            )

            if pullback_risk > 0:

                # Target 1:
                # Local trade resistance.

                if (
                    trade_resistance
                    > pullback_entry
                ):
                    pullback_rr_t1 = (
                        (
                            trade_resistance
                            - pullback_entry
                        )
                        / pullback_risk
                    )

                # Target 2:
                # Major resistance.

                if (
                    major_resistance
                    > pullback_entry
                ):
                    pullback_rr_t2 = (
                        (
                            major_resistance
                            - pullback_entry
                        )
                        / pullback_risk
                    )

        # -----------------------------------------------------
        # COLLECT ANALYSIS RESULTS
        # -----------------------------------------------------

        results = {
            "technical": technical,
            "volume": volume,
            "risk": risk,
        }

        # -----------------------------------------------------
        # FINAL SCORE
        # -----------------------------------------------------

        final_score = calculate_final_score(
            results
        )

        # -----------------------------------------------------
        # DECISION ENGINE
        #
        # Pass pullback R/R so the engine can distinguish:
        #
        # "Bad entry now"
        #
        # from
        #
        # "Potentially attractive after pullback"
        # -----------------------------------------------------

        decision = make_decision(
            score=final_score,
            technical=technical,
            volume=volume,
            risk=risk,
            pullback_rr_t1=pullback_rr_t1,
            pullback_rr_t2=pullback_rr_t2,
        )

        # -----------------------------------------------------
        # GENERATE REPORT
        # -----------------------------------------------------

        report, report_path = generate_report(
            symbol,
            snapshot,
            results,
            levels,
            final_score,
            decision
        )

        return {
            "symbol": symbol,
            "snapshot": snapshot,
            "results": results,
            "levels": levels,

            "pullback_setup": {
                "entry": (
                    round(pullback_entry, 4)
                    if pullback_entry is not None
                    else None
                ),
                "stop": (
                    round(pullback_stop, 4)
                    if pullback_stop is not None
                    else None
                ),
                "risk_reward_t1": (
                    round(pullback_rr_t1, 2)
                    if pullback_rr_t1 is not None
                    else None
                ),
                "risk_reward_t2": (
                    round(pullback_rr_t2, 2)
                    if pullback_rr_t2 is not None
                    else None
                ),
            },

            "score": final_score,
            "decision": decision,
            "report": report,
            "report_path": str(report_path),
        }
