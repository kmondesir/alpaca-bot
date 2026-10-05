"""Symbol normalization shared by strategy, risk, and order code."""

import re
from datetime import date
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


def option_underlying(symbol: str) -> Optional[str]:
    match = _OPTION_SYMBOL.match(normalize_symbol(symbol))
    return match.group(1) if match else None


def option_expiration(symbol: str) -> Optional[date]:
    match = _OPTION_SYMBOL.match(normalize_symbol(symbol))
    if match is None:
        return None
    value = match.group(2)
    return date(2000 + int(value[:2]), int(value[2:4]), int(value[4:]))


def option_type(symbol: str) -> Optional[str]:
    """Return "call" or "put" for an OCC option symbol."""
    match = _OPTION_SYMBOL.match(normalize_symbol(symbol))
    if match is None:
        return None
    return "call" if match.group(3) == "C" else "put"
