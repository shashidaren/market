from abc import ABC, abstractmethod
from app.schemas.market import MarketSnapshot


class MarketDataProvider(ABC):

    @abstractmethod
    def get_market_data(self, symbol: str) -> MarketSnapshot:
        raise NotImplementedError
