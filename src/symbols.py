"""Symbol normalization shared by strategy, risk, and order code."""


def normalize_symbol(symbol: str) -> str:
    value = symbol.strip().upper()
    if value in ("BTC", "BTCUSD"):
        return "BTC/USD"
    return value


def is_crypto_symbol(symbol: str) -> bool:
    return "/" in normalize_symbol(symbol)

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
