"""Shared option-chain row returned by every option data client."""

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class OptionQuote:
    symbol: str  # OCC symbol, the same format Alpaca trades, e.g. SPXW261007C07800000
    side: str  # "call" or "put"
    strike: float
    bid: float
    ask: float
    open_interest: float
    gamma: float | None
    underlying_price: float


def black_scholes_gamma(spot: float, strike: float, volatility: float, years: float) -> float | None:
    """Gamma per $1 move, with zero rates; None when the inputs can't price an option."""
    if spot <= 0 or strike <= 0 or volatility <= 0 or years <= 0:
        return None
    root_time = volatility * math.sqrt(years)
    d1 = (math.log(spot / strike) + 0.5 * volatility * volatility * years) / root_time
    return math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi) / (spot * root_time)
