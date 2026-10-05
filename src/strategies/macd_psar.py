"""MACD crossover signals confirmed by Parabolic SAR on shorter timeframes."""

import logging
from typing import Literal

from market_data import CoinbaseDataClient
from symbols import normalize_symbol

from .base import Signal, closed_bars
from .indicators import macd, psar_direction

logger = logging.getLogger(__name__)


def _macd_cross(closes: list[float]) -> Literal["long", "short"] | None:
    result = macd(closes)
    if result is None:
        return None

    line, signal, _ = result
    previous_macd, current_macd = line[-2:]
    previous_signal, current_signal = signal[-2:]

    if (
        previous_macd <= previous_signal
        and current_macd > current_signal
        and max(previous_macd, current_macd, previous_signal, current_signal) < 0
    ):
        return "long"
    if (
        previous_macd >= previous_signal
        and current_macd < current_signal
        and min(previous_macd, current_macd, previous_signal, current_signal) > 0
    ):
        return "short"
    return None


def _signal_from_bars(
    symbol: str, macd_bars: list, one_minute_bars: list, five_minute_bars: list
) -> Signal | None:
    direction = _macd_cross([float(bar.close) for bar in macd_bars])
    if direction is None:
        return None
    if psar_direction(one_minute_bars) != direction:
        return None
    if psar_direction(five_minute_bars) != direction:
        return None
    return Signal(
        symbol,
        direction,
        macd_bars[-1].timestamp,
        description=(
            f"MACD {direction} crossover confirmed by 1m and 5m PSAR "
            f"at {macd_bars[-1].timestamp.isoformat()}"
        ),
    )


class MacdPsarStrategy:
    def __init__(self, data_client, symbols: tuple[str, ...], crypto_bars_client=None):
        self.data_client = data_client
        self.crypto_bars_client = crypto_bars_client or CoinbaseDataClient()
        self.symbols = tuple(dict.fromkeys(normalize_symbol(symbol) for symbol in symbols if symbol.strip()))

    @classmethod
    def from_trade(cls, trade, symbols: tuple[str, ...]) -> "MacdPsarStrategy":
        return cls(trade.stock_data, symbols)

    def _closed_bars(self, symbol: str, minutes: int) -> list:
        return closed_bars(self.data_client, self.crypto_bars_client, symbol, minutes)

    def evaluate(self, symbol: str) -> Signal | None:
        macd_bars = self._closed_bars(symbol, 15)
        one_minute_bars = self._closed_bars(symbol, 1)
        five_minute_bars = self._closed_bars(symbol, 5)
        return _signal_from_bars(symbol, macd_bars, one_minute_bars, five_minute_bars)

    def generate_signals(self) -> list[Signal]:
        if not self.symbols:
            logger.info("No strategy symbols configured; skipping strategy")
            return []

        signals = []
        for symbol in self.symbols:
            try:
                signal = self.evaluate(symbol)
                if signal is not None:
                    signals.append(signal)
            except Exception:
                logger.exception("Strategy evaluation failed for %s", symbol)
        return signals
