"""https://docs.cdp.coinbase.com/exchange/reference — public endpoints, no key required."""

from datetime import datetime, timezone

import requests

from .bar import Bar, crypto_base, crypto_quote

_TIMEOUT = 10


class CoinbaseDataClient:
    _BASE_URL = "https://api.exchange.coinbase.com"
    _GRANULARITIES = {1: 60, 5: 300, 15: 900, 60: 3600, 360: 21600, 1440: 86400}

    def get_bars(self, symbol: str, minutes: int, limit: int = 100) -> list[Bar]:
        granularity = self._GRANULARITIES.get(minutes)
        if granularity is None:
            raise ValueError(f"Unsupported Coinbase granularity: {minutes} minutes")
        product_id = f"{crypto_base(symbol)}-{crypto_quote(symbol)}"
        response = requests.get(
            f"{self._BASE_URL}/products/{product_id}/candles",
            params={"granularity": granularity},
            headers={"User-Agent": "alpaca-bot"},
            timeout=_TIMEOUT,
        )
        response.raise_for_status()
        # Coinbase returns [time, low, high, open, close, volume], newest first.
        candles = sorted(response.json(), key=lambda candle: candle[0])[-limit:]
        return [
            Bar(
                timestamp=datetime.fromtimestamp(candle[0], tz=timezone.utc),
                low=float(candle[1]),
                high=float(candle[2]),
                open=float(candle[3]),
                close=float(candle[4]),
                volume=float(candle[5]),
            )
            for candle in candles
        ]

    def get_latest_price(self, symbol: str) -> float:
        product_id = f"{crypto_base(symbol)}-{crypto_quote(symbol)}"
        response = requests.get(
            f"{self._BASE_URL}/products/{product_id}/ticker",
            headers={"User-Agent": "alpaca-bot"},
            timeout=_TIMEOUT,
        )
        response.raise_for_status()
        return float(response.json()["price"])
