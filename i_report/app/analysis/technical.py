import pandas as pd
import pandas_ta_classic as ta

from app.schemas.market import AnalysisResult


def analyze_technical(df: pd.DataFrame) -> AnalysisResult:

    data = df.copy()

    data["RSI"] = ta.rsi(data["Close"], length=14)

    macd = ta.macd(data["Close"])

    if macd is not None:
        data = data.join(macd)

    data["SMA20"] = ta.sma(data["Close"], length=20)
    data["SMA50"] = ta.sma(data["Close"], length=50)
    data["SMA200"] = ta.sma(data["Close"], length=200)

    bb = ta.bbands(data["Close"], length=20)
    if bb is not None:
        data = data.join(bb)

    latest = data.iloc[-1]

    price = float(latest["Close"])
    rsi = float(latest["RSI"])

    score = 50
    reasons = []

    # RSI
    if 45 <= rsi <= 65:
        score += 10
        reasons.append("RSI in healthy momentum zone")

    elif rsi < 30:
        score += 5
        reasons.append("RSI oversold")

    elif rsi > 75:
        score -= 15
        reasons.append("RSI strongly overbought")

    # Moving averages
    if price > latest["SMA20"]:
        score += 5
        reasons.append("Price above SMA20")

    if price > latest["SMA50"]:
        score += 8
        reasons.append("Price above SMA50")

    if price > latest["SMA200"]:
        score += 10
        reasons.append("Price above SMA200")

    # MACD
    macd_cols = [
        col for col in data.columns
        if col.startswith("MACD_")
        and not col.startswith("MACDh_")
        and not col.startswith("MACDs_")
    ]

    if macd_cols:
        macd_value = latest[macd_cols[0]]

        signal_cols = [
            col for col in data.columns
            if col.startswith("MACDs_")
        ]

        if signal_cols:
            signal_value = latest[signal_cols[0]]

            if macd_value > signal_value:
                score += 10
                reasons.append("MACD bullish")

            else:
                score -= 5
                reasons.append("MACD bearish")

    score = max(0, min(100, score))

    if score >= 75:
        verdict = "BULLISH"
    elif score >= 55:
        verdict = "LEAN_BULLISH"
    elif score >= 40:
        verdict = "NEUTRAL"
    else:
        verdict = "BEARISH"

    return AnalysisResult(
        name="Technical Analysis",
        score=score,
        verdict=verdict,
        summary="; ".join(reasons),
        details={
            "price": price,
            "rsi": round(rsi, 2),
            "sma20": round(float(latest["SMA20"]), 4),
            "sma50": round(float(latest["SMA50"]), 4),
            "sma200": round(float(latest["SMA200"]), 4),
        }
    )
