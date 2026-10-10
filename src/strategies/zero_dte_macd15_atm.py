"""0DTE at-the-money options on a 15-minute MACD cross beyond the zero line.

On each closed 15-minute bar, the MACD(12, 26, 9) line crossing above its
signal line while the MACD line is above zero is a call; crossing below while
it is below zero is a put. Only the first such cross per underlying per day
whose bar closes inside the TRADING_START/TRADING_STOP window is traded, and
only on the runs within FRESH_MINUTES of that bar's close.

The contract is the strike nearest the underlying's price (calls for a long
view, puts for a short one), expiring today, with an acceptable bid/ask spread
and tradable on Alpaca. No GEX filter is applied. Option chains, index bars
(--indexes), sizing and exits (STOP_LOSS trailing stop, OPTION_FLATTEN_TIME)
work as in the 0DTE MACD divergence strategy.
"""

import logging
from datetime import datetime, time, timedelta, timezone
from typing import Optional

import config

from .base import Signal
from .indicators import macd
from .zero_dte_macd_divergence import (
    MARKET_TZ,
    MAX_SPREAD,
    Direction,
    OptionQuote,
    ZeroDteMacdDivergenceStrategy,
    _spread_ok,
)

logger = logging.getLogger(__name__)

CROSS_MINUTES = 15
# A 15-min MACD needs this many closed bars before a cross counts.
MIN_BARS = 60
# Runs after a bar closes that may still act on its cross (covers a missed or late run).
FRESH_MINUTES = 5
# Nearest strikes tried, in order, until one passes the spread and tradability checks.
ATM_CANDIDATES = 3
DEFAULT_WINDOW_START = time(9, 30)


def zero_line_crosses(bars: list) -> list[Optional[Direction]]:
    """Per bar: "long" for a MACD/signal cross up above zero, "short" for a cross down below zero, else None."""
    result = macd([float(bar.close) for bar in bars])
    if result is None:
        return [None] * len(bars)
    line, signal, _ = result
    crosses: list[Optional[Direction]] = [None]
    for i in range(1, len(bars)):
        if line[i - 1] <= signal[i - 1] and line[i] > signal[i] and line[i] > 0:
            crosses.append("long")
        elif line[i - 1] >= signal[i - 1] and line[i] < signal[i] and line[i] < 0:
            crosses.append("short")
        else:
            crosses.append(None)
    return crosses


class ZeroDteMacd15AtmStrategy(ZeroDteMacdDivergenceStrategy):
    def select_atm_contract(
        self, underlying: str, direction: Direction, price: float, chain: list[OptionQuote]
    ) -> Optional[str]:
        """Return the strike nearest `price` on the right side (calls or puts) with an acceptable spread."""
        side = "call" if direction == "long" else "put"
        contracts = sorted((quote for quote in chain if quote.side == side), key=lambda quote: abs(quote.strike - price))
        if not contracts:
            logger.info("No %s contracts expiring today for %s", side, underlying)
            return None
        for quote in contracts[:ATM_CANDIDATES]:
            if _spread_ok(quote.bid, quote.ask) and self._alpaca_tradable(quote.symbol):
                logger.info("Chose ATM %s for %s: strike %.2f, ask %.2f (spot %.2f)", quote.symbol, underlying, quote.strike, quote.ask, price)
                return quote.symbol
        logger.info("No ATM %s for %s within %.0f%% spread among the %d nearest strikes", side, underlying, MAX_SPREAD * 100, ATM_CANDIDATES)
        return None

    def evaluate(self, symbol: str, now: Optional[datetime] = None) -> Signal | None:
        now = now or datetime.now(timezone.utc)
        today = now.astimezone(MARKET_TZ).date()
        bars = self._closed_bars(symbol, CROSS_MINUTES)
        if len(bars) < MIN_BARS:
            return None
        latest = bars[-1]
        closed_at = latest.timestamp + timedelta(minutes=CROSS_MINUTES)
        if not timedelta(0) <= now - closed_at <= timedelta(minutes=FRESH_MINUTES):
            return None  # only act on the runs right after a 15-min bar closes
        crosses = zero_line_crosses(bars)
        direction = crosses[-1]
        if direction is None:
            return None

        window_start = config.TRADING_START or DEFAULT_WINDOW_START
        for bar, cross in zip(bars[:-1], crosses[:-1]):
            local_close = (bar.timestamp + timedelta(minutes=CROSS_MINUTES)).astimezone(MARKET_TZ)
            if cross is not None and local_close.date() == today and local_close.time() >= window_start:
                logger.info("Skipping %s: today's first 15-min MACD zero-line cross was at %s", symbol, local_close.strftime("%H:%M"))
                return None

        trigger_bars = self._closed_bars(symbol, 1)
        spot = float(trigger_bars[-1].close) if trigger_bars else float(latest.close)
        chain = self.same_day_chain(symbol, today)
        contract = self.select_atm_contract(symbol, direction, spot, chain)
        if contract is None:
            return None
        option_kind = "call" if direction == "long" else "put"
        return Signal(
            contract,
            direction,
            latest.timestamp,
            underlying=symbol,
            description=(
                f"0DTE ATM {option_kind} on {symbol}: 15m MACD cross {'above' if direction == 'long' else 'below'} "
                f"its signal line {'above' if direction == 'long' else 'below'} zero at {closed_at.isoformat()}"
            ),
        )
