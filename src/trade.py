"""Thin wrapper around the Alpaca trading and market data APIs.

Execution only: it opens positions sized in USD, looks orders up, and closes
them by client_order_id. Deciding what to trade is left to the caller.
"""

import logging
import math
import time
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Optional

from alpaca.common.exceptions import APIError
from alpaca.data.historical import (
    CryptoHistoricalDataClient,
    OptionHistoricalDataClient,
    StockHistoricalDataClient,
)
from alpaca.data.requests import (
    CryptoLatestQuoteRequest,
    OptionLatestQuoteRequest,
    StockLatestQuoteRequest,
)
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass, OrderSide, OrderStatus, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, LimitOrderRequest, MarketOrderRequest

import config
from symbols import crypto_pair, is_crypto_symbol, is_option_symbol, normalize_symbol, option_type

logger = logging.getLogger(__name__)

POSITIONS = ("long", "short", "call", "put")
# Alpaca returns at most 500 orders per request.
MAX_ORDERS = 500

# Orders in these states can no longer fill, so there is nothing to cancel.
# (DONE_FOR_DAY is not here: those orders resume the next trading day.)
_DONE_STATUSES = {
    OrderStatus.FILLED,
    OrderStatus.CANCELED,
    OrderStatus.EXPIRED,
    OrderStatus.REJECTED,
    OrderStatus.REPLACED,
}

# How long close_order waits for a cancel to settle before giving up.
_CANCEL_TIMEOUT = 10.0


def _filled(order) -> float:
    return float(order.filled_qty or 0)


def _round_notional(notional: float) -> float:
    """Round a dollar amount down to whole cents."""
    return float(Decimal(str(notional)).quantize(Decimal("0.01"), rounding=ROUND_DOWN))


def _round_to_increment(price: float, increment: float) -> float:
    step = Decimal(str(increment))
    return float((Decimal(str(price)) / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step)


class Trade:
    def __init__(self, key: str, secret: str, url: str):
        """url is the trading API base URL (config.BASE_URL), without /v2."""
        self.client = TradingClient(key, secret, url_override=url)
        self.stock_data = StockHistoricalDataClient(key, secret)
        self.crypto_data = CryptoHistoricalDataClient(key, secret)
        self.option_data = OptionHistoricalDataClient(key, secret)

    def get_balance(self) -> dict:
        """Return the account's cash, equity and buying power."""
        account = self.client.get_account()
        non_marginable_buying_power = getattr(account, "non_marginable_buying_power", None)
        options_buying_power = getattr(account, "options_buying_power", None)
        return {
            "currency": account.currency,
            "cash": float(account.cash),
            "equity": float(account.equity),
            "buying_power": float(account.buying_power),
            "non_marginable_buying_power": (
                float(non_marginable_buying_power)
                if non_marginable_buying_power is not None
                else 0.0
            ),
            "options_buying_power": (
                float(options_buying_power) if options_buying_power is not None else 0.0
            ),
            "portfolio_value": float(account.portfolio_value),
        }

    def get_orders(self, limit: int = 100) -> list:
        """Return the most recent orders in any status, newest first."""
        return self.client.get_orders(
            filter=GetOrdersRequest(status=QueryOrderStatus.ALL, limit=min(limit, MAX_ORDERS), nested=False)
        )

    def get_order(self, client_order_id: str):
        """Look an order up by its client_order_id."""
        return self.client.get_order_by_client_id(client_order_id)

    def open_position(
        self,
        asset: str,
        amount: float,
        position: str,
        client_order_id: Optional[str] = None,
    ):
        """Open a position worth about amount USD and return the submitted order.

        position is "long" or "short" for a stock or crypto pair, and "call" or
        "put" for an OCC option contract symbol (e.g. SPY261014C00580000),
        which is bought to open.
        client_order_id defaults to a random UUID.
        """
        symbol = normalize_symbol(asset)
        position = position.lower()
        if position not in POSITIONS:
            raise ValueError(f"position must be one of {', '.join(POSITIONS)}, not {position!r}")
        if amount <= 0:
            raise ValueError("amount must be positive")
        client_order_id = client_order_id or config.new_client_order_id()

        if position in ("call", "put"):
            if not is_option_symbol(symbol):
                raise ValueError(f"{position} needs an OCC option symbol such as SPY261014C00580000, not {symbol}")
            if option_type(symbol) != position:
                raise ValueError(f"{symbol} is a {option_type(symbol)}, not a {position}")
            return self._buy_option(symbol, amount, client_order_id)
        if is_option_symbol(symbol):
            raise ValueError(f"{symbol} is an option contract; use --position {option_type(symbol)}")
        if position == "short":
            return self._open_short(symbol, amount, client_order_id)

        notional = _round_notional(amount)
        if notional <= 0:
            raise ValueError("amount must be at least $0.01")
        time_in_force = TimeInForce.GTC if is_crypto_symbol(symbol) else TimeInForce.DAY
        return self._submit(
            MarketOrderRequest(
                symbol=symbol,
                notional=notional,
                side=OrderSide.BUY,
                time_in_force=time_in_force,
                client_order_id=client_order_id,
            )
        )

    def _open_short(self, symbol: str, amount: float, client_order_id: str):
        """Sell short the shares amount buys at the bid; notional shorts aren't allowed."""
        if is_crypto_symbol(symbol):
            raise ValueError("Crypto short selling is unsupported")
        asset = self.get_asset_data(symbol)
        if not asset["shortable"]:
            raise RuntimeError(f"{symbol} is not shortable")
        bid = float(asset["bid"] or 0)
        if bid <= 0:
            raise RuntimeError(f"No valid bid price available to short {symbol}")
        scale = 1_000_000 if asset["fractionable"] else 1
        qty = math.floor(amount / bid * scale) / scale
        if qty <= 0:
            raise RuntimeError(f"${amount} is too small to short one share of {symbol} at {bid}")
        return self._submit(
            MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
                client_order_id=client_order_id,
            )
        )

    def _buy_option(self, symbol: str, amount: float, client_order_id: str):
        """Buy the whole contracts amount covers with a day limit order at the ask."""
        ask = self.get_option_quote(symbol)["ask"]
        if ask <= 0:
            raise RuntimeError(f"No valid ask price available for {symbol}")
        qty = math.floor(amount / (ask * 100))
        if qty < 1:
            raise RuntimeError(f"${amount} is too small to buy one {symbol} contract at {ask} (${ask * 100:.2f})")
        return self._submit(
            LimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
                limit_price=_round_to_increment(ask, 0.01),
                client_order_id=client_order_id,
            )
        )

    def _submit(self, request):
        order = self.client.submit_order(order_data=request)
        logger.info(
            "Placed %s %s %s %s (%s, %s)",
            order.order_type.value,
            order.side.value,
            order.qty or f"${order.notional}",
            order.symbol,
            order.id,
            order.client_order_id,
        )
        return order

    def close_order(self, order_id: str, client_order_id: Optional[str] = None):
        """Close the order whose client_order_id is order_id and return the result.

        A working order is cancelled first. If nothing had filled, the cancelled
        order is returned. Otherwise the filled quantity is closed with an
        opposite market order, which is returned; client_order_id tags it and
        defaults to a random UUID.
        """
        order = self.get_order(order_id)
        if order.status not in _DONE_STATUSES:
            if order.status != OrderStatus.PENDING_CANCEL:
                self.client.cancel_order_by_id(order.id)
            order = self._wait_until_done(order.id)
            if order.status not in _DONE_STATUSES:
                raise RuntimeError(f"Cancel of {order_id} is still pending ({order.status.value}); retry")
            logger.info("Cancelled %s (%s)", order.id, order.status.value)

        filled = _filled(order)
        if filled == 0:
            return order

        position = self._find_position(order.symbol)
        if position is None:
            raise RuntimeError(f"No open {order.symbol} position left to close for {order_id}")
        held_side = getattr(position.side, "value", str(position.side)).lower()
        expected_side = "long" if order.side == OrderSide.BUY else "short"
        if held_side != expected_side:
            raise RuntimeError(f"{order.symbol} position is {held_side}, so {order_id} ({order.side.value}) is already closed")
        # Crypto fees come out of the purchased coin, so less than filled may be held.
        qty = min(filled, abs(float(position.qty_available or 0)))
        if qty <= 0:
            raise RuntimeError(f"No available {order.symbol} to close for {order_id}; another order may hold it")

        closing = self._submit(
            MarketOrderRequest(
                symbol=order.symbol,
                qty=qty,
                side=OrderSide.SELL if order.side == OrderSide.BUY else OrderSide.BUY,
                time_in_force=TimeInForce.GTC if is_crypto_symbol(order.symbol) else TimeInForce.DAY,
                client_order_id=client_order_id or config.new_client_order_id(),
            )
        )
        logger.info("Closing %s of %s for %s", qty, order.symbol, order_id)
        return closing

    def _wait_until_done(self, order_id, timeout: float = _CANCEL_TIMEOUT):
        """Poll until the order reaches a final status or timeout seconds pass."""
        deadline = time.monotonic() + timeout
        order = self.client.get_order_by_id(order_id)
        while order.status not in _DONE_STATUSES and time.monotonic() < deadline:
            time.sleep(0.5)
            order = self.client.get_order_by_id(order_id)
        return order

    def _find_position(self, symbol: str):
        """Return the open position for symbol, matching crypto pairs to Alpaca's unslashed form."""
        target = normalize_symbol(symbol)
        for position in self.client.get_all_positions():
            held = (
                crypto_pair(position.symbol)
                if position.asset_class == AssetClass.CRYPTO
                else normalize_symbol(position.symbol)
            )
            if held == target:
                return position
        return None

    def get_option_quote(self, symbol: str) -> dict:
        """Return the latest bid/ask for an OCC option contract."""
        symbol = normalize_symbol(symbol)
        quote = self.option_data.get_option_latest_quote(
            OptionLatestQuoteRequest(symbol_or_symbols=symbol)
        )[symbol]
        return {"bid": float(quote.bid_price or 0), "ask": float(quote.ask_price or 0)}

    def get_asset_data(self, symbol: str) -> dict:
        """Return asset details plus the latest bid/ask quote."""
        symbol = normalize_symbol(symbol)
        asset = self.client.get_asset(symbol)
        if asset.asset_class == AssetClass.CRYPTO:
            quotes = self.crypto_data.get_crypto_latest_quote(
                CryptoLatestQuoteRequest(symbol_or_symbols=symbol)
            )
        else:
            quotes = self.stock_data.get_stock_latest_quote(
                StockLatestQuoteRequest(symbol_or_symbols=symbol)
            )
        quote = quotes[symbol]
        return {
            "symbol": asset.symbol,
            "name": asset.name,
            "asset_class": asset.asset_class.value,
            "exchange": asset.exchange.value,
            "tradable": asset.tradable,
            "fractionable": asset.fractionable,
            "shortable": asset.shortable,
            "bid": quote.bid_price,
            "ask": quote.ask_price,
            "quote_time": quote.timestamp,
        }
