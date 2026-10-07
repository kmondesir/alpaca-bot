"""Thin wrapper around the Alpaca trading and market data APIs.

Entries are stored as parents and protective exits as child rows (see db.py),
so later cron runs can reconcile their status.
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
from alpaca.trading.enums import (
    AssetClass,
    OrderSide,
    OrderStatus,
    TimeInForce,
)
from alpaca.trading.requests import (
    ClosePositionRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    ReplaceOrderRequest,
    StopLimitOrderRequest,
    TrailingStopOrderRequest,
)

import config
import risk
from db import PENDING_STATUSES, STATUSES, OrderDB
from symbols import crypto_pair, is_crypto_symbol, is_option_symbol, normalize_symbol

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
    """Map an Alpaca order status to the persisted status vocabulary."""
    if order.status == OrderStatus.FILLED:
        return "FILLED"
    if order.status == OrderStatus.EXPIRED:
        return "EXPIRED"
    if order.status in (OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.REPLACED):
        return "CANCELLED"
    return "PARTIAL" if _filled(order) > 0 else "SUBMITTED"


# Raise a crypto stop only after it can move by this fraction of the trail
# distance, so a rising price doesn't replace the order on every run.
_TRAIL_MIN_STEP = 0.1

# A stop marked with this note is being cancelled so its remainder can be sold
# at market; the note lets a later run finish the sale if the cancel lags.
_MARKET_FALLBACK_NOTE = "cancelling stop for market sell"
# How long to wait for Alpaca to confirm a cancel before deferring to next run.
_CANCEL_WAIT_SECONDS = 10.0


def _round_notional(notional: float) -> float:
    """Round a dollar amount down to whole cents, the most precision Alpaca accepts."""
    return float(Decimal(str(notional)).quantize(Decimal("0.01"), rounding=ROUND_DOWN))


def _round_to_increment(price: float, increment: float) -> float:
    tick = Decimal(str(increment))
    return float((Decimal(str(price)) / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick)


class Trade:
    def __init__(self, key: str, secret: str, url: str, db: OrderDB):
        """url is the trading API base URL (config.BASE_URL), without /v2."""
        self.client = TradingClient(key, secret, url_override=url)
        self.stock_data = StockHistoricalDataClient(key, secret)
        self.crypto_data = CryptoHistoricalDataClient(key, secret)
        self.option_data = OptionHistoricalDataClient(key, secret)
        self.db = db

    def _record_parent(self, order, description: Optional[str] = None) -> str:
        """Save the Alpaca state for an entry order and its parent position."""
        status = _db_status(order)
        if self.db.get_parent(order.client_order_id) is None:
            self.db.create_parent(
                order.client_order_id,
                order.symbol,
                description,
                order.side.value,
                float(order.qty) if order.qty is not None else None,
                order.order_type.value,
            )
        self.db.save_parent_entry(
            order.client_order_id,
            status,
            description,
            _costs(order),
            order.side.value,
            float(order.qty) if order.qty is not None else None,
            order.order_type.value,
        )
        return status

    def _record_child(
        self,
        order,
        description: Optional[str] = None,
        parent_id: Optional[str] = None,
        child_role: Optional[str] = None,
    ) -> str:
        """Save the Alpaca state for a protective child order."""
        status = _db_status(order)
        row = self.db.get_child(order.client_order_id)
        if row is None:
            if parent_id is None or child_role is None:
                raise KeyError(f"No child order found for {order.client_order_id!r}")
            self.db.create_child(
                order.client_order_id,
                parent_id,
                child_role,
                description,
                order.side.value,
                float(order.qty) if order.qty is not None else None,
                order.order_type.value,
            )
            row = self.db.get_child(order.client_order_id)
        if row is None:
            raise KeyError(f"Could not create child order {order.client_order_id!r}")
        self.db.save_child_order(
            order.client_order_id,
            status,
            description,
            _costs(order),
            order.side.value,
            float(order.qty) if order.qty is not None else None,
            order.order_type.value,
            row["basis"],
        )
        return status

    def sync_orders(self) -> None:
        """Refresh pending entry, protective child, and parent close orders."""
        for row in self.db.parents_by_entry_status(*PENDING_STATUSES):
            parent_id = row["parent_id"]
            try:
                order = self.client.get_order_by_client_id(parent_id)
            except APIError as error:
                if error.status_code == 404 and row["entry_status"] == "NA":
                    self.db.save_parent_entry(
                        parent_id,
                        "SKIPPED",
                        "not found at Alpaca",
                        row["costs"] or 0,
                        row["side"],
                        row["quantity"],
                        row["order_type"],
                    )
                    logger.info("%s never reached Alpaca; marked SKIPPED", parent_id)
                else:
                    logger.warning("Could not sync entry %s: %s", parent_id, error)
                continue
            status = self._record_parent(order)
            self._log_transition(parent_id, status, row["entry_status"])

        for row in self.db.children_by_status(*PENDING_STATUSES):
            child_id = row["child_id"]
            try:
                order = self.client.get_order_by_client_id(child_id)
            except APIError as error:
                if error.status_code == 404 and row["status"] == "NA":
                    self.db.save_child_order(
                        child_id,
                        "SKIPPED",
                        "not found at Alpaca",
                        row["costs"] or 0,
                        row["side"],
                        row["quantity"],
                        row["order_type"],
                        row["basis"],
                    )
                    logger.info("%s never reached Alpaca; marked SKIPPED", child_id)
                else:
                    logger.warning("Could not sync child %s: %s", child_id, error)
                continue
            status = self._record_child(order)
            self._finalize_child(child_id, status, row["status"])

        for row in self.db.parents_by_close_status(*PENDING_STATUSES):
            parent_id = row["parent_id"]
            close_id = row["close_order_id"]
            try:
                order = self.client.get_order_by_client_id(close_id)
            except APIError as error:
                if error.status_code == 404 and row["close_status"] == "NA":
                    status = "SKIPPED"
                    self.db.save_parent_close(
                        parent_id, close_id, status, row["close_costs"] or 0,
                        row["close_side"], row["close_quantity"], row["close_type"],
                    )
                else:
                    logger.warning("Could not sync close %s: %s", close_id, error)
                continue
            status = _db_status(order)
            self.db.save_parent_close(
                parent_id,
                close_id,
                status,
                _costs(order),
                order.side.value,
                float(order.qty) if order.qty is not None else None,
                order.order_type.value,
            )
            self._finalize_parent_close(parent_id, status, row["close_status"])

    def _log_transition(self, order_id: str, status: str, previous_status: Optional[str]) -> None:
        if previous_status is not None and status != previous_status:
            logger.info("%s: %s -> %s", order_id, previous_status, status)

    def _finalize_child(self, child_id: str, status: str, previous_status: Optional[str]) -> None:
        self._log_transition(child_id, status, previous_status)
        if status != "FILLED":
            return
        row = self.db.get_child(child_id)
        parent = self.db.get_parent(row["parent_id"])
        self.db.mark_parent_closed(parent["parent_id"])
        if row["basis"] is not None:
            pnl = row["costs"] - row["basis"] if row["side"] == OrderSide.SELL.value else row["basis"] - row["costs"]
            risk.record_trade(child_id, pnl, parent["symbol"])
            entry_side = OrderSide.BUY.value if row["side"] == OrderSide.SELL.value else OrderSide.SELL.value
            risk.mark_strategy_position_closed(parent["symbol"], entry_side)
        if row["linked_child_id"]:
            self._cancel_sibling(row["linked_child_id"])

    def _finalize_parent_close(
        self, parent_id: str, status: str, previous_status: Optional[str]
    ) -> None:
        self._log_transition(parent_id, status, previous_status)
        if status != "FILLED":
            return
        parent = self.db.get_parent(parent_id)
        basis = parent["basis"] if parent["basis"] is not None else parent["costs"]
        if basis is not None:
            pnl = parent["close_costs"] - basis if parent["side"] == OrderSide.BUY.value else basis - parent["close_costs"]
            risk.record_trade(parent_id, pnl, parent["symbol"])
            risk.mark_strategy_position_closed(parent["symbol"], parent["side"])

    def _cancel_sibling(self, sibling_id: str) -> None:
        """Cancel the other protective child after one leg fills."""
        sibling = self.db.get_child(sibling_id)
        if sibling is None or sibling["status"] not in PENDING_STATUSES:
            return
        try:
            self.cancel_order(sibling_id)
            logger.info("Cancelled %s: sibling protective order filled", sibling_id)
        except APIError as error:
            logger.warning("Could not cancel sibling %s: %s", sibling_id, error)

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

    def is_market_open(self, symbol: Optional[str] = None) -> bool:
        """Return whether the market is open, including holiday and early-close schedules."""
        if symbol is not None and is_crypto_symbol(symbol):
            return True
        return self.client.get_clock().is_open

    def place_order(
        self,
        symbol: str,
        side: str,
        qty: Optional[float] = None,
        notional: Optional[float] = None,
        limit_price: Optional[float] = None,
        stop_price: Optional[float] = None,
        trail_percent: Optional[float] = None,
        time_in_force: str = "day",
        client_order_id: Optional[str] = None,
        description: Optional[str] = None,
        parent_id: Optional[str] = None,
        child_role: Optional[str] = None,
    ):
        """Submit a market, limit, or trailing-stop order.

        Pass exactly one of qty (shares/units) or notional (dollar amount);
        notional is rounded down to whole cents.
        side is "buy" or "sell"; time_in_force is e.g. "day", "gtc", "ioc".
        limit_price and trail_percent are mutually exclusive; with neither, a
        market order is submitted. trail_percent is a percent value (e.g. 5
        for 5%), not a fraction.
        client_order_id defaults to a new "<PREFIX>-<uuid7>" tag.
        description is stored in the database alongside the order.
        """
        if (qty is None) == (notional is None):
            raise ValueError("Pass exactly one of qty or notional")
        if notional is not None:
            notional = _round_notional(notional)
            if notional <= 0:
                raise ValueError("notional must be at least $0.01")
        symbol = normalize_symbol(symbol)
        if trail_percent is not None and (limit_price is not None or stop_price is not None):
            raise ValueError("A trailing stop cannot include limit_price or stop_price")
        if stop_price is not None and limit_price is None:
            raise ValueError("stop_price requires limit_price for a stop-limit order")
        if is_crypto_symbol(symbol) and time_in_force.lower() not in ("gtc", "ioc"):
            raise ValueError("Crypto orders require time_in_force='gtc' or 'ioc'")
        if (parent_id is None) != (child_role is None):
            raise ValueError("parent_id and child_role must be provided together")
        order_side = OrderSide(side.lower())
        client_order_id = client_order_id or config.new_client_order_id()

        params = dict(
            symbol=symbol,
            qty=qty,
            notional=notional,
            side=order_side,
            time_in_force=TimeInForce(time_in_force.lower()),
            client_order_id=client_order_id,
        )
        if stop_price is not None:
            order_type = "stop_limit"
            request = StopLimitOrderRequest(
                **params,
                stop_price=stop_price,
                limit_price=limit_price,
            )
        elif trail_percent is None:
            if limit_price is None:
                order_type = "market"
                request = MarketOrderRequest(**params)
            else:
                order_type = "limit"
                request = LimitOrderRequest(**params, limit_price=limit_price)
        else:
            order_type = "trailing_stop"
            request = TrailingStopOrderRequest(**params, trail_percent=trail_percent)

        # Record first: if the submit times out after reaching Alpaca, the
        order = self.client.submit_order(order_data=request)
        if parent_id is None:
            self._record_parent(order, description)
        else:
            self._record_child(order, description, parent_id, child_role)
        logger.info("Placed %s %s %s (%s)", side, qty or notional, symbol, client_order_id)
        return order

    def open_position(
        self,
        symbol: str,
        notional: float,
        side: str = "buy",
        time_in_force: Optional[str] = None,
        description: Optional[str] = None,
        client_order_id: Optional[str] = None,
    ):
        """Submit a market entry using the notional approved by the risk module."""
        symbol = normalize_symbol(symbol)
        crypto = is_crypto_symbol(symbol)
        time_in_force = time_in_force or ("gtc" if crypto else "day")
        if notional <= 0:
            raise ValueError("notional must be positive")
        if crypto and side.lower() == OrderSide.SELL.value:
            raise RuntimeError("Crypto short selling is unsupported")
        if side.lower() == OrderSide.SELL.value:
            asset = self.get_asset_data(symbol)
            if not asset["shortable"]:
                raise RuntimeError(f"{symbol} is not shortable")
            reference_price = float(asset["bid"] or 0)
            if reference_price <= 0:
                raise RuntimeError(f"No valid bid price available to short {symbol}")
            scale = 1_000_000 if asset["fractionable"] else 1
            qty = math.floor(notional / reference_price * scale) / scale
            if qty <= 0:
                raise RuntimeError(f"Buying power is too small to short one share of {symbol}")
            return self.place_order(
                symbol,
                side,
                qty=qty,
                time_in_force=time_in_force,
                client_order_id=client_order_id,
                description=description,
            )
        return self.place_order(
            symbol,
            side,
            notional=notional,
            time_in_force=time_in_force,
            client_order_id=client_order_id,
            description=description,
        )

    def get_option_quote(self, symbol: str) -> dict:
        """Return the latest bid/ask for an OCC option contract."""
        symbol = normalize_symbol(symbol)
        quote = self.option_data.get_option_latest_quote(
            OptionLatestQuoteRequest(symbol_or_symbols=symbol)
        )[symbol]
        return {"bid": float(quote.bid_price or 0), "ask": float(quote.ask_price or 0)}

    def open_option_position(
        self,
        symbol: str,
        qty: int,
        limit_price: float,
        description: Optional[str] = None,
        client_order_id: Optional[str] = None,
    ):
        """Buy to open whole option contracts with a day limit order."""
        symbol = normalize_symbol(symbol)
        if not is_option_symbol(symbol):
            raise ValueError(f"{symbol} is not an option contract symbol")
        if qty < 1 or int(qty) != qty:
            raise ValueError("Option quantity must be a positive whole number of contracts")
        return self.place_order(
            symbol,
            OrderSide.BUY.value,
            qty=int(qty),
            limit_price=_round_to_increment(limit_price, 0.01),
            time_in_force="day",
            client_order_id=client_order_id,
            description=description,
        )

    def protect_position(
        self,
        symbol: str,
        qty: float,
        avg_price: float,
        description: Optional[str] = None,
        position_side: str = "buy",
        *,
        parent_id: str,
    ) -> tuple:
        """Submit enabled protective exits for a filled long or short position.

        A zero take-profit or trailing-stop setting disables that exit. When
        both are enabled, they are linked so sync_orders() cancels the sibling
        after one fills. The return value is always (take_profit, trailing_stop),
        with None for any disabled exit.
        """
        basis = qty * avg_price
        symbol = normalize_symbol(symbol)
        crypto = is_crypto_symbol(symbol)
        option = is_option_symbol(symbol)
        entry_side = OrderSide(position_side.lower())
        if entry_side not in (OrderSide.BUY, OrderSide.SELL):
            raise ValueError("position_side must be 'buy' or 'sell'")
        if (crypto or option) and entry_side != OrderSide.BUY:
            raise ValueError("Crypto and option protective orders only support long positions")
        if self.db.get_parent(parent_id) is None:
            raise KeyError(f"No parent position found for {parent_id!r}")
        exit_side = OrderSide.SELL if entry_side == OrderSide.BUY else OrderSide.BUY
        take_profit = None
        trailing_stop = None
        take_profit_id = None
        trailing_stop_id = None
        price_increment = 0.01
        if crypto:
            asset = self.client.get_asset(symbol)
            price_increment = float(asset.price_increment or 0.01)
            if price_increment <= 0:
                raise ValueError(f"Invalid price increment for {symbol}")

        if config.TAKE_PROFIT > 0:
            take_profit_id = config.new_client_order_id()
            multiplier = 1 + config.TAKE_PROFIT if entry_side == OrderSide.BUY else 1 - config.TAKE_PROFIT
            take_profit_price = _round_to_increment(avg_price * multiplier, price_increment)
            if take_profit_price <= 0:
                raise ValueError("TAKE_PROFIT must be less than 1 for a short position")
            take_profit = self.place_order(
                symbol,
                exit_side.value,
                qty=qty,
                limit_price=take_profit_price,
                time_in_force="gtc" if crypto else "day",
                client_order_id=take_profit_id,
                description=description or f"take-profit for {symbol}",
                parent_id=parent_id,
                child_role="take_profit",
            )
        # Alpaca has no trailing stops for options; risk.py trails STOP_LOSS for them each run.
        if config.STOP_LOSS > 0 and not option:
            trailing_stop_id = config.new_client_order_id()
            if crypto:
                if config.STOP_LOSS >= 1:
                    raise ValueError("STOP_LOSS must be less than 1 for crypto")
                stop_price = _round_to_increment(
                    avg_price * (1 - config.STOP_LOSS), price_increment
                )
                limit_price = _round_to_increment(stop_price - price_increment, price_increment)
                if stop_price <= 0 or limit_price <= 0:
                    raise ValueError("Crypto stop-limit prices must be positive")
                trailing_stop = self.place_order(
                    symbol,
                    exit_side.value,
                    qty=qty,
                    limit_price=limit_price,
                    stop_price=stop_price,
                    time_in_force="gtc",
                    client_order_id=trailing_stop_id,
                    description=description or f"trailing stop-limit for {symbol}",
                    parent_id=parent_id,
                    child_role="trailing_stop",
                )
            else:
                trailing_stop = self.place_order(
                    symbol,
                    exit_side.value,
                    qty=qty,
                    trail_percent=config.STOP_LOSS * 100,
                    client_order_id=trailing_stop_id,
                    description=description or f"trailing-stop for {symbol}",
                    parent_id=parent_id,
                    child_role="trailing_stop",
                )

        if take_profit_id and trailing_stop_id:
            self.db.link_children(take_profit_id, trailing_stop_id, basis)
        elif take_profit_id:
            self.db.save_child_order(
                take_profit_id,
                _db_status(take_profit),
                description,
                _costs(take_profit),
                take_profit.side.value,
                float(take_profit.qty) if take_profit.qty is not None else None,
                take_profit.order_type.value,
                basis,
            )
        elif trailing_stop_id:
            self.db.save_child_order(
                trailing_stop_id,
                _db_status(trailing_stop),
                description,
                _costs(trailing_stop),
                trailing_stop.side.value,
                float(trailing_stop.qty) if trailing_stop.qty is not None else None,
                trailing_stop.order_type.value,
                basis,
            )
        for order in (take_profit, trailing_stop):
            if order is not None and _db_status(order) == "FILLED":
                self._finalize_child(order.client_order_id, "FILLED", "NA")
        return take_profit, trailing_stop

    def manage_crypto_stops(self) -> None:
        """Trail working crypto stop-limits, and sell at market when one gaps.

        Alpaca has no trailing-stop order type for crypto, so each run moves the
        stop to STOP_LOSS below the current bid when that is higher than
        the working stop. Stops are never lowered; a failed replace leaves the
        existing stop in place.

        A stop-limit only sells at or above its limit price, so a fast drop can
        trigger it without filling. When the bid is at or below the stop and the
        order is still unfilled, the stop is cancelled and the remainder is sold
        at market. Run this before sync_orders() so a cancel that is still
        pending is resumed on the next run instead of being recorded as a
        plain cancellation.
        """
        if config.STOP_LOSS <= 0:
            return
        for row in self.db.children_by_status(*STATUSES):
            if row["role"] != "trailing_stop" or row["order_type"] != "stop_limit":
                continue
            # A noted row may already be marked CANCELLED if its cancel landed
            # after the last run's wait; it still needs its market sell.
            if row["status"] not in PENDING_STATUSES and row["description"] != _MARKET_FALLBACK_NOTE:
                continue
            try:
                self._manage_crypto_stop(row)
            except Exception:
                logger.exception("Could not manage stop %s", row["child_id"])

    def _manage_crypto_stop(self, row: dict) -> None:
        child_id = row["child_id"]
        symbol = normalize_symbol(self.db.get_parent(row["parent_id"])["symbol"])
        if not is_crypto_symbol(symbol):
            return
        order = self.client.get_order_by_client_id(child_id)
        if row["description"] == _MARKET_FALLBACK_NOTE:
            self._finish_market_fallback(row, symbol, order)
            return
        if order.status in _DONE_STATUSES or order.stop_price is None:
            return

        bid = self._crypto_bid(symbol)
        if bid <= 0:
            return
        current_stop = float(order.stop_price)
        if bid <= current_stop:
            self._start_market_fallback(row, symbol, order, bid)
            return
        # Alpaca rejects replacing accepted, pending_new, or pending_* orders.
        if order.status != OrderStatus.NEW:
            return

        price_increment = float(self.client.get_asset(symbol).price_increment or 0.01)
        new_stop = _round_to_increment(bid * (1 - config.STOP_LOSS), price_increment)
        min_step = max(bid * config.STOP_LOSS * _TRAIL_MIN_STEP, price_increment)
        if new_stop - current_stop < min_step:
            return

        new_id = config.new_client_order_id()
        limit_price = _round_to_increment(new_stop - price_increment, price_increment)
        replacement = self.client.replace_order_by_id(
            order.id,
            ReplaceOrderRequest(
                stop_price=new_stop, limit_price=limit_price, client_order_id=new_id
            ),
        )
        self._replace_child_row(row, new_id, replacement, f"trailing stop-limit for {symbol}")
        logger.info(
            "Raised %s stop from %s to %s (bid %s): %s -> %s",
            symbol, current_stop, new_stop, bid, child_id, new_id,
        )

    def _crypto_bid(self, symbol: str) -> float:
        quote = self.crypto_data.get_crypto_latest_quote(
            CryptoLatestQuoteRequest(symbol_or_symbols=symbol)
        )[symbol]
        return float(quote.bid_price or 0)

    def _start_market_fallback(self, row: dict, symbol: str, order, bid: float) -> None:
        """Cancel a stop the price has passed so its remainder can sell at market."""
        logger.warning(
            "%s bid %s is at or below stop %s and %s has not filled; cancelling for a market sell",
            symbol, bid, order.stop_price, row["child_id"],
        )
        # Note the intent first so a cancel that finishes after this run still
        # ends in a market sell rather than an unprotected position.
        self.db.save_child_order(
            row["child_id"], row["status"], _MARKET_FALLBACK_NOTE, row["costs"] or 0,
            row["side"], row["quantity"], row["order_type"], row["basis"],
        )
        row = self.db.get_child(row["child_id"])
        try:
            self.client.cancel_order_by_id(order.id)
        except APIError as error:
            logger.warning("Could not cancel %s: %s", row["child_id"], error)
        sibling = self.db.get_sibling(row["child_id"])
        if sibling is not None and sibling["status"] in PENDING_STATUSES:
            try:
                self.cancel_order(sibling["child_id"])
            except APIError as error:
                logger.warning("Could not cancel sibling %s: %s", sibling["child_id"], error)
        self._finish_market_fallback(row, symbol, self._wait_until_done(order.id))

    def _finish_market_fallback(self, row: dict, symbol: str, order) -> None:
        child_id = row["child_id"]
        if order.status == OrderStatus.FILLED:
            # The stop filled before the cancel landed; sync_orders() records it.
            self.db.save_child_order(
                child_id, row["status"], f"trailing stop-limit for {symbol}", row["costs"] or 0,
                row["side"], row["quantity"], row["order_type"], row["basis"],
            )
            return
        if order.status not in _DONE_STATUSES:
            logger.info("Cancel of %s still pending; market sell will follow next run", child_id)
            return

        order_qty = float(order.qty or 0)
        filled = _filled(order)
        quantity = min(order_qty - filled, risk.available_quantity(self, symbol))
        if quantity <= 0:
            self._record_child(order, f"stop cancelled; no {symbol} left to sell")
            logger.warning("Cancelled %s but no %s is available to sell", child_id, symbol)
            return
        if filled > 0:
            logger.warning(
                "%s partly filled %s before the market sell; that portion's P&L is not recorded",
                child_id, filled,
            )
        basis = None
        if row["basis"] is not None and order_qty > 0:
            basis = row["basis"] * quantity / order_qty
        new_id = config.new_client_order_id()
        market = self.place_order(
            symbol,
            OrderSide.SELL.value,
            qty=quantity,
            time_in_force="gtc",
            client_order_id=new_id,
            description=f"market sell after stop gap for {symbol}",
            parent_id=row["parent_id"],
            child_role="trailing_stop",
        )
        self._record_child(order, f"stop gapped; replaced by market sell {new_id}")
        self.db.save_child_order(
            new_id,
            _db_status(market),
            None,
            _costs(market),
            market.side.value,
            float(market.qty) if market.qty is not None else None,
            market.order_type.value,
            basis,
        )
        logger.warning("Sold %s %s at market after stop %s did not fill", quantity, symbol, child_id)
        if _db_status(market) == "FILLED":
            self._finalize_child(new_id, "FILLED", "NA")

    def _wait_until_done(self, order_id, timeout: float = None):
        """Poll an order until Alpaca reports a final status or the timeout passes."""
        deadline = time.monotonic() + (_CANCEL_WAIT_SECONDS if timeout is None else timeout)
        while True:
            order = self.client.get_order_by_id(order_id)
            if order.status in _DONE_STATUSES or time.monotonic() >= deadline:
                return order
            time.sleep(0.5)

    def _replace_child_row(self, row: dict, new_id: str, replacement, description: str) -> None:
        """Retire a replaced child row and record its replacement under the same parent."""
        self.db.save_child_order(
            row["child_id"],
            "CANCELLED",
            f"replaced by {new_id}",
            row["costs"] or 0,
            row["side"],
            row["quantity"],
            row["order_type"],
            row["basis"],
        )
        self._record_child(replacement, description, row["parent_id"], "trailing_stop")
        self.db.save_child_order(
            new_id,
            _db_status(replacement),
            description,
            _costs(replacement),
            replacement.side.value,
            float(replacement.qty) if replacement.qty is not None else None,
            replacement.order_type.value,
            row["basis"],
        )
        if row["linked_child_id"]:
            self.db.link_children(row["linked_child_id"], new_id, row["basis"])

    def close_order(self, client_order_id: str):
        """Close a parent position and retain the close order details on it."""
        row = self.db.get_parent(client_order_id)
        if row is None:
            raise KeyError(f"No parent order found for {client_order_id!r}")
        if row["status"] in ("CLOSED", "CLOSING"):
            logger.info("%s already closed or closing; skipping", client_order_id)
            return None
        if row["close_order_id"] and row["close_status"] in PENDING_STATUSES:
            logger.info("%s already has a pending close order; skipping", client_order_id)
            return None

        order = self.client.get_order_by_client_id(client_order_id)
        if order.status not in _DONE_STATUSES:
            self.client.cancel_order_by_id(order.id)
            order = self.client.get_order_by_id(order.id)

        filled = _filled(order)
        if filled == 0:
            self._record_parent(order, "close_order: nothing filled, order cancelled")
            return None
        if self._cancel_protective_children(client_order_id):
            logger.info("%s was already closed by its protective order", client_order_id)
            return None

        # A crypto order's asset_id is the trading pair, not the held position,
        # so close by the position's own asset_id.
        position = self._find_position(order.symbol)
        if position is None:
            raise RuntimeError(f"No open position found for {order.symbol}")
        # Crypto fees come out of the purchased coin, so hold less than filled.
        quantity = min(filled, abs(float(position.qty_available or 0)))
        if quantity <= 0:
            raise RuntimeError(f"No available {order.symbol} to close for {client_order_id}")
        closing = self.client.close_position(
            position.asset_id, close_options=ClosePositionRequest(qty=str(quantity))
        )
        closing_status = _db_status(closing)
        self.db.save_parent_close(
            client_order_id,
            closing.client_order_id,
            closing_status,
            _costs(closing),
            closing.side.value,
            float(closing.qty) if closing.qty is not None else None,
            closing.order_type.value,
        )
        self._finalize_parent_close(client_order_id, closing_status, None)
        logger.info("Closed %s of %s (%s)", quantity, order.symbol, client_order_id)
        return closing

    def _cancel_protective_children(self, parent_id: str) -> bool:
        """Cancel a parent's working exits, which reserve the held quantity.

        Returns True when one of them had already filled, meaning the position
        is closed and nothing is left to sell.
        """
        exited = False
        for child in self.db.children_by_parent(parent_id):
            if child["status"] not in PENDING_STATUSES:
                continue
            child_id = child["child_id"]
            order = self.client.get_order_by_client_id(child_id)
            if order.status not in _DONE_STATUSES:
                if order.status != OrderStatus.PENDING_CANCEL:
                    self.client.cancel_order_by_id(order.id)
                order = self._wait_until_done(order.id)
            if order.status not in _DONE_STATUSES:
                raise RuntimeError(f"Cancel of {child_id} is still pending; retry the close next run")
            status = self._record_child(order)
            if status == "FILLED":
                self._finalize_child(child_id, status, child["status"])
                exited = True
        return exited

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

    def close_orders(self) -> list:
        """Close every order in the database tagged with this PREFIX.

        Runs close_order() on each row that is still working or holds a fill;
        orders and positions from other prefixes or outside the database are
        left alone. Returns the closing orders that were sent.
        """
        closing_orders = []
        for row in self.db.parents_by_status(
            "NA", "SUBMITTED", "PARTIAL", "OPEN", "FILLED", "CANCELLED", "EXPIRED"
        ):
            client_order_id = row["parent_id"]
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
        order = self.client.get_order_by_id(order.id)
        if self.db.get_child(client_order_id) is not None:
            self._record_child(order)
        else:
            self._record_parent(order)

    def cancel_orders(self):
        """Cancel all open orders, leaving positions untouched."""
        result = self.client.cancel_orders()
        self.sync_orders()
        return result

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
