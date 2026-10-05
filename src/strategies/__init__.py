"""Strategy registry: start.py picks one by name with --strategy.

Each strategy exposes `from_trade(trade, symbols)` and `generate_signals()`,
returning `Signal`s that risk.py gates and executes.
"""

from .base import Signal
from .macd_psar import MacdPsarStrategy
from .zero_dte_macd_divergence import ZeroDteMacdDivergenceStrategy

STRATEGIES = {
    "macd_psar": MacdPsarStrategy,
    "0dte_macd_divergence": ZeroDteMacdDivergenceStrategy,
}
DEFAULT_STRATEGY = "macd_psar"


def build_strategy(name: str, trade, symbols: tuple[str, ...]):
    return STRATEGIES[name].from_trade(trade, symbols)


__all__ = [
    "DEFAULT_STRATEGY",
    "STRATEGIES",
    "MacdPsarStrategy",
    "Signal",
    "ZeroDteMacdDivergenceStrategy",
    "build_strategy",
]
