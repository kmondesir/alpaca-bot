"""https://coinmarketcap.com/api/documentation/v1/ — free tier needs an API key, latest quotes only."""

import requests

import config

from .bar import crypto_base, crypto_quote

_TIMEOUT = 10


class CoinMarketCapClient:
    """Free tier offers no historical OHLCV, so this client has no get_bars."""

    _BASE_URL = "https://pro-api.coinmarketcap.com/v1"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or config.COINMARKETCAP_API_KEY
        if not self.api_key:
            raise RuntimeError("COINMARKETCAP_API_KEY is not configured")

    def get_latest_price(self, symbol: str) -> float:
        base, quote = crypto_base(symbol), crypto_quote(symbol)
        response = requests.get(
            f"{self._BASE_URL}/cryptocurrency/quotes/latest",
            params={"symbol": base, "convert": quote},
            headers={"X-CMC_PRO_API_KEY": self.api_key, "Accept": "application/json"},
            timeout=_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()["data"][base]
        if isinstance(data, list):
            data = data[0]
        return float(data["quote"][quote]["price"])
