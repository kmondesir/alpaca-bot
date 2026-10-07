"""0DTE option entries from multi-timeframe MACD divergence.

A direction change needs three timeframes to agree:

1. Setup (5m): regular MACD divergence inside today's session. Price makes a
   lower swing low while the MACD line makes a higher low below zero
   (bullish), or price makes a higher swing high while MACD makes a lower high
   above zero (bearish).
2. Trigger (1m): the MACD line crosses its signal line in the divergence
   direction on the latest closed bar, below zero for calls and above zero
   for puts.
3. Momentum (15m): the MACD histogram is turning the same way.

With GEX_FILTER on, a signal is only traded when dealer net gamma exposure
(GEX) on today's chain is negative (below -GEX_THRESHOLD). In negative gamma,
dealer hedging amplifies moves rather than damping them. GEX sums
gamma x open interest x 100 x spot^2 x 1% over contracts expiring today, with
calls positive and puts negative. If GEX can't be computed, the trade is skipped.

A long view buys the nearest-the-money call expiring today; a short view buys
the nearest-the-money put. Only underlyings with same-day expirations (e.g.
SPY, QQQ, IWM, or the indexes SPX and XSP) can trade. Exits are handled by
risk.py: take-profit limit, a STOP_LOSS trailing stop moved each run, and a
forced flatten at OPTION_FLATTEN_TIME.

Index bars (SPX, XSP, from --indexes) come from Yahoo Finance; stock bars from
Alpaca. Today's option chain (open interest, gamma, bid/ask) is fetched once a
direction is found: from MarketData.app when MARKETDATA_API_KEY is set,
otherwise from Yahoo Finance, whose gamma is Black-Scholes gamma from implied
volatility. The chosen contract must also be tradable on Alpaca.
"""

import logging
from datetime import datetime, time, timezone
from typing import Literal, Optional
from zoneinfo import ZoneInfo

from alpaca.common.exceptions import APIError

import config
from market_data import MarketDataAppClient, OptionQuote, YahooFinanceDataClient, YahooOptionChainClient
from symbols import is_crypto_symbol, is_option_symbol, normalize_symbol

from .base import Signal, closed_bars, closed_index_bars
from .indicators import macd

logger = logging.getLogger(__name__)

MARKET_TZ = ZoneInfo("America/New_York")
SESSION_OPEN = time(9, 30)

SETUP_MINUTES = 5
TRIGGER_MINUTES = 1
MOMENTUM_MINUTES = 15
# Bars on each side that a swing high/low must beat.
PIVOT_WINDOW = 2
# The second divergence pivot must be at most this many setup bars old.
MAX_DIVERGENCE_AGE = 6
# Strikes considered on each side of the underlying price.
STRIKE_RANGE = 0.03
CONTRACT_CANDIDATES = 5
# Maximum (ask - bid) / mid for a contract to be tradable.
MAX_SPREAD = 0.15
# Monthly index options settle at the open, so they stop trading the day before
# they expire; same-day entries use the PM-settled roots (SPXW, NDXP, RUTW).
AM_SETTLED_ROOTS = {"SPX", "NDX", "RUT"}

Direction = Literal["long", "short"]


def _option_root(contract_symbol: str) -> str:
    """OCC root, e.g. SPXW from SPXW261007C07800000 (expiry, type and strike are 15 characters)."""
    return contract_symbol[:-15]


def _spread_ok(bid: float, ask: float) -> bool:
    return bid > 0 and ask > 0 and (ask - bid) / ((ask + bid) / 2) <= MAX_SPREAD


def chain_gex(chain: list[OptionQuote], spot: float) -> Optional[float]:
    """Dealer net GEX in dollars per 1% move from a MarketData.app chain, or None without data."""
    counted = [quote for quote in chain if quote.open_interest and quote.gamma is not None]
    if not counted:
        return None
    return sum(
        (1 if quote.side == "call" else -1) * quote.gamma * quote.open_interest * 100 * spot * spot * 0.01
        for quote in counted
    )


def _pivots(values: list[float], start: int, kind: Literal["low", "high"]) -> list[int]:
    """Indices from `start` that are the lowest/highest within PIVOT_WINDOW bars each side."""
    pivots = []
    for index in range(max(start, PIVOT_WINDOW), len(values) - PIVOT_WINDOW):
        window = values[index - PIVOT_WINDOW : index + PIVOT_WINDOW + 1]
        if values[index] == (min(window) if kind == "low" else max(window)):
            pivots.append(index)
    return pivots


def find_divergence(bars: list, session_start: int) -> tuple[Direction, int] | None:
    """Return (direction, pivot index) for the most recent regular MACD divergence."""
    result = macd([float(bar.close) for bar in bars])
    if result is None:
        return None
    line = result[0]
    lows = [float(bar.low) for bar in bars]
    highs = [float(bar.high) for bar in bars]
    newest = len(bars) - 1
    found = []

    low_pivots = _pivots(lows, session_start, "low")
    if len(low_pivots) >= 2:
        first, second = low_pivots[-2:]
        if lows[second] < lows[first] and line[second] > line[first] and line[second] < 0:
            found.append(("long", second))

    high_pivots = _pivots(highs, session_start, "high")
    if len(high_pivots) >= 2:
        first, second = high_pivots[-2:]
        if highs[second] > highs[first] and line[second] < line[first] and line[second] > 0:
            found.append(("short", second))

    found = [item for item in found if newest - item[1] <= MAX_DIVERGENCE_AGE]
    if not found:
        return None
    return max(found, key=lambda item: item[1])


def macd_cross(bars: list) -> Direction | None:
    """Direction of a MACD/signal cross on the latest bar: bullish below zero, bearish above."""
    result = macd([float(bar.close) for bar in bars])
    if result is None:
        return None
    line, signal, histogram = result
    previous, current = histogram[-2:]
    levels = line[-2:] + signal[-2:]
    if previous <= 0 < current and max(levels) < 0:
        return "long"
    if previous >= 0 > current and min(levels) > 0:
        return "short"
    return None


def histogram_turning(bars: list) -> Direction | None:
    result = macd([float(bar.close) for bar in bars])
    if result is None:
        return None
    previous, current = result[2][-2:]
    if current > previous:
        return "long"
    if current < previous:
        return "short"
    return None


def session_start_index(bars: list, today) -> int:
    """Index of the first bar in today's regular session, or len(bars) if none."""
    for index, bar in enumerate(bars):
        local = bar.timestamp.astimezone(MARKET_TZ)
        if local.date() == today and local.time() >= SESSION_OPEN:
            return index
    return len(bars)


def direction_from_bars(
    setup_bars: list, trigger_bars: list, momentum_bars: list, today
) -> Direction | None:
    divergence = find_divergence(setup_bars, session_start_index(setup_bars, today))
    if divergence is None:
        return None
    direction = divergence[0]
    if macd_cross(trigger_bars) != direction:
        return None
    if histogram_turning(momentum_bars) != direction:
        return None
    return direction


class ZeroDteMacdDivergenceStrategy:
    def __init__(
        self,
        data_client,
        trading_client,
        option_chain_client,
        symbols: tuple[str, ...],
        indexes: tuple[str, ...] = (),
        index_data_client=None,
    ):
        self.data_client = data_client
        self.trading_client = trading_client
        self.option_chain_client = option_chain_client
        self.index_data_client = index_data_client
        normalized = dict.fromkeys(normalize_symbol(symbol) for symbol in symbols if symbol.strip())
        unsupported = [symbol for symbol in normalized if is_crypto_symbol(symbol) or is_option_symbol(symbol)]
        for symbol in unsupported:
            logger.warning("0DTE strategy needs an optionable stock or ETF; skipping %s", symbol)
        self.indexes = tuple(dict.fromkeys(normalize_symbol(symbol) for symbol in indexes if symbol.strip()))
        stocks = tuple(symbol for symbol in normalized if symbol not in unsupported and symbol not in self.indexes)
        self.symbols = stocks + self.indexes

    @classmethod
    def from_trade(
        cls, trade, symbols: tuple[str, ...], indexes: tuple[str, ...] = ()
    ) -> "ZeroDteMacdDivergenceStrategy":
        option_chain_client = MarketDataAppClient() if config.MARKETDATA_API_KEY else YahooOptionChainClient()
        return cls(trade.stock_data, trade.client, option_chain_client, symbols, indexes, YahooFinanceDataClient())

    def _closed_bars(self, symbol: str, minutes: int) -> list:
        if symbol in self.indexes:
            return closed_index_bars(self.index_data_client, symbol, minutes)
        return closed_bars(self.data_client, None, symbol, minutes)

    def same_day_chain(self, underlying: str, today) -> list[OptionQuote]:
        """Contracts on `underlying` expiring today, minus AM-settled index options that no longer trade."""
        chain = self.option_chain_client.get_option_chain(underlying, today, index=underlying in self.indexes)
        return [quote for quote in chain if _option_root(quote.symbol) not in AM_SETTLED_ROOTS]

    def gex_allows(self, underlying: str, spot: float, chain: list[OptionQuote]) -> bool:
        if not config.GEX_FILTER:
            return True
        gex = chain_gex(chain, spot)
        if gex is None:
            logger.info("Skipping %s: no gamma/open interest data to compute GEX", underlying)
            return False
        if gex >= -config.GEX_THRESHOLD:
            logger.info("Skipping %s: net GEX %.0f is not below %.0f", underlying, gex, -config.GEX_THRESHOLD)
            return False
        logger.info("Net GEX for %s is %.0f (negative gamma); trading", underlying, gex)
        return True

    def _alpaca_tradable(self, contract_symbol: str) -> bool:
        try:
            return bool(self.trading_client.get_option_contract(contract_symbol).tradable)
        except APIError as error:
            logger.info("Alpaca has no tradable %s: %s", contract_symbol, error)
            return False

    def select_contract(
        self, underlying: str, direction: Direction, price: float, chain: list[OptionQuote]
    ) -> Optional[str]:
        """Return the nearest-the-money contract expiring today with an acceptable spread."""
        side = "call" if direction == "long" else "put"
        contracts = [
            quote for quote in chain if quote.side == side and abs(quote.strike - price) <= price * STRIKE_RANGE
        ]
        if not contracts:
            logger.info("No %s contracts expiring today for %s", side, underlying)
            return None

        contracts.sort(key=lambda quote: abs(quote.strike - price))
        for quote in contracts[:CONTRACT_CANDIDATES]:
            if _spread_ok(quote.bid, quote.ask) and self._alpaca_tradable(quote.symbol):
                return quote.symbol
        logger.info("No %s contract for %s has a spread within %.0f%%", side, underlying, MAX_SPREAD * 100)
        return None

    def evaluate(self, symbol: str, now: Optional[datetime] = None) -> Signal | None:
        today = (now or datetime.now(timezone.utc)).astimezone(MARKET_TZ).date()

        setup_bars = self._closed_bars(symbol, SETUP_MINUTES)
        trigger_bars = self._closed_bars(symbol, TRIGGER_MINUTES)
        momentum_bars = self._closed_bars(symbol, MOMENTUM_MINUTES)
        if not trigger_bars:
            return None
        direction = direction_from_bars(setup_bars, trigger_bars, momentum_bars, today)
        if direction is None:
            return None

        trigger_bar = trigger_bars[-1]
        spot = float(trigger_bar.close)
        # Fetched only once a direction is found: MarketData.app charges a credit per contract.
        chain = self.same_day_chain(symbol, today)
        if not self.gex_allows(symbol, spot, chain):
            return None
        contract = self.select_contract(symbol, direction, spot, chain)
        if contract is None:
            return None
        option_kind = "call" if direction == "long" else "put"
        return Signal(
            contract,
            direction,
            trigger_bar.timestamp,
            underlying=symbol,
            description=(
                f"0DTE {option_kind} on {symbol}: 5m MACD {direction} divergence, "
                f"1m MACD cross {'below' if direction == 'long' else 'above'} zero, "
                f"15m histogram turn at {trigger_bar.timestamp.isoformat()}"
            ),
        )

    def generate_signals(self) -> list[Signal]:
        if not self.symbols:
            logger.info("No 0DTE strategy symbols configured; skipping strategy")
            return []

        signals = []
        for symbol in self.symbols:
            try:
                signal = self.evaluate(symbol)
                if signal is not None:
                    signals.append(signal)
            except Exception:
                logger.exception("0DTE strategy evaluation failed for %s", symbol)
        return signals
