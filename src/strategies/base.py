"""Shared signal type and closed-bar fetching for every strategy.

Crypto bars come from Coinbase; stock bars come from Alpaca's IEX feed; index
bars come from Yahoo Finance, since Alpaca has no index price data.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from alpaca.common.enums import Sort
from alpaca.data.enums import DataFeed
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from symbols import is_crypto_symbol, normalize_symbol

BAR_LIMIT = 100


@dataclass(frozen=True)
class Signal:
    symbol: str
    direction: Literal["long", "short"]
    bar_timestamp: datetime
    # Set for option signals: symbol is then an OCC contract bought to open,
    # so a short view is expressed by buying a put rather than selling.
    underlying: Optional[str] = None
    description: Optional[str] = None

    @property
    def is_option(self) -> bool:
        return self.underlying is not None

    @property
    def side(self) -> Literal["buy", "sell"]:
        if self.is_option:
            return "buy"
        return "buy" if self.direction == "long" else "sell"


def closed_bars(
    data_client, crypto_bars_client, symbol: str, minutes: int, limit: int = BAR_LIMIT
) -> list:
    """Return up to `limit` closed bars, oldest first, dropping the in-progress candle."""
    now = datetime.now(timezone.utc)
    if is_crypto_symbol(symbol):
        # Fetch one extra bar so dropping the in-progress candle still leaves `limit`.
        bars = crypto_bars_client.get_bars(symbol, minutes, limit + 1)
        return [bar for bar in bars if bar.timestamp + timedelta(minutes=minutes) <= now][-limit:]

    request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame(minutes, TimeFrameUnit.Minute),
        start=now - timedelta(days=14),
        end=now,
        limit=limit,
        sort=Sort.DESC,
        feed=DataFeed.IEX,
    )
    response = data_client.get_stock_bars(request)
    data = response.data if hasattr(response, "data") else response
    bars = next(
        (values for key, values in data.items() if normalize_symbol(key) == normalize_symbol(symbol)),
        [],
    )
    bars = list(reversed(bars))
    return [bar for bar in bars if bar.timestamp + timedelta(minutes=minutes) <= now]


def closed_index_bars(yahoo_client, symbol: str, minutes: int, limit: int = BAR_LIMIT) -> list:
    """Return up to `limit` closed Yahoo Finance bars for an index such as SPX, oldest first."""
    now = datetime.now(timezone.utc)
    # Yahoo prefixes indexes with ^, e.g. ^SPX. Fetch one extra bar so dropping
    # the in-progress candle still leaves `limit`.
    bars = yahoo_client.get_bars(f"^{normalize_symbol(symbol)}", minutes, limit + 1)
    return [bar for bar in bars if bar.timestamp + timedelta(minutes=minutes) <= now][-limit:]
