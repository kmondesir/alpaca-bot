"""0DTE option entries from multi-timeframe MACD divergence.

A direction change needs three timeframes to agree:

1. Setup (5m): regular MACD divergence inside today's session. Price makes a
   lower swing low while the MACD line makes a higher low below zero
   (bullish), or price makes a higher swing high while MACD makes a lower high
   above zero (bearish).
2. Trigger (1m): the MACD line crosses its signal line in the divergence
   direction on the latest closed bar.
3. Momentum (15m): the MACD histogram is turning the same way.

A long view buys the nearest-the-money call expiring today; a short view buys
the nearest-the-money put. Only underlyings with same-day expirations (e.g.
SPY, QQQ, IWM) can trade. Exits are handled by risk.py: take-profit limit,
OPTION_STOP_LOSS, and a forced flatten at OPTION_FLATTEN_TIME.
"""

import logging
from datetime import datetime, time, timezone
from typing import Literal, Optional
from zoneinfo import ZoneInfo

from alpaca.data.requests import OptionLatestQuoteRequest
from alpaca.trading.enums import ContractType
from alpaca.trading.requests import GetOptionContractsRequest

from symbols import is_crypto_symbol, is_option_symbol, normalize_symbol

from .base import Signal, closed_bars
from .indicators import macd

logger = logging.getLogger(__name__)

MARKET_TZ = ZoneInfo("America/New_York")
SESSION_OPEN = time(9, 30)
# Skip the opening auction noise and leave time for a 0DTE trade to work.
ENTRY_START = time(9, 45)
ENTRY_CUTOFF = time(15, 0)

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

Direction = Literal["long", "short"]


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
    """Direction of a MACD/signal cross on the latest bar, at any level."""
    result = macd([float(bar.close) for bar in bars])
    if result is None:
        return None
    _, _, histogram = result
    previous, current = histogram[-2:]
    if previous <= 0 < current:
        return "long"
    if previous >= 0 > current:
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
    def __init__(self, data_client, trading_client, option_data_client, symbols: tuple[str, ...]):
        self.data_client = data_client
        self.trading_client = trading_client
        self.option_data_client = option_data_client
        normalized = dict.fromkeys(normalize_symbol(symbol) for symbol in symbols if symbol.strip())
        unsupported = [symbol for symbol in normalized if is_crypto_symbol(symbol) or is_option_symbol(symbol)]
        for symbol in unsupported:
            logger.warning("0DTE strategy needs an optionable stock or ETF; skipping %s", symbol)
        self.symbols = tuple(symbol for symbol in normalized if symbol not in unsupported)

    @classmethod
    def from_trade(cls, trade, symbols: tuple[str, ...]) -> "ZeroDteMacdDivergenceStrategy":
        return cls(trade.stock_data, trade.client, trade.option_data, symbols)

    def _closed_bars(self, symbol: str, minutes: int) -> list:
        return closed_bars(self.data_client, None, symbol, minutes)

    def select_contract(self, underlying: str, direction: Direction, price: float, today) -> Optional[str]:
        """Return the nearest-the-money contract expiring today with an acceptable spread."""
        contract_type = ContractType.CALL if direction == "long" else ContractType.PUT
        response = self.trading_client.get_option_contracts(
            GetOptionContractsRequest(
                underlying_symbols=[underlying],
                expiration_date=today,
                type=contract_type,
                strike_price_gte=f"{price * (1 - STRIKE_RANGE):.2f}",
                strike_price_lte=f"{price * (1 + STRIKE_RANGE):.2f}",
                limit=500,
            )
        )
        contracts = [contract for contract in (response.option_contracts or []) if contract.tradable]
        if not contracts:
            logger.info("No %s contracts expiring %s for %s", contract_type.value, today, underlying)
            return None

        contracts.sort(key=lambda contract: abs(float(contract.strike_price) - price))
        candidates = [contract.symbol for contract in contracts[:CONTRACT_CANDIDATES]]
        quotes = self.option_data_client.get_option_latest_quote(
            OptionLatestQuoteRequest(symbol_or_symbols=candidates)
        )
        for symbol in candidates:
            quote = quotes.get(symbol)
            if quote is None:
                continue
            bid, ask = float(quote.bid_price or 0), float(quote.ask_price or 0)
            if bid <= 0 or ask <= 0:
                continue
            if (ask - bid) / ((ask + bid) / 2) <= MAX_SPREAD:
                return symbol
        logger.info("No %s contract for %s has a spread within %.0f%%", contract_type.value, underlying, MAX_SPREAD * 100)
        return None

    def evaluate(self, symbol: str, now: Optional[datetime] = None) -> Signal | None:
        local_now = (now or datetime.now(timezone.utc)).astimezone(MARKET_TZ)
        if not ENTRY_START <= local_now.time() < ENTRY_CUTOFF:
            return None
        today = local_now.date()

        setup_bars = self._closed_bars(symbol, SETUP_MINUTES)
        trigger_bars = self._closed_bars(symbol, TRIGGER_MINUTES)
        momentum_bars = self._closed_bars(symbol, MOMENTUM_MINUTES)
        if not trigger_bars:
            return None
        direction = direction_from_bars(setup_bars, trigger_bars, momentum_bars, today)
        if direction is None:
            return None

        trigger_bar = trigger_bars[-1]
        contract = self.select_contract(symbol, direction, float(trigger_bar.close), today)
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
                f"1m MACD cross, 15m histogram turn at {trigger_bar.timestamp.isoformat()}"
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
