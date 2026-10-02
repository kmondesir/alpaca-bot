"""https://binance-docs.github.io/apidocs/spot/en/ — public endpoints, no key required."""

from datetime import datetime, timezone

import requests

from .bar import Bar, crypto_base, crypto_quote

_TIMEOUT = 10


class BinanceDataClient:
    _BASE_URL = "https://api.binance.com/api/v3"
    _INTERVALS = {1: "1m", 3: "3m", 5: "5m", 15: "15m", 30: "30m", 60: "1h", 240: "4h", 1440: "1d"}

    def get_bars(self, symbol: str, minutes: int, limit: int = 100) -> list[Bar]:
        interval = self._INTERVALS.get(minutes)
        if interval is None:
            raise ValueError(f"Unsupported Binance interval: {minutes} minutes")
        pair = f"{crypto_base(symbol)}{crypto_quote(symbol)}"
        response = requests.get(
            f"{self._BASE_URL}/klines",
            params={"symbol": pair, "interval": interval, "limit": limit},
            timeout=_TIMEOUT,
        )
        response.raise_for_status()
        return [
            Bar(
                timestamp=datetime.fromtimestamp(candle[0] / 1000, tz=timezone.utc),
                open=float(candle[1]),
                high=float(candle[2]),
                low=float(candle[3]),
                close=float(candle[4]),
                volume=float(candle[5]),
            )
            for candle in response.json()
        ]

    def get_latest_price(self, symbol: str) -> float:
        pair = f"{crypto_base(symbol)}{crypto_quote(symbol)}"
        response = requests.get(f"{self._BASE_URL}/ticker/price", params={"symbol": pair}, timeout=_TIMEOUT)
        response.raise_for_status()
        return float(response.json()["price"])
