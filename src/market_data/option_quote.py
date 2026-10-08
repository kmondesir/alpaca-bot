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
    delta: float | None  # negative for puts
    underlying_price: float


def _d1(spot: float, strike: float, volatility: float, years: float) -> float | None:
    """Black-Scholes d1 with zero rates; None when the inputs can't price an option."""
    if spot <= 0 or strike <= 0 or volatility <= 0 or years <= 0:
        return None
    return (math.log(spot / strike) + 0.5 * volatility * volatility * years) / (volatility * math.sqrt(years))


def black_scholes_gamma(spot: float, strike: float, volatility: float, years: float) -> float | None:
    """Gamma per $1 move, with zero rates; None when the inputs can't price an option."""
    d1 = _d1(spot, strike, volatility, years)
    if d1 is None:
        return None
    return math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi) / (spot * volatility * math.sqrt(years))


def black_scholes_delta(side: str, spot: float, strike: float, volatility: float, years: float) -> float | None:
    """Call delta N(d1), or put delta N(d1) - 1, with zero rates; None when the inputs can't price an option."""
    d1 = _d1(spot, strike, volatility, years)
    if d1 is None:
        return None
    call_delta = 0.5 * (1 + math.erf(d1 / math.sqrt(2)))
    return call_delta if side == "call" else call_delta - 1
