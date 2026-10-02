"""Symbol normalization shared by strategy, risk, and order code."""


def normalize_symbol(symbol: str) -> str:
    value = symbol.strip().upper()
    if value in ("BTC", "BTCUSD"):
        return "BTC/USD"
    return value


def is_crypto_symbol(symbol: str) -> bool:
    return "/" in normalize_symbol(symbol)