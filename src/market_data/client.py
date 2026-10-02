"""Dispatcher that picks a provider per symbol class."""

import logging

from symbols import is_crypto_symbol, normalize_symbol

from .bar import Bar
from .binance import BinanceDataClient
from .coinbase import CoinbaseDataClient
from .yahoo_finance import YahooFinanceDataClient

logger = logging.getLogger(__name__)


class MarketDataClient:
    """Dispatches crypto bars to Binance (falling back to Coinbase) and stock bars to Yahoo Finance."""

    def __init__(self):
        self.binance = BinanceDataClient()
        self.coinbase = CoinbaseDataClient()
        self.yahoo = YahooFinanceDataClient()

    def get_bars(self, symbol: str, minutes: int, limit: int = 100) -> list[Bar]:
        symbol = normalize_symbol(symbol)
        if not is_crypto_symbol(symbol):
            return self.yahoo.get_bars(symbol, minutes, limit)
        try:
            return self.binance.get_bars(symbol, minutes, limit)
        except Exception:
            logger.warning("Binance bars failed for %s; falling back to Coinbase", symbol, exc_info=True)
            return self.coinbase.get_bars(symbol, minutes, limit)
