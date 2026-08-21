from app.schemas.market import AnalysisResult


def analyze_risk(
    price,
    support=None,
    resistance=None,
    trade_support=None,
    trade_resistance=None,
    major_support=None,
    major_resistance=None,
):

    # ---------------------------------------------------------
    # BACKWARD COMPATIBILITY
    #
    # If trade levels are not provided, fall back to the
    # original support/resistance values.
    # ---------------------------------------------------------

    if trade_support is None:
        trade_support = support

    if trade_resistance is None:
        trade_resistance = resistance

    # ---------------------------------------------------------
    # VALIDATE LEVELS
    # ---------------------------------------------------------

    if not trade_support or not trade_resistance:

        return AnalysisResult(
            name="Risk Analysis",
            score=40,
            verdict="UNKNOWN",
            summary="Unable to establish reliable trading levels",
            details={}
        )

    # ---------------------------------------------------------
    # CALCULATE TRADE RISK / REWARD
    # ---------------------------------------------------------

    risk = price - trade_support
    reward = trade_resistance - price

    if risk <= 0 or reward <= 0:
        ratio = 0
    else:
        ratio = reward / risk

    # ---------------------------------------------------------
    # SCORE
    # ---------------------------------------------------------

    if ratio >= 3:
        score = 90
        verdict = "EXCELLENT"

    elif ratio >= 2:
        score = 75
        verdict = "GOOD"

    elif ratio >= 1.5:
        score = 65
        verdict = "ACCEPTABLE"

    elif ratio >= 1:
        score = 55
        verdict = "MODERATE"

    else:
        score = 30
        verdict = "POOR"

    # ---------------------------------------------------------
    # BUILD SUMMARY
    # ---------------------------------------------------------

    summary = (
        f"Risk/reward ratio: {ratio:.2f}"
    )

    return AnalysisResult(
        name="Risk Reward",
        score=score,
        verdict=verdict,
        summary=summary,

        details={

            # Trade setup
            "entry": round(price, 4),

            "trade_support": round(
                trade_support,
                4
            ),

            "trade_resistance": round(
                trade_resistance,
                4
            ),

            "risk": round(
                risk,
                4
            ),

            "reward": round(
                reward,
                4
            ),

            "risk_reward_ratio": round(
                ratio,
                2
            ),

            # Structural levels
            "major_support": (
                round(major_support, 4)
                if major_support
                else None
            ),

            "major_resistance": (
                round(major_resistance, 4)
                if major_resistance
                else None
            ),
        }
    )
