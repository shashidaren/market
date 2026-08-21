from dataclasses import dataclass
from typing import Optional
import pandas as pd


@dataclass
class MarketSnapshot:
    symbol: str
    price: float
    previous_close: Optional[float]
    change_percent: Optional[float]
    currency: str
    dataframe: pd.DataFrame


@dataclass
class AnalysisResult:
    name: str
    score: float
    verdict: str
    summary: str
    details: dict
