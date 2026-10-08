"""Unofficial Yahoo Finance endpoints used by finance.yahoo.com — no key required.

Bars come from the chart endpoint. Option chains need Yahoo's cookie and crumb,
so they go through the yfinance library.
"""

import math
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

import requests
import yfinance

from symbols import is_crypto_symbol, normalize_symbol

from .bar import Bar, crypto_base, crypto_quote
from .option_quote import OptionQuote, black_scholes_delta, black_scholes_gamma

_TIMEOUT = 10
_MARKET_CLOSE = time(16, 0)
_MARKET_TZ = ZoneInfo("America/New_York")
_SECONDS_PER_YEAR = 365 * 24 * 60 * 60
# Floor on time to expiry so 0DTE gamma stays finite in the last minute.
_MIN_SECONDS_TO_EXPIRY = 60
# Yahoo fills implied volatility with placeholders such as 0.00001 when it has
# no quote (e.g. outside market hours); below this it is treated as missing.
_MIN_IMPLIED_VOLATILITY = 0.01


class YahooOptionChainClient:
    """Option chains with open interest, bid/ask and implied volatility.

    Yahoo has no greeks, so gamma and delta are Black-Scholes values from each
    contract's implied volatility and the time left until the 16:00 ET close
    on expiry.
    """

    def get_option_chain(self, underlying: str, expiration: date, index: bool = False) -> list[OptionQuote]:
        """Every contract on `underlying` expiring on `expiration`; empty if there are none."""
        ticker = yfinance.Ticker(f"^{normalize_symbol(underlying)}" if index else normalize_symbol(underlying))
        if expiration.isoformat() not in ticker.options:
            return []
        chain = ticker.option_chain(expiration.isoformat())
        spot = float(chain.underlying["regularMarketPrice"])
        expires = datetime.combine(expiration, _MARKET_CLOSE, _MARKET_TZ)
        seconds = max((expires - datetime.now(timezone.utc)).total_seconds(), _MIN_SECONDS_TO_EXPIRY)
        years = seconds / _SECONDS_PER_YEAR
        quotes = []
        for side, frame in (("call", chain.calls), ("put", chain.puts)):
            for row in frame.itertuples():
                strike = float(row.strike)
                volatility = _number(row.impliedVolatility)
                usable = volatility >= _MIN_IMPLIED_VOLATILITY
                quotes.append(
                    OptionQuote(
                        symbol=row.contractSymbol,
                        side=side,
                        strike=strike,
                        bid=_number(row.bid),
                        ask=_number(row.ask),
                        open_interest=_number(row.openInterest),
                        gamma=black_scholes_gamma(spot, strike, volatility, years) if usable else None,
                        delta=black_scholes_delta(side, spot, strike, volatility, years) if usable else None,
                        underlying_price=spot,
                    )
                )
        return quotes


def _number(value) -> float:
    """Yahoo leaves blanks as NaN; treat them as 0."""
    return 0.0 if value is None or math.isnan(value) else float(value)


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
