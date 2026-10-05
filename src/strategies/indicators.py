"""Indicator math shared by strategies."""

from typing import Literal

MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
PSAR_STEP = 0.02
PSAR_MAX = 0.2


def ema(values: list[float], period: int) -> list[float]:
    multiplier = 2 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append((value - result[-1]) * multiplier + result[-1])
    return result


def macd(closes: list[float]) -> tuple[list[float], list[float], list[float]] | None:
    """Return (macd, signal, histogram) aligned with closes, or None if too short."""
    if len(closes) < MACD_SLOW + MACD_SIGNAL:
        return None
    fast = ema(closes, MACD_FAST)
    slow = ema(closes, MACD_SLOW)
    line = [fast_value - slow_value for fast_value, slow_value in zip(fast, slow)]
    signal = ema(line, MACD_SIGNAL)
    histogram = [line_value - signal_value for line_value, signal_value in zip(line, signal)]
    return line, signal, histogram


def psar_direction(bars: list) -> Literal["long", "short"] | None:
    if len(bars) < 3:
        return None

    highs = [float(bar.high) for bar in bars]
    lows = [float(bar.low) for bar in bars]
    closes = [float(bar.close) for bar in bars]
    rising = closes[1] >= closes[0]
    sar = min(lows[:2]) if rising else max(highs[:2])
    extreme = max(highs[:2]) if rising else min(lows[:2])
    acceleration = PSAR_STEP

    for index in range(2, len(bars)):
        candidate = sar + acceleration * (extreme - sar)
        if rising:
            candidate = min(candidate, lows[index - 1], lows[index - 2])
            if lows[index] < candidate:
                rising = False
                sar = extreme
                extreme = lows[index]
                acceleration = PSAR_STEP
            else:
                sar = candidate
                if highs[index] > extreme:
                    extreme = highs[index]
                    acceleration = min(acceleration + PSAR_STEP, PSAR_MAX)
        else:
            candidate = max(candidate, highs[index - 1], highs[index - 2])
            if highs[index] > candidate:
                rising = True
                sar = extreme
                extreme = highs[index]
                acceleration = PSAR_STEP
            else:
                sar = candidate
                if lows[index] < extreme:
                    extreme = lows[index]
                    acceleration = min(acceleration + PSAR_STEP, PSAR_MAX)

    if rising and closes[-1] > sar:
        return "long"
    if not rising and closes[-1] < sar:
        return "short"
    return None
