"""Thin wrapper around the Alpaca trading and market data APIs.

Every order placed, cancelled or closed through Trade is recorded in the
orders table (see db.py), so later cron runs know what earlier runs did.
"""

import logging
from typing import Optional

from alpaca.common.exceptions import APIError
from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import CryptoLatestQuoteRequest, StockLatestQuoteRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass, OrderSide, OrderStatus, TimeInForce
from alpaca.trading.requests import (
    ClosePositionRequest,
    LimitOrderRequest,
    MarketOrderRequest,
)

import config
from db import PENDING_STATUSES, OrderDB

logger = logging.getLogger(__name__)

# Orders in these states can no longer fill, so there is nothing to cancel.
# (DONE_FOR_DAY is not here: those orders resume the next trading day.)
_DONE_STATUSES = {
    OrderStatus.FILLED,
    OrderStatus.CANCELED,
    OrderStatus.EXPIRED,
    OrderStatus.REJECTED,
    OrderStatus.REPLACED,
}


def _filled(order) -> float:
    return float(order.filled_qty or 0)


def _costs(order) -> float:
    """Filled quantity times average fill price."""
    return _filled(order) * float(order.filled_avg_price or 0)


def _db_status(order) -> str:
    """Map an Alpaca order status onto the orders table's status."""
    if order.status == OrderStatus.FILLED:
        return "FILLED"
    if order.status == OrderStatus.EXPIRED:
        return "EXPIRED"
    if order.status in (OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.REPLACED):
        return "CANCELLED"
    return "PARTIAL" if _filled(order) > 0 else "SUBMITTED"


class Trade:
    def __init__(self, key: str, secret: str, url: str, db: OrderDB):
        """url is the trading API base URL (config.BASE_URL), without /v2."""
        self.client = TradingClient(key, secret, url_override=url)
        self.stock_data = StockHistoricalDataClient(key, secret)
        self.crypto_data = CryptoHistoricalDataClient(key, secret)
        self.db = db

    def _record(self, order, description: Optional[str] = None) -> str:
        """Save the order's current Alpaca state to the database."""
        status = _db_status(order)
        self.db.save(order.client_order_id, status, description, _costs(order))
        return status

    def sync_orders(self) -> None:
        """Refresh every pending order in the database from Alpaca.

        Call once at the start of each run so the table reflects fills,
        cancellations and expiries that happened since the last run.
        """
        for row in self.db.by_status(*PENDING_STATUSES):
            client_order_id = row["client_order_id"]
            try:
                order = self.client.get_order_by_client_id(client_order_id)
            except APIError as e:
                if e.status_code == 404 and row["status"] == "NA":
                    self.db.save(client_order_id, "SKIPPED", "not found at Alpaca")
                    logger.info("%s never reached Alpaca; marked SKIPPED", client_order_id)
                else:
                    logger.warning("Could not sync %s: %s", client_order_id, e)
                continue
            status = self._record(order)
            if status != row["status"]:
                logger.info("%s: %s -> %s", client_order_id, row["status"], status)

    def get_balance(self) -> dict:
        """Return the account's cash, equity and buying power."""
        account = self.client.get_account()
        return {
            "currency": account.currency,
            "cash": float(account.cash),
            "equity": float(account.equity),
            "buying_power": float(account.buying_power),
            "portfolio_value": float(account.portfolio_value),
        }

    def place_order(
        self,
        symbol: str,
        side: str,
        qty: Optional[float] = None,
        notional: Optional[float] = None,
        limit_price: Optional[float] = None,
        time_in_force: str = "day",
        client_order_id: Optional[str] = None,
        description: Optional[str] = None,
    ):
        """Submit a market order, or a limit order if limit_price is given.

        Pass exactly one of qty (shares/units) or notional (dollar amount).
        side is "buy" or "sell"; time_in_force is e.g. "day", "gtc", "ioc".
        client_order_id defaults to a new "<PREFIX>-<uuid7>" tag.
        description is stored in the database alongside the order.
        """
        if (qty is None) == (notional is None):
            raise ValueError("Pass exactly one of qty or notional")
        client_order_id = client_order_id or config.new_client_order_id()

        params = dict(
            symbol=symbol,
            qty=qty,
            notional=notional,
            side=OrderSide(side.lower()),
            time_in_force=TimeInForce(time_in_force.lower()),
            client_order_id=client_order_id,
        )
        if limit_price is None:
            request = MarketOrderRequest(**params)
        else:
            request = LimitOrderRequest(**params, limit_price=limit_price)

        # Record first: if the submit times out after reaching Alpaca, the
        # next sync_orders() still finds the order under this tag.
        self.db.save(client_order_id, "NA", description)
        order = self.client.submit_order(order_data=request)
        self._record(order)
        logger.info("Placed %s %s %s (%s)", side, qty or notional, symbol, client_order_id)
        return order

    def close_order(self, client_order_id: str):
        """Unwind a single order, looked up by its client_order_id tag.

        Cancels any unfilled remainder, then closes the quantity that did fill
        with a market order. Returns the closing order, or None if nothing
        filled or the order was already closed by an earlier run.
        """
        row = self.db.get(client_order_id)
        if row is not None and row["status"] == "CLOSED":
            logger.info("%s already closed; skipping", client_order_id)
            return None

        order = self.client.get_order_by_client_id(client_order_id)
        if order.status not in _DONE_STATUSES:
            self.client.cancel_order_by_id(order.id)
            order = self.client.get_order_by_id(order.id)

        filled = _filled(order)
        if filled == 0:
            self._record(order, "close_order: nothing filled, order cancelled")
            return None
        closing = self.client.close_position(
            order.asset_id, close_options=ClosePositionRequest(qty=str(filled))
        )
        # The closing order has no row of its own; the original row records it.
        self.db.save(
            client_order_id,
            "CLOSED",
            f"close_order: closed {filled:g} {order.symbol} at market (closing order {closing.id})",
            _costs(order),
        )
        logger.info("Closed %s of %s (%s)", filled, order.symbol, client_order_id)
        return closing

    def close_orders(self) -> list:
        """Close every order in the database tagged with this PREFIX.

        Runs close_order() on each row that is still working or holds a fill;
        orders and positions from other prefixes or outside the database are
        left alone. Returns the closing orders that were sent.
        """
        closing_orders = []
        for row in self.db.by_status(
            "SUBMITTED", "PARTIAL", "FILLED", "CANCELLED", "EXPIRED"
        ):
            client_order_id = row["client_order_id"]
            if not client_order_id.startswith(f"{config.PREFIX}-"):
                continue
            # A cancelled or expired row only needs closing if it partly filled.
            if row["status"] in ("CANCELLED", "EXPIRED") and not (row["costs"] or 0) > 0:
                continue
            try:
                closing = self.close_order(client_order_id)
            except APIError as e:
                logger.error("Could not close %s: %s", client_order_id, e)
                continue
            if closing is not None:
                closing_orders.append(closing)
        return closing_orders

    def cancel_order(self, client_order_id: str) -> None:
        """Cancel a single open order by its client_order_id tag."""
        order = self.client.get_order_by_client_id(client_order_id)
        self.client.cancel_order_by_id(order.id)
        # Cancellation is asynchronous; the next sync_orders() records the result.
        self._record(self.client.get_order_by_id(order.id))

    def cancel_orders(self):
        """Cancel all open orders, leaving positions untouched."""
        result = self.client.cancel_orders()
        self.sync_orders()
        return result

    def get_asset_data(self, symbol: str) -> dict:
        """Return asset details plus the latest bid/ask quote."""
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
