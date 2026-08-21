# /opt/market/indices_signal/telegram_bot.py

import logging

import requests

from indices_config import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    MESSAGE_PREFIX,
    TICKERS,
)

from signal_engine import (
    run,
    log_signal,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)


# ============================================================
# MESSAGE FORMAT
# ============================================================

def format_message(sig):

    cfg = TICKERS[
        sig["ticker"]
    ]

    emoji = cfg["emoji"]

    direction = (
        "🟢 BUY"
        if sig["type"] == "BUY"
        else "🔴 SELL"
    )

    risk = abs(
        sig["price"]
        - sig["sl"]
    )

    reward = abs(
        sig["tp"]
        - sig["price"]
    )

    rr = (
        reward / risk
        if risk > 0
        else 0
    )

    bar_time = sig["bar_time"]

    msg = f"""
{MESSAGE_PREFIX} {emoji}

*{cfg["display_name"]}*

{direction} SIGNAL

💰 Entry Reference: `{sig["price"]:.2f}`
🛑 Stop Loss: `{sig["sl"]:.2f}`
🎯 Take Profit: `{sig["tp"]:.2f}`

📊 Risk:Reward = *1:{rr:.2f}*

⭐ Signal Score: *{sig["score"]}/100*

📈 Daily Trend: *{sig["daily_trend"]}*
📉 RSI: `{sig["rsi"]:.1f}`
📊 ADX: `{sig["adx"]:.1f}`
📏 ATR: `{sig["atr"]:.2f}`

🕯 Completed 4H Candle:
`{bar_time}`

✅ Confirmations:
"""

    for reason in sig["reasons"]:

        # Escape Markdown special chars so a reason string can never
        # break Telegram's parser (e.g. underscores in filenames).
        safe_reason = (
            str(reason)
            .replace("_", "\\_")
            .replace("*", "\\*")
            .replace("[", "\\[")
            .replace("`", "\\`")
        )

        msg += (
            f"• {safe_reason}\n"
        )

    msg += """

⚠️ Confirm the current XM CFD price before entering.
⏰ Strategy: 4H Multi-Timeframe
"""

    return msg.strip()


# ============================================================
# TELEGRAM SEND
# ============================================================

def send_telegram(text):

    if not TELEGRAM_BOT_TOKEN:
        logger.error(
            "TELEGRAM_BOT_TOKEN is not configured"
        )

        return False

    if not TELEGRAM_CHAT_ID:
        logger.error(
            "TELEGRAM_CHAT_ID is not configured"
        )

        return False

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
    }

    try:

        response = requests.post(
            url,
            json=payload,
            timeout=15,
        )

        if response.status_code == 200:

            logger.info(
                "Telegram message sent"
            )

            return True

        logger.error(
            "Telegram API error: %s",
            response.text,
        )

        return False

    except requests.RequestException as exc:

        logger.error(
            "Telegram exception: %s",
            exc,
        )

        return False


# ============================================================
# MAIN
# ============================================================

def main():

    logger.info(
        "Running indices signal check"
    )

    signals = run()

    if not signals:

        logger.info(
            "No signals generated"
        )

        return

    for signal in signals:

        message = format_message(
            signal
        )

        if send_telegram(message):

            log_signal(signal)

        else:

            logger.error(
                "%s %s was NOT logged because Telegram failed",
                signal["ticker"],
                signal["type"],
            )


if __name__ == "__main__":
    main()
