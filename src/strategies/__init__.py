"""Strategy registry: start.py picks one by name with --strategy.

Each strategy exposes `from_trade(trade, symbols)` and `generate_signals()`,
returning `Signal`s that risk.py gates and executes.
"""

from .base import Signal
from .macd_psar import MacdPsarStrategy
from .zero_dte_macd15_atm import ZeroDteMacd15AtmStrategy
from .zero_dte_macd_divergence import ZeroDteMacdDivergenceStrategy

STRATEGIES = {
    "macd_psar": MacdPsarStrategy,
    "0dte_macd_divergence": ZeroDteMacdDivergenceStrategy,
    "0dte_macd15_atm": ZeroDteMacd15AtmStrategy,
}
DEFAULT_STRATEGY = "macd_psar"
# Strategies that accept --indexes (index underlyings such as SPX).
INDEX_STRATEGIES = {"0dte_macd_divergence", "0dte_macd15_atm"}


def build_strategy(name: str, trade, symbols: tuple[str, ...], indexes: tuple[str, ...] = ()):
    if indexes:
        return STRATEGIES[name].from_trade(trade, symbols, indexes)
    return STRATEGIES[name].from_trade(trade, symbols)


__all__ = [
    "DEFAULT_STRATEGY",
    "INDEX_STRATEGIES",
    "STRATEGIES",
    "MacdPsarStrategy",
    "Signal",
    "ZeroDteMacd15AtmStrategy",
    "ZeroDteMacdDivergenceStrategy",
    "build_strategy",
]
