def make_decision(
    score,
    technical,
    volume,
    risk,
    pullback_rr_t1=None,
    pullback_rr_t2=None,
):

    warnings = []

    # -----------------------------------------------------
    # WARNINGS
    # -----------------------------------------------------

    if technical.score < 40:
        warnings.append(
            "Technical trend is weak"
        )

    if volume.score < 40:
        warnings.append(
            "Volume confirmation is weak"
        )

    if risk.score < 50:
        warnings.append(
            "Current price risk/reward is unattractive"
        )

    # -----------------------------------------------------
    # PULLBACK QUALITY
    #
    # Use the best available pullback target.
    #
    # Target 2 is normally the larger structural target,
    # so use whichever risk/reward ratio is higher.
    # -----------------------------------------------------

    pullback_rr = None

    pullback_ratios = [
        ratio
        for ratio in [
            pullback_rr_t1,
            pullback_rr_t2,
        ]
        if ratio is not None
        and ratio > 0
    ]

    if pullback_ratios:
        pullback_rr = max(
            pullback_ratios
        )

    # -----------------------------------------------------
    # DECISION LOGIC
    #
    # Priority is important:
    #
    # 1. Excellent current setup
    # 2. Good current setup
    # 3. Bad current entry but attractive pullback
    # 4. Mixed / waiting
    # 5. Weak setup
    # -----------------------------------------------------

    # -----------------------------------------------------
    # STRONG BUY
    #
    # Strong technical conditions and excellent current
    # risk/reward.
    # -----------------------------------------------------

    if (
        score >= 80
        and technical.score >= 70
        and risk.score >= 75
        and volume.score >= 50
    ):
        decision = "STRONG_BUY"

    # -----------------------------------------------------
    # BUY
    #
    # Good overall conditions and acceptable current
    # risk/reward.
    # -----------------------------------------------------

    elif (
        score >= 70
        and technical.score >= 60
        and risk.score >= 55
    ):
        decision = "BUY"

    # -----------------------------------------------------
    # WATCH BREAKOUT
    #
    # Technical structure is strong and risk is acceptable,
    # but volume confirmation is still weak.
    # -----------------------------------------------------

    elif (
        technical.score >= 65
        and risk.score >= 50
        and volume.score < 50
    ):
        decision = "WATCH_BREAKOUT"

    # -----------------------------------------------------
    # WAIT FOR PULLBACK
    #
    # Current entry is unattractive, but the projected
    # pullback setup has at least 2:1 potential R/R.
    # -----------------------------------------------------

    elif (
        technical.score >= 60
        and risk.score < 50
        and pullback_rr is not None
        and pullback_rr >= 2.0
    ):
        decision = "WAIT_FOR_PULLBACK"

        warnings.append(
            f"Pullback setup offers potential R/R of "
            f"{pullback_rr:.2f}"
        )

    # -----------------------------------------------------
    # WAIT FOR ENTRY
    #
    # Setup is not poor, but neither the current entry nor
    # pullback conditions are strong enough yet.
    # -----------------------------------------------------

    elif (
        score >= 55
        or technical.score >= 55
    ):
        decision = "WAIT_FOR_ENTRY"

    # -----------------------------------------------------
    # NEUTRAL
    #
    # Mixed signals.
    # -----------------------------------------------------

    elif score >= 40:
        decision = "NEUTRAL"

    # -----------------------------------------------------
    # AVOID
    #
    # Weak overall conditions.
    # -----------------------------------------------------

    else:
        decision = "AVOID"

    # -----------------------------------------------------
    # CONFIDENCE
    #
    # Base confidence on the overall score.
    # -----------------------------------------------------

    confidence = min(
        95,
        max(
            35,
            int(score)
        )
    )

    return {
        "decision": decision,
        "confidence": confidence,
        "warnings": warnings,
    }
