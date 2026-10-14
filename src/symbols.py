"""Symbol normalization for stocks, crypto pairs, and OCC option contracts."""

import re
from typing import Optional

# OCC option symbol, e.g. SPY261004C00575000: root, YYMMDD expiry, C/P, strike x 1000.
_OPTION_SYMBOL = re.compile(r"^([A-Z]{1,6})(\d{6})([CP])(\d{8})$")


def normalize_symbol(symbol: str) -> str:
    value = symbol.strip().upper()
    if value in ("BTC", "BTCUSD"):
        return "BTC/USD"
    return value


def is_crypto_symbol(symbol: str) -> bool:
    return "/" in normalize_symbol(symbol)


def is_option_symbol(symbol: str) -> bool:
    return _OPTION_SYMBOL.match(normalize_symbol(symbol)) is not None


def option_type(symbol: str) -> Optional[str]:
    """Return "call" or "put" for an OCC option symbol."""
    match = _OPTION_SYMBOL.match(normalize_symbol(symbol))
    if match is None:
        return None
    return "call" if match.group(3) == "C" else "put"


_CRYPTO_QUOTES = ("USDT", "USDC", "USD", "BTC")


def crypto_pair(symbol: str) -> str:
    """Restore the slash Alpaca drops from crypto position symbols, e.g. ETHUSD -> ETH/USD."""
    value = normalize_symbol(symbol)
    if "/" in value:
        return value
    for quote in _CRYPTO_QUOTES:
        if value.endswith(quote) and len(value) > len(quote):
            return f"{value[:-len(quote)]}/{quote}"
    return value
