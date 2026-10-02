"""Shared bar type and crypto symbol helpers for market data clients."""

from dataclasses import dataclass
from datetime import datetime

from symbols import normalize_symbol


@dataclass(frozen=True)
class Bar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


def crypto_base(symbol: str) -> str:
    """'BTC/USD' -> 'BTC'."""
    return normalize_symbol(symbol).split("/")[0]


def crypto_quote(symbol: str) -> str:
    """'BTC/USD' -> 'USD'."""
    return normalize_symbol(symbol).split("/")[1]
