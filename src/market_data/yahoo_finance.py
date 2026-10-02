"""Unofficial chart endpoint used by finance.yahoo.com — no key required."""

from datetime import datetime, timezone

import requests

from symbols import is_crypto_symbol, normalize_symbol

from .bar import Bar, crypto_base, crypto_quote

_TIMEOUT = 10


class YahooFinanceDataClient:
    _BASE_URL = "https://query1.finance.yahoo.com/v8/finance/chart"
    _INTERVALS = {1: "1m", 2: "2m", 5: "5m", 15: "15m", 30: "30m", 60: "60m", 1440: "1d"}
    # Yahoo rejects intraday intervals outside a narrow range window.
    _RANGES = {1: "1d", 2: "5d", 5: "5d", 15: "5d", 30: "1mo", 60: "3mo", 1440: "1y"}

    def get_bars(self, symbol: str, minutes: int, limit: int = 100) -> list[Bar]:
        interval = self._INTERVALS.get(minutes)
        if interval is None:
            raise ValueError(f"Unsupported Yahoo Finance interval: {minutes} minutes")
        ticker = f"{crypto_base(symbol)}-{crypto_quote(symbol)}" if is_crypto_symbol(symbol) else normalize_symbol(symbol)
        response = requests.get(
            f"{self._BASE_URL}/{ticker}",
            params={"interval": interval, "range": self._RANGES[minutes]},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=_TIMEOUT,
        )
        response.raise_for_status()
        result = response.json()["chart"]["result"][0]
        timestamps = result["timestamp"]
        quote = result["indicators"]["quote"][0]
        bars = [
            Bar(timestamp=datetime.fromtimestamp(ts, tz=timezone.utc), open=o, high=h, low=lo, close=c, volume=v)
            for ts, o, h, lo, c, v in zip(
                timestamps, quote["open"], quote["high"], quote["low"], quote["close"], quote["volume"]
            )
            if None not in (o, h, lo, c)
        ]
        return bars[-limit:]
