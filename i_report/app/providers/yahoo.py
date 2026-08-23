import sys
from pathlib import Path

from app.providers.base import MarketDataProvider
from app.schemas.market import MarketSnapshot

# Repo-root yahoo_client (circuit breaker). i_report/app/providers → market.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
try:
    import yahoo_client
except ImportError:  # pragma: no cover — standalone i_report checkout
    yahoo_client = None


class YahooProvider(MarketDataProvider):

    def normalize_symbol(self, symbol: str) -> str:
        symbol = symbol.upper().strip()

        if "." not in symbol:
            return f"{symbol}.KL"

        return symbol

    def get_market_data(self, symbol: str) -> MarketSnapshot:

        yahoo_symbol = self.normalize_symbol(symbol)

        if yahoo_client is not None:
            df = yahoo_client.history(
                yahoo_symbol,
                period="1y",
                interval="1d",
                auto_adjust=False,
            )
        else:
            import yfinance as yf
            df = yf.Ticker(yahoo_symbol).history(
                period="1y",
                interval="1d",
                auto_adjust=False,
            )

        if df is None or df.empty:
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
