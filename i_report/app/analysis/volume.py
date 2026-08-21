from app.schemas.market import AnalysisResult


def analyze_volume(df):

    latest_volume = float(df["Volume"].iloc[-1])

    average_volume = float(
        df["Volume"]
        .tail(20)
        .mean()
    )

    ratio = (
        latest_volume / average_volume
        if average_volume > 0
        else 0
    )

    score = 50
    reasons = []

    if ratio > 2:
        score += 20
        reasons.append("Major volume expansion")

    elif ratio > 1.3:
        score += 10
        reasons.append("Above-average volume")

    elif ratio < 0.7:
        score -= 10
        reasons.append("Weak volume participation")

    price_change = (
        df["Close"].iloc[-1]
        - df["Close"].iloc[-2]
    )

    if price_change > 0 and ratio > 1.3:
        score += 10
        reasons.append("Price increase supported by volume")

    elif price_change < 0 and ratio > 1.5:
        score -= 10
        reasons.append("Heavy selling pressure")

    score = max(0, min(100, score))

    verdict = (
        "STRONG"
        if score >= 70
        else "NEUTRAL"
        if score >= 40
        else "WEAK"
    )

    return AnalysisResult(
        name="Volume Analysis",
        score=score,
        verdict=verdict,
        summary="; ".join(reasons),
        details={
            "latest_volume": int(latest_volume),
            "average_volume_20d": int(average_volume),
            "volume_ratio": round(ratio, 2),
        }
    )
