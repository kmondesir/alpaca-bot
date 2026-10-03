"""Tracks consecutive losing trades and trips the STATE kill switch after too many.

The loss file (config.LOSS_DIRECTORY/losses.json) is recreated automatically
if missing. It is never reset by this module: once STATE is switched off, a
human must review the losses, delete the file, and set STATE back to true.
"""

import json
import logging
import sqlite3
from contextlib import closing
from typing import Optional

from alpaca.common.exceptions import APIError
from alpaca.trading.enums import AssetClass, OrderStatus, QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest
from dotenv import set_key

import config
from symbols import crypto_pair, is_crypto_symbol, normalize_symbol
from db import utc_now

logger = logging.getLogger(__name__)

LOSS_FILE = config.LOSS_DIRECTORY / "losses.json"
ENV_FILE = config.ROOT_DIR / ".env"

_STRATEGY_SIGNALS_SCHEMA = """
CREATE TABLE IF NOT EXISTS strategy_signals (
    symbol TEXT NOT NULL,
    signal_timestamp TEXT NOT NULL,
    client_order_id TEXT NOT NULL UNIQUE,
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    protected_qty REAL NOT NULL DEFAULT 0,
    protection_complete INTEGER NOT NULL DEFAULT 0,
    position_closed INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (symbol, signal_timestamp)
)
"""


def _strategy_connection() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(config.DB_PATH, timeout=30)
    try:
        with connection:
            connection.execute(_STRATEGY_SIGNALS_SCHEMA)
            try:
                connection.execute(
                    "ALTER TABLE strategy_signals "
                    "ADD COLUMN position_closed INTEGER NOT NULL DEFAULT 0"
                )
            except sqlite3.OperationalError as error:
                if "duplicate column" not in str(error).lower():
                    raise
    except Exception:
        connection.close()
        raise
    return connection


def claim_strategy_signal(
    symbol: str, signal_timestamp: str, client_order_id: str, side: str
) -> bool:
    """Atomically claim a crossover so only one run can submit its entry order."""
    if side not in ("buy", "sell"):
        raise ValueError("side must be 'buy' or 'sell'")
    with closing(_strategy_connection()) as connection:
        with connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO strategy_signals
                    (symbol, signal_timestamp, client_order_id, side)
                VALUES (?, ?, ?, ?)
                """,
                (symbol, signal_timestamp, client_order_id, side),
            )
            return cursor.rowcount == 1


def pending_strategy_entries() -> list[dict]:
    """Return claimed entries whose filled quantity still needs protection."""
    with closing(_strategy_connection()) as connection:
        with connection:
            rows = connection.execute(
                """
                SELECT client_order_id, symbol, side, protected_qty
                FROM strategy_signals
                WHERE protection_complete = 0
                """
            ).fetchall()
    return [
        {
            "client_order_id": row[0],
            "symbol": row[1],
            "side": row[2],
            "protected_qty": row[3],
        }
        for row in rows
    ]


def strategy_entries_for_symbol(symbol: str) -> list[dict]:
    """Return strategy entries associated with a symbol, newest first."""
    with closing(_strategy_connection()) as connection:
        rows = connection.execute(
            """
            SELECT client_order_id, side, position_closed
            FROM strategy_signals
            WHERE symbol = ?
            ORDER BY signal_timestamp DESC
            """,
            (symbol,),
        ).fetchall()
    return [
        {"client_order_id": row[0], "side": row[1], "position_closed": bool(row[2])}
        for row in rows
    ]


def update_strategy_protection(
    client_order_id: str, protected_qty: float, complete: bool = False
) -> None:
    """Record protected fills, retaining the signal claim to prevent re-entry."""
    with closing(_strategy_connection()) as connection:
        with connection:
            connection.execute(
                """
                UPDATE strategy_signals
                SET protected_qty = ?, protection_complete = ?
                WHERE client_order_id = ?
                """,
                (protected_qty, int(complete), client_order_id),
            )


def release_strategy_signal(client_order_id: str) -> None:
    """Release a claim only when no entry order was recorded locally."""
    with closing(_strategy_connection()) as connection:
        with connection:
            connection.execute(
                "DELETE FROM strategy_signals WHERE client_order_id = ?",
                (client_order_id,),
            )


def ignore_strategy_signal(client_order_id: str) -> None:
    """Keep a signal deduplicated without recording it as an opened position."""
    with closing(_strategy_connection()) as connection:
        with connection:
            connection.execute(
                """
                UPDATE strategy_signals
                SET protection_complete = 1, position_closed = 1
                WHERE client_order_id = ?
                """,
                (client_order_id,),
            )


def mark_strategy_position_closed(symbol: str, entry_side: str) -> None:
    """Mark the newest active strategy entry closed after an exit fills."""
    with closing(_strategy_connection()) as connection:
        with connection:
            connection.execute(
                """
                UPDATE strategy_signals
                SET position_closed = 1
                WHERE client_order_id = (
                    SELECT client_order_id
                    FROM strategy_signals
                    WHERE symbol = ? AND side = ? AND position_closed = 0
                    ORDER BY signal_timestamp DESC
                    LIMIT 1
                )
                """,
                (symbol, entry_side),
            )


_TERMINAL_STATUSES = {
    OrderStatus.FILLED,
    OrderStatus.CANCELED,
    OrderStatus.EXPIRED,
    OrderStatus.REJECTED,
    OrderStatus.REPLACED,
}


def _position_symbol(position) -> str:
    """Alpaca reports crypto positions without the slash used everywhere else."""
    if position.asset_class == AssetClass.CRYPTO:
        return crypto_pair(position.symbol)
    return normalize_symbol(position.symbol)


def available_quantity(trade, symbol: str) -> float:
    """Return the held quantity not already reserved by open orders.

    Alpaca deducts crypto fees from the purchased asset, so the held quantity
    can be smaller than the entry order's filled_qty.
    """
    target = normalize_symbol(symbol)
    for position in trade.client.get_all_positions():
        if _position_symbol(position) == target:
            return float(position.qty_available or 0)
    return 0.0


def _protect_strategy_entry(trade, entry: dict) -> None:
    client_order_id = entry["client_order_id"]
    row = trade.db.get(client_order_id)
    if row is None or row["status"] == "SKIPPED":
        release_strategy_signal(client_order_id)
        return
    try:
        order = trade.client.get_order_by_client_id(client_order_id)
    except APIError as error:
        logger.warning("Could not check strategy entry %s: %s", client_order_id, error)
        return

    symbol = entry["symbol"]
    filled_quantity = float(order.filled_qty or 0)
    protected_quantity = float(entry["protected_qty"])
    additional_quantity = filled_quantity - protected_quantity
    average_price = float(order.filled_avg_price or 0)
    if additional_quantity > 0 and average_price > 0:
        if is_crypto_symbol(symbol):
            available_quantity = available_quantity(trade, symbol)
            if available_quantity < additional_quantity:
                logger.info(
                    "Protecting %s of %s filled %s; the rest went to fees or is already reserved",
                    available_quantity,
                    additional_quantity,
                    symbol,
                )
                additional_quantity = available_quantity
        if additional_quantity > 0:
            trade.protect_position(
                symbol,
                additional_quantity,
                average_price,
                description=f"strategy protection for {symbol}",
                position_side=entry["side"],
                parent_id=client_order_id,
            )
        else:
            logger.warning("No available %s to protect for %s", symbol, client_order_id)
        update_strategy_protection(client_order_id, filled_quantity)
        protected_quantity = filled_quantity

    if order.status in _TERMINAL_STATUSES and filled_quantity <= protected_quantity:
        update_strategy_protection(client_order_id, protected_quantity, complete=True)


def _protect_pending_strategy_entries(trade) -> None:
    for entry in pending_strategy_entries():
        try:
            _protect_strategy_entry(trade, entry)
        except Exception:
            logger.exception(
                "Could not protect strategy entry %s for %s",
                entry["client_order_id"],
                entry["symbol"],
            )


def _loss_limit_reached() -> bool:
    limit = config.MAX_CONSECUTIVE_LOSSES
    if limit <= 0:
        return False
    try:
        data = json.loads(LOSS_FILE.read_text(encoding="utf-8")) if LOSS_FILE.exists() else {}
        consecutive_losses = data.get("consecutive_losses", 0)
        if not isinstance(consecutive_losses, int) or consecutive_losses < 0:
            raise ValueError("consecutive_losses must be a non-negative integer")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        logger.error("Could not read consecutive-loss state; blocking new entries: %s", error)
        return True

    if consecutive_losses < limit:
        return False
    if config.STATE:
        set_key(str(ENV_FILE), "STATE", "false")
    config.STATE = False
    logger.warning(
        "Consecutive-loss limit reached (%d/%d); blocking new entries",
        consecutive_losses,
        limit,
    )
    return True


def _occupied_symbols(trade) -> tuple[dict, set[str]]:
    positions = {
        _position_symbol(position): position
        for position in trade.client.get_all_positions()
    }
    pending_orders = trade.client.get_orders(
        filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500)
    )
    occupied_symbols = set(positions)
    occupied_symbols.update(normalize_symbol(order.symbol) for order in pending_orders)
    return positions, occupied_symbols


def process_strategy_signals(trade, signals: list) -> list:
    """Apply risk gates and execute signals, closing tracked positions on reversals."""
    try:
        _protect_pending_strategy_entries(trade)
    except Exception:
        logger.exception("Could not reconcile strategy protective orders")

    if not signals:
        return []
    if not config.STATE:
        logger.info("STATE is off; risk manager blocked strategy trades")
        return []
    if _loss_limit_reached():
        return []
    positions, occupied_symbols = _occupied_symbols(trade)
    submitted_orders = []
    active_statuses = {"NA", "SUBMITTED", "PARTIAL", "FILLED", "OPEN", "CLOSING"}

    for signal in signals:
        if not config.STATE:
            logger.info("STATE switched off during risk processing; stopping signal handling")
            break
        if _loss_limit_reached():
            break
        symbol = normalize_symbol(signal.symbol)
        if not trade.is_market_open(symbol):
            logger.info("Market closed for %s; risk manager skipped signal", symbol)
            continue
        client_order_id = config.new_client_order_id()
        if not claim_strategy_signal(
            symbol, signal.bar_timestamp.isoformat(), client_order_id, signal.side
        ):
            logger.info("Duplicate strategy signal ignored for %s", symbol)
            continue

        close_attempted = False
        try:
            position = positions.get(symbol)
            if position is not None:
                position_side = "buy" if str(position.side).lower().endswith("long") else "sell"
                tracked_entries = [
                    entry
                    for entry in strategy_entries_for_symbol(symbol)
                    if not entry["position_closed"] and entry["side"] == position_side
                ]
                if position_side == signal.side:
                    logger.info("Existing %s position blocks another %s entry for %s", position_side, signal.direction, symbol)
                    ignore_strategy_signal(client_order_id)
                    continue
                if not tracked_entries:
                    logger.warning("Opposite signal for unmanaged %s position; leaving it untouched", symbol)
                    ignore_strategy_signal(client_order_id)
                    continue
                for entry in tracked_entries:
                    row = trade.db.get(entry["client_order_id"])
                    if row is not None and row["status"] in active_statuses:
                        close_attempted = True
                        trade.close_order(entry["client_order_id"])
                ignore_strategy_signal(client_order_id)
                logger.info("Closed tracked %s position in %s; waiting for a new signal before reversing", position_side, symbol)
                continue

            if is_crypto_symbol(symbol) and signal.side == "sell":
                logger.info("Ignoring short signal for crypto %s; crypto short selling is unsupported", symbol)
                ignore_strategy_signal(client_order_id)
                continue

            pending_entries = [
                entry
                for entry in strategy_entries_for_symbol(symbol)
                if not entry["position_closed"]
                and (row := trade.db.get(entry["client_order_id"])) is not None
                and row["status"] in active_statuses
            ]
            if pending_entries:
                logger.info("Existing strategy order blocks another entry for %s", symbol)
                ignore_strategy_signal(client_order_id)
                continue

            if symbol not in occupied_symbols and len(occupied_symbols) >= config.MAX_OPEN_POSITIONS:
                logger.warning(
                    "Maximum open positions reached (%d/%d); blocking %s",
                    len(occupied_symbols),
                    config.MAX_OPEN_POSITIONS,
                    symbol,
                )
                ignore_strategy_signal(client_order_id)
                continue

            balance = trade.get_balance()
            buying_power_key = (
                "non_marginable_buying_power" if is_crypto_symbol(symbol) else "buying_power"
            )
            notional = balance.get(buying_power_key, 0.0) * config.WAGER
            if notional <= 0:
                logger.warning("Calculated wager is not positive; blocking %s", symbol)
                ignore_strategy_signal(client_order_id)
                continue

            order = trade.open_position(
                symbol,
                side=signal.side,
                notional=notional,
                description=(
                    f"MACD {signal.direction} crossover confirmed by 1m and 5m PSAR "
                    f"at {signal.bar_timestamp.isoformat()}"
                ),
                client_order_id=client_order_id,
            )
            submitted_orders.append(order)
            occupied_symbols.add(symbol)
            logger.info("Risk manager submitted %s entry for %s", signal.direction, symbol)
            _protect_pending_strategy_entries(trade)
        except Exception:
            if not close_attempted and trade.db.get(client_order_id) is None:
                try:
                    release_strategy_signal(client_order_id)
                except Exception:
                    logger.exception("Could not release failed strategy signal for %s", symbol)
            logger.exception("Risk processing failed for strategy signal on %s", symbol)

    return submitted_orders


def _load() -> dict:
    """Return the loss file's contents, creating it first if it doesn't exist."""
    if not LOSS_FILE.exists():
        config.LOSS_DIRECTORY.mkdir(parents=True, exist_ok=True)
        data = {"consecutive_losses": 0, "history": []}
        LOSS_FILE.write_text(json.dumps(data, indent=2))
        return data
    return json.loads(LOSS_FILE.read_text())


def record_trade(client_order_id: str, pnl: float, symbol: Optional[str] = None) -> None:
    """Log a closed trade's profit/loss, updating the consecutive-loss streak.

    pnl is the exit proceeds minus the entry cost basis. A pnl below zero
    extends the streak; a pnl at or above zero resets it. Reaching
    config.MAX_CONSECUTIVE_LOSSES switches STATE off in .env.
    """
    data = _load()
    is_loss = pnl < 0
    data["consecutive_losses"] = data.get("consecutive_losses", 0) + 1 if is_loss else 0
    data.setdefault("history", []).append(
        {
            "client_order_id": client_order_id,
            "symbol": symbol,
            "pnl": pnl,
            "time": utc_now(),
        }
    )
    LOSS_FILE.write_text(json.dumps(data, indent=2))

    streak = data["consecutive_losses"]
    logger.info("%s: pnl=%.2f, consecutive_losses=%d", client_order_id, pnl, streak)
    if config.MAX_CONSECUTIVE_LOSSES > 0 and streak >= config.MAX_CONSECUTIVE_LOSSES:
        logger.error("%d consecutive losses; switching STATE off in .env", streak)
        set_key(str(ENV_FILE), "STATE", "false")
        config.STATE = False
