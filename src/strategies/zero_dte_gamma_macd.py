"""0DTE options on the most-traded underlying in positive gamma with a 15-minute MACD zero-line cross.

Each run, every underlying in --assets is checked for a 15-minute MACD(12, 26,
9) cross on the bar that just closed: the MACD line crossing above its signal
line while above zero is a call, crossing below while below zero is a put.
Underlyings with a cross are ranked by average daily volume over the last
ADV_DAYS sessions, highest first. The first one that has options expiring
today (supports 0DTE) and whose dealer net gamma exposure is positive (above
GEX_THRESHOLD) is traded; at most one signal is returned per run.

In positive gamma, dealer hedging damps moves, so this strategy expects the
cross to carry on in an orderly trend. GEX is computed as in the 0DTE MACD
divergence strategy (gamma x open interest x 100 x spot^2 x 1% over today's
contracts, calls positive, puts negative); if it can't be computed, the
underlying is skipped. GEX_FILTER does not apply: positive gamma is always
required here.

The contract, chains and sizing work as in the 0DTE MACD divergence strategy
(the MIN_DELTA/MAX_DELTA out-of-the-money contract closest to 0.50 delta).

Exits ignore TAKE_PROFIT and STOP_LOSS: a call is sold once the underlying's
1-minute MACD crosses below its signal line while below zero (a put once it
crosses above while above zero), counting only bars that close after entry.
Without a reversal, the option is held until OPTION_FLATTEN_TIME. risk.py
checks this each run for entries tagged with MACD_REVERSAL_EXIT.

Whether an underlying has a same-day expiry is read from today's chain: in
2026, AAPL, MSFT, AMZN, META, AVGO and TSLA list Monday, Wednesday and Friday
expirations, while GOOG lists Fridays only.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from alpaca.data.enums import DataFeed
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

import config
from symbols import normalize_symbol, option_type, option_underlying

from .base import Signal, closed_bars
from .zero_dte_macd15_atm import CROSS_MINUTES, FRESH_MINUTES, MIN_BARS, zero_line_crosses
from .zero_dte_macd_divergence import MARKET_TZ, Direction, ZeroDteMacdDivergenceStrategy, chain_gex

logger = logging.getLogger(__name__)

# Sessions averaged for the volume ranking.
ADV_DAYS = 20
# Exit rule stored with each entry; risk.py calls macd_reversal_exit for it.
MACD_REVERSAL_EXIT = "macd_1m_reversal"
EXIT_MINUTES = 1


def macd_reversal_exit(data_client, contract: str, entered_at: datetime) -> Optional[str]:
    """Why `contract` should be sold, if the underlying's 1-minute MACD has crossed against it since entry."""
    bars = closed_bars(data_client, None, option_underlying(contract), EXIT_MINUTES)
    if len(bars) < MIN_BARS:
        return None
    against = "short" if option_type(contract) == "call" else "long"
    for bar, cross in zip(bars, zero_line_crosses(bars)):
        closed_at = bar.timestamp + timedelta(minutes=EXIT_MINUTES)
        if cross == against and closed_at > entered_at:
            level = "below" if against == "short" else "above"
            return f"1m MACD crossed {level} its signal line {level} zero at {closed_at.isoformat()}"
    return None


class ZeroDteGammaMacdStrategy(ZeroDteMacdDivergenceStrategy):
    def average_daily_volumes(self, symbols: list[str], now: datetime) -> dict[str, float]:
        """Mean daily volume over the last ADV_DAYS completed sessions, 0 for a symbol without bars."""
        today_open = datetime.combine(now.astimezone(MARKET_TZ).date(), datetime.min.time(), MARKET_TZ)
        response = self.data_client.get_stock_bars(
            StockBarsRequest(
                symbol_or_symbols=symbols,
                timeframe=TimeFrame.Day,
                start=today_open - timedelta(days=ADV_DAYS * 2 + 10),
                end=today_open,
                feed=DataFeed.IEX,
            )
        )
        data = response.data if hasattr(response, "data") else response
        volumes = {normalize_symbol(key): [float(bar.volume) for bar in bars][-ADV_DAYS:] for key, bars in data.items()}
        return {symbol: sum(volumes.get(symbol, [])) / len(volumes[symbol]) if volumes.get(symbol) else 0.0 for symbol in symbols}

    def fresh_cross(self, symbol: str, now: datetime) -> tuple[Direction, list] | None:
        """(direction, bars) when the 15-minute bar that just closed has a MACD zero-line cross."""
        bars = self._closed_bars(symbol, CROSS_MINUTES)
        if len(bars) < MIN_BARS:
            return None
        closed_at = bars[-1].timestamp + timedelta(minutes=CROSS_MINUTES)
        if not timedelta(0) <= now - closed_at <= timedelta(minutes=FRESH_MINUTES):
            return None  # only act on the runs right after a 15-min bar closes
        direction = zero_line_crosses(bars)[-1]
        return None if direction is None else (direction, bars)

    def positive_gamma(self, underlying: str, spot: float, chain) -> bool:
        gex = chain_gex(chain, spot)
        if gex is None:
            logger.info("Skipping %s: no gamma/open interest data to compute GEX", underlying)
            return False
        if gex <= config.GEX_THRESHOLD:
            logger.info("Skipping %s: net GEX %.0f is not above %.0f", underlying, gex, config.GEX_THRESHOLD)
            return False
        logger.info("Net GEX for %s is %.0f (positive gamma)", underlying, gex)
        return True

    def generate_signals(self, now: Optional[datetime] = None) -> list[Signal]:
        if not self.symbols:
            logger.info("No 0DTE strategy symbols configured; skipping strategy")
            return []
        now = now or datetime.now(timezone.utc)
        today = now.astimezone(MARKET_TZ).date()

        crosses = {}
        for symbol in self.symbols:
            try:
                cross = self.fresh_cross(symbol, now)
            except Exception:
                logger.exception("0DTE gamma MACD cross check failed for %s", symbol)
                continue
            if cross is not None:
                crosses[symbol] = cross
        if not crosses:
            return []

        volumes = self.average_daily_volumes(list(crosses), now)
        ranked = sorted(crosses, key=lambda symbol: volumes[symbol], reverse=True)
        logger.info(
            "15m MACD zero-line crosses by average daily volume: %s",
            ", ".join(f"{symbol} {crosses[symbol][0]} ({volumes[symbol]:,.0f})" for symbol in ranked),
        )
        for symbol in ranked:
            direction, bars = crosses[symbol]
            try:
                chain = self.same_day_chain(symbol, today)
                if not chain:
                    logger.info("Skipping %s: no options expire today (no 0DTE)", symbol)
                    continue
                trigger_bars = self._closed_bars(symbol, 1)
                spot = float(trigger_bars[-1].close) if trigger_bars else float(bars[-1].close)
                if not self.positive_gamma(symbol, spot, chain):
                    continue
                contract = self.select_contract(symbol, direction, spot, chain)
            except Exception:
                logger.exception("0DTE gamma MACD evaluation failed for %s", symbol)
                continue
            if contract is None:
                continue
            option_kind = "call" if direction == "long" else "put"
            closed_at = bars[-1].timestamp + timedelta(minutes=CROSS_MINUTES)
            return [
                Signal(
                    contract,
                    direction,
                    bars[-1].timestamp,
                    underlying=symbol,
                    exit_rule=MACD_REVERSAL_EXIT,
                    description=(
                        f"0DTE {option_kind} on {symbol}: top average daily volume in positive gamma, "
                        f"15m MACD cross {'above' if direction == 'long' else 'below'} zero at {closed_at.isoformat()}"
                    ),
                )
            ]
        return []
