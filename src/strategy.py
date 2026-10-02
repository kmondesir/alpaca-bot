"""MACD crossover signals confirmed by Parabolic SAR on shorter timeframes."""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from alpaca.common.enums import Sort
from alpaca.data.enums import DataFeed
from alpaca.data.requests import CryptoBarsRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from symbols import is_crypto_symbol, normalize_symbol

logger = logging.getLogger(__name__)

_MACD_FAST = 12
_MACD_SLOW = 26
_MACD_SIGNAL = 9
_PSAR_STEP = 0.02
_PSAR_MAX = 0.2
_BAR_LIMIT = 100


@dataclass(frozen=True)
class Signal:
    symbol: str
    direction: Literal["long", "short"]
    bar_timestamp: datetime

    @property
    def side(self) -> Literal["buy", "sell"]:
        return "buy" if self.direction == "long" else "sell"


def _ema(values: list[float], period: int) -> list[float]:
    multiplier = 2 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append((value - result[-1]) * multiplier + result[-1])
    return result


def _macd_cross(closes: list[float]) -> Literal["long", "short"] | None:
    if len(closes) < _MACD_SLOW + _MACD_SIGNAL:
        return None

    fast = _ema(closes, _MACD_FAST)
    slow = _ema(closes, _MACD_SLOW)
    macd = [fast_value - slow_value for fast_value, slow_value in zip(fast, slow)]
    signal = _ema(macd, _MACD_SIGNAL)
    previous_macd, current_macd = macd[-2:]
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


def _psar_direction(bars: list) -> Literal["long", "short"] | None:
    if len(bars) < 3:
        return None

    highs = [float(bar.high) for bar in bars]
    lows = [float(bar.low) for bar in bars]
    closes = [float(bar.close) for bar in bars]
    rising = closes[1] >= closes[0]
    sar = min(lows[:2]) if rising else max(highs[:2])
    extreme = max(highs[:2]) if rising else min(lows[:2])
    acceleration = _PSAR_STEP

    for index in range(2, len(bars)):
        candidate = sar + acceleration * (extreme - sar)
        if rising:
            candidate = min(candidate, lows[index - 1], lows[index - 2])
            if lows[index] < candidate:
                rising = False
                sar = extreme
                extreme = lows[index]
                acceleration = _PSAR_STEP
            else:
                sar = candidate
                if highs[index] > extreme:
                    extreme = highs[index]
                    acceleration = min(acceleration + _PSAR_STEP, _PSAR_MAX)
        else:
            candidate = max(candidate, highs[index - 1], highs[index - 2])
            if highs[index] > candidate:
                rising = True
                sar = extreme
                extreme = highs[index]
                acceleration = _PSAR_STEP
            else:
                sar = candidate
                if lows[index] < extreme:
                    extreme = lows[index]
                    acceleration = min(acceleration + _PSAR_STEP, _PSAR_MAX)

    if rising and closes[-1] > sar:
        return "long"
    if not rising and closes[-1] < sar:
        return "short"
    return None


def _signal_from_bars(
    symbol: str, macd_bars: list, one_minute_bars: list, five_minute_bars: list
) -> Signal | None:
    direction = _macd_cross([float(bar.close) for bar in macd_bars])
    if direction is None:
        return None
    if _psar_direction(one_minute_bars) != direction:
        return None
    if _psar_direction(five_minute_bars) != direction:
        return None
    return Signal(symbol, direction, macd_bars[-1].timestamp)


class MacdPsarStrategy:
    def __init__(self, data_client, symbols: tuple[str, ...], crypto_data_client=None):
        self.data_client = data_client
        self.crypto_data_client = crypto_data_client
        self.symbols = tuple(dict.fromkeys(normalize_symbol(symbol) for symbol in symbols if symbol.strip()))

    def _closed_bars(self, symbol: str, minutes: int) -> list:
        now = datetime.now(timezone.utc)
        if is_crypto_symbol(symbol):
            if self.crypto_data_client is None:
                raise RuntimeError("A crypto data client is required for crypto symbols")
            request = CryptoBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame(minutes, TimeFrameUnit.Minute),
                start=now - timedelta(days=14),
                end=now,
                limit=_BAR_LIMIT,
                sort=Sort.DESC,
            )
            response = self.crypto_data_client.get_crypto_bars(request)
        else:
            request = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame(minutes, TimeFrameUnit.Minute),
                start=now - timedelta(days=14),
                end=now,
                limit=_BAR_LIMIT,
                sort=Sort.DESC,
                feed=DataFeed.IEX,
            )
            response = self.data_client.get_stock_bars(request)
        data = response.data if hasattr(response, "data") else response
        bars = next(
            (values for key, values in data.items() if normalize_symbol(key) == normalize_symbol(symbol)),
            [],
        )
        bars = list(reversed(bars))
        return [bar for bar in bars if bar.timestamp + timedelta(minutes=minutes) <= now]

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
