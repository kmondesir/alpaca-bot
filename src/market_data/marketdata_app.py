"""https://www.marketdata.app/docs/api/ — option chains with greeks and open interest; needs an API key.

Each contract returned costs one API credit, so fetch a chain only when it is needed.
"""

from datetime import date

import requests

import config
from symbols import normalize_symbol

from .option_quote import OptionQuote

_TIMEOUT = 20


class MarketDataAppClient:
    _BASE_URL = "https://api.marketdata.app/v1"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or config.MARKETDATA_API_KEY
        if not self.api_key:
            raise RuntimeError("MARKETDATA_API_KEY is not configured")

    def get_option_chain(self, underlying: str, expiration: date, index: bool = False) -> list[OptionQuote]:
        """Every contract on `underlying` expiring on `expiration`; empty if there are none.

        MarketData.app takes index symbols as-is (SPX), so `index` is unused.
        """
        response = requests.get(
            f"{self._BASE_URL}/options/chain/{normalize_symbol(underlying)}/",
            params={"expiration": expiration.isoformat()},
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=_TIMEOUT,
        )
        if response.status_code == 404 and response.json().get("s") == "no_data":
            return []
        response.raise_for_status()
        data = response.json()
        if data.get("s") != "ok":
            raise RuntimeError(f"MarketData option chain for {underlying} failed: {data.get('errmsg', data)}")
        return [
            OptionQuote(
                symbol=symbol,
                side=side,
                strike=float(strike),
                bid=float(bid or 0),
                ask=float(ask or 0),
                open_interest=float(interest or 0),
                gamma=None if gamma is None else float(gamma),
                underlying_price=float(price),
            )
            for symbol, side, strike, bid, ask, interest, gamma, price in zip(
                data["optionSymbol"],
                data["side"],
                data["strike"],
                data["bid"],
                data["ask"],
                data["openInterest"],
                data["gamma"],
                data["underlyingPrice"],
            )
        ]
