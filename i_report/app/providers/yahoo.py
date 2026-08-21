import yfinance as yf

from app.providers.base import MarketDataProvider
from app.schemas.market import MarketSnapshot


class YahooProvider(MarketDataProvider):

    def normalize_symbol(self, symbol: str) -> str:
        symbol = symbol.upper().strip()

        if "." not in symbol:
            return f"{symbol}.KL"

        return symbol

    def get_market_data(self, symbol: str) -> MarketSnapshot:

        yahoo_symbol = self.normalize_symbol(symbol)

        ticker = yf.Ticker(yahoo_symbol)

        df = ticker.history(
            period="1y",
            interval="1d",
            auto_adjust=False
        )

        if df.empty:
            raise ValueError(
                f"No market data found for {symbol}"
            )

        latest = df.iloc[-1]

        price = float(latest["Close"])

        previous_close = None
        change_percent = None

        if len(df) > 1:
            previous_close = float(df.iloc[-2]["Close"])

            if previous_close:
                change_percent = (
                    (price - previous_close)
                    / previous_close
                    * 100
                )

        return MarketSnapshot(
            symbol=symbol.upper(),
            price=price,
            previous_close=previous_close,
            change_percent=change_percent,
            currency="MYR",
            dataframe=df
        )
