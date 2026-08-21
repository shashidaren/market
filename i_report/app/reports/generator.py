from datetime import datetime
from pathlib import Path

from app.core.config import settings


def format_level(value):
    """
    Format price levels safely.
    """

    if value is None:
        return "N/A"

    return f"{value:.4f}"


def format_percent(value):
    """
    Format percentages safely.
    """

    if value is None:
        return "N/A"

    return f"{value:.2f}%"


def format_ratio(value):
    """
    Format risk/reward ratios safely.
    """

    if value is None:
        return "N/A"

    return f"{value:.2f}"


def generate_report(
    symbol,
    snapshot,
    results,
    levels,
    final_score,
    decision
):

    now = datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    # -----------------------------------------------------
    # MARKET / LEVEL DATA
    # -----------------------------------------------------

    entry = snapshot.price

    trade_support = levels.get(
        "trade_support"
    )

    trade_resistance = levels.get(
        "trade_resistance"
    )

    major_support = levels.get(
        "major_support"
    )

    major_resistance = levels.get(
        "major_resistance"
    )

    atr = levels.get(
        "atr"
    )

    # -----------------------------------------------------
    # CURRENT TRADE SETUP
    # -----------------------------------------------------

    risk = None
    risk_percent = None

    reward_1 = None
    reward_1_percent = None

    reward_2 = None
    reward_2_percent = None

    rr_target_1 = None
    rr_target_2 = None

    if (
        trade_support is not None
        and trade_support < entry
    ):
        risk = entry - trade_support

        risk_percent = (
            risk / entry
        ) * 100

    if (
        trade_resistance is not None
        and trade_resistance > entry
    ):
        reward_1 = (
            trade_resistance - entry
        )

        reward_1_percent = (
            reward_1 / entry
        ) * 100

    if (
        major_resistance is not None
        and major_resistance > entry
    ):
        reward_2 = (
            major_resistance - entry
        )

        reward_2_percent = (
            reward_2 / entry
        ) * 100

    if risk is not None and risk > 0:

        if reward_1 is not None:
            rr_target_1 = (
                reward_1 / risk
            )

        if reward_2 is not None:
            rr_target_2 = (
                reward_2 / risk
            )

    # -----------------------------------------------------
    # PULLBACK PLAN
    # -----------------------------------------------------

    pullback_entry_low = None
    pullback_entry_high = None

    invalidation = None
    distance_to_entry = None

    if (
        trade_support is not None
        and atr is not None
        and atr > 0
    ):

        # Preferred entry zone:
        #
        # Lower boundary = confirmed support
        # Upper boundary = support + 0.5 ATR

        pullback_entry_low = (
            trade_support
        )

        pullback_entry_high = (
            trade_support
            + (atr * 0.5)
        )

        # Invalidation:
        #
        # One ATR below confirmed support.

        invalidation = (
            trade_support
            - atr
        )

        # Distance from current price to the
        # upper edge of the preferred entry zone.

        if entry > pullback_entry_high:

            distance_to_entry = (
                (
                    entry
                    - pullback_entry_high
                )
                / entry
            ) * 100

    # -----------------------------------------------------
    # PULLBACK TRADE SETUP
    #
    # Use the upper boundary of the entry zone as the
    # reference entry. This is conservative because we do
    # not assume the trader catches the exact support level.
    # -----------------------------------------------------

    pullback_entry = pullback_entry_high

    pullback_risk = None
    pullback_risk_percent = None

    pullback_reward_1 = None
    pullback_reward_1_percent = None

    pullback_reward_2 = None
    pullback_reward_2_percent = None

    pullback_rr_1 = None
    pullback_rr_2 = None

    if (
        pullback_entry is not None
        and invalidation is not None
        and invalidation < pullback_entry
    ):
        pullback_risk = (
            pullback_entry
            - invalidation
        )

        pullback_risk_percent = (
            pullback_risk
            / pullback_entry
        ) * 100

    if (
        pullback_entry is not None
        and trade_resistance is not None
        and trade_resistance > pullback_entry
    ):
        pullback_reward_1 = (
            trade_resistance
            - pullback_entry
        )

        pullback_reward_1_percent = (
            pullback_reward_1
            / pullback_entry
        ) * 100

    if (
        pullback_entry is not None
        and major_resistance is not None
        and major_resistance > pullback_entry
    ):
        pullback_reward_2 = (
            major_resistance
            - pullback_entry
        )

        pullback_reward_2_percent = (
            pullback_reward_2
            / pullback_entry
        ) * 100

    if (
        pullback_risk is not None
        and pullback_risk > 0
    ):

        if pullback_reward_1 is not None:
            pullback_rr_1 = (
                pullback_reward_1
                / pullback_risk
            )

        if pullback_reward_2 is not None:
            pullback_rr_2 = (
                pullback_reward_2
                / pullback_risk
            )

    # -----------------------------------------------------
    # REPORT
    # -----------------------------------------------------

    report = f"""
══════════════════════════════════════════════
INTELLIGENCE REPORT
══════════════════════════════════════════════

Symbol: {symbol}
Generated: {now}

CURRENT MARKET
──────────────────────────────────────────────
Price: RM{snapshot.price:.4f}

Previous Close:
{snapshot.previous_close}

Change:
{snapshot.change_percent:.2f}%

ANALYSIS SCORES
──────────────────────────────────────────────
Technical: {results["technical"].score}/100
Volume:    {results["volume"].score}/100
Risk:      {results["risk"].score}/100

FINAL SCORE
──────────────────────────────────────────────
{final_score}/100

DECISION
──────────────────────────────────────────────
{decision["decision"]}

Confidence: {decision["confidence"]}%

TRADE LEVELS
──────────────────────────────────────────────
Trade Support:    RM{format_level(trade_support)}
Trade Resistance: RM{format_level(trade_resistance)}

MAJOR STRUCTURE
──────────────────────────────────────────────
Major Support:    RM{format_level(major_support)}
Major Resistance: RM{format_level(major_resistance)}

VOLATILITY
──────────────────────────────────────────────
ATR:                 RM{format_level(atr)}
Average Daily Range: RM{format_level(levels.get("average_daily_range"))}

CURRENT TRADE SETUP
──────────────────────────────────────────────
Entry Reference:   RM{entry:.4f}
Stop Reference:    RM{format_level(trade_support)}
Target 1:          RM{format_level(trade_resistance)}
Target 2:          RM{format_level(major_resistance)}

Risk:              RM{format_level(risk)}
Risk Percentage:   {format_percent(risk_percent)}

Reward Target 1:   RM{format_level(reward_1)}
Reward 1 Percent:  {format_percent(reward_1_percent)}

Reward Target 2:   RM{format_level(reward_2)}
Reward 2 Percent:  {format_percent(reward_2_percent)}

Risk/Reward T1:    {format_ratio(rr_target_1)}
Risk/Reward T2:    {format_ratio(rr_target_2)}

PULLBACK PLAN
──────────────────────────────────────────────
Preferred Entry Zone:
RM{format_level(pullback_entry_low)} - RM{format_level(pullback_entry_high)}

Current Price:
RM{entry:.4f}

Distance to Entry:
{format_percent(distance_to_entry)}

Invalidation:
RM{format_level(invalidation)}

PULLBACK TRADE SETUP
──────────────────────────────────────────────
Entry Reference:   RM{format_level(pullback_entry)}
Stop Reference:    RM{format_level(invalidation)}

Target 1:          RM{format_level(trade_resistance)}
Target 2:          RM{format_level(major_resistance)}

Risk:              RM{format_level(pullback_risk)}
Risk Percentage:   {format_percent(pullback_risk_percent)}

Reward Target 1:   RM{format_level(pullback_reward_1)}
Reward 1 Percent:  {format_percent(pullback_reward_1_percent)}

Reward Target 2:   RM{format_level(pullback_reward_2)}
Reward 2 Percent:  {format_percent(pullback_reward_2_percent)}

Risk/Reward T1:    {format_ratio(pullback_rr_1)}
Risk/Reward T2:    {format_ratio(pullback_rr_2)}

ANALYSIS
──────────────────────────────────────────────

TECHNICAL:
{results["technical"].summary}

VOLUME:
{results["volume"].summary}

RISK:
{results["risk"].summary}

WARNINGS:
"""

    if decision["warnings"]:
        report += "\n".join(
            f"⚠ {warning}"
            for warning in decision["warnings"]
        )
    else:
        report += "None"

    report += (
        "\n\n"
        "══════════════════════════════════════════════\n"
    )

    filename = (
        f"{symbol}_"
        f"{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        f".txt"
    )

    path = Path(
        settings.REPORT_DIR
    ) / filename

    path.write_text(report)

    return report, path
