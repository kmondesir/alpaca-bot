"""Command-line entry point: execute and look up Alpaca orders.

Prints the result as JSON on stdout; orders are always a list of dictionaries.
Logs go to the log file and stderr.

    python src/execute.py --balance
    python src/execute.py --orders
    python src/execute.py --orders ID1,ID2
    python src/execute.py --asset SPY --amount 500 --position long [--client_order_id TAG]
    python src/execute.py --close ID1,ID2
"""

import argparse
import json
import logging
import sys
from typing import Optional

from alpaca.common.exceptions import APIError

import config
from trade import POSITIONS, Trade

logger = logging.getLogger(__name__)

# --orders with no ids lists this many of the most recent orders.
RECENT_ORDERS = 100
# Alpaca allows at most 128 characters in a client_order_id.
MAX_CLIENT_ORDER_ID = 128


def _parse_ids(value: str) -> tuple[str, ...]:
    ids = tuple(dict.fromkeys(item.strip() for item in value.strip("{}").split(",") if item.strip()))
    if not ids:
        raise argparse.ArgumentTypeError("expected at least one client_order_id")
    return ids


def _parse_amount(value: str) -> float:
    try:
        amount = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a USD amount: {value!r}") from None
    if amount <= 0:
        raise argparse.ArgumentTypeError("amount must be positive")
    return amount


def _parse_client_order_id(value: str) -> str:
    value = value.strip()
    if not value or len(value) > MAX_CLIENT_ORDER_ID:
        raise argparse.ArgumentTypeError(f"client_order_id must be 1 to {MAX_CLIENT_ORDER_ID} characters")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Execute and look up Alpaca orders; results print as JSON.")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--balance", action="store_true", help="show the account balance")
    action.add_argument(
        "--orders",
        nargs="?",
        const=(),
        type=_parse_ids,
        metavar="ID1,ID2",
        help=f"show the last {RECENT_ORDERS} orders, or only the orders with the given client_order_ids",
    )
    action.add_argument(
        "--close",
        type=_parse_ids,
        metavar="ID1,ID2",
        help="close orders by client_order_id: cancel if working, then sell/buy back any fill",
    )
    action.add_argument("--asset", help="symbol to open a position in: stock, crypto pair (BTC/USD), or OCC option")

    parser.add_argument("--amount", type=_parse_amount, help="position size in USD (with --asset)")
    parser.add_argument(
        "--position",
        type=str.lower,
        choices=POSITIONS,
        help="long or short for stocks and crypto, call or put for an option contract (with --asset)",
    )
    parser.add_argument(
        "--client_order_id",
        type=_parse_client_order_id,
        help="client_order_id for the order placed (default: a random UUID); with --close, only one id is allowed",
    )
    parser.add_argument("--logdirectory", help="log folder, relative to the project root (default: LOG_DIRECTORY)")
    parser.add_argument("--logname", help="log file name (default: LOG_NAME)")
    return parser


def _validate(parser: argparse.ArgumentParser, args) -> None:
    if args.asset is not None:
        missing = [flag for flag, value in (("--amount", args.amount), ("--position", args.position)) if value is None]
        if missing:
            parser.error(f"--asset requires {' and '.join(missing)}")
    else:
        for flag, value in (("--amount", args.amount), ("--position", args.position)):
            if value is not None:
                parser.error(f"{flag} is only used with --asset")
    if args.client_order_id is not None:
        if args.asset is None and args.close is None:
            parser.error("--client_order_id is only used with --asset or --close")
        if args.close is not None and len(args.close) > 1:
            parser.error("--client_order_id can only tag the close of a single order")


def _serialize(item):
    if hasattr(item, "model_dump"):
        return item.model_dump(mode="json")
    return str(item)


def _print_result(value) -> None:
    print(json.dumps(value, indent=2, default=_serialize))


def _error(order_id: str, error: Exception) -> dict:
    if isinstance(error, APIError):
        message = f"{error.status_code}: {error}"
    else:
        message = str(error)
    return {"id": order_id, "error": message}


def _for_each_id(ids: tuple[str, ...], action) -> tuple[list, bool]:
    """Run action on each id; a failure becomes an {"id", "error"} entry so the rest still run."""
    results, failed = [], False
    for order_id in ids:
        try:
            results.append(action(order_id))
        except (APIError, RuntimeError, ValueError) as error:
            logger.error("%s failed: %s", order_id, error)
            results.append(_error(order_id, error))
            failed = True
    return results, failed


def _run(args, trade: Trade) -> bool:
    """Run the requested action and print its result; returns False if anything failed."""
    if args.balance:
        _print_result(trade.get_balance())
        return True
    if args.orders is not None:
        if not args.orders:
            _print_result(trade.get_orders(RECENT_ORDERS))
            return True
        orders, failed = _for_each_id(args.orders, trade.get_order)
        _print_result(orders)
        return not failed

    # Placing and closing orders honor the STATE kill switch; lookups don't.
    if not config.STATE:
        logger.error("STATE is off in .env; not placing or closing orders")
        _print_result([{"error": "STATE is off; no orders placed or closed"}])
        return False
    if args.close is not None:
        orders, failed = _for_each_id(
            args.close, lambda order_id: trade.close_order(order_id, args.client_order_id)
        )
        _print_result(orders)
        return not failed
    client_order_id = args.client_order_id or config.new_client_order_id()
    try:
        order = trade.open_position(args.asset, args.amount, args.position, client_order_id)
    except (APIError, RuntimeError, ValueError) as error:
        logger.error("Could not open %s %s: %s", args.position, args.asset, error)
        _print_result([_error(client_order_id, error)])
        return False
    _print_result([order])
    return True


def main(argv: Optional[list[str]] = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _validate(parser, args)
    config.setup_logging(args.logdirectory, args.logname)

    required = {
        "ALPACA_API_KEY": config.ALPACA_API_KEY,
        "ALPACA_SECRET_KEY": config.ALPACA_SECRET_KEY,
        "ALPACA_BASE_URL": config.BASE_URL,
    }
    missing = [name for name, value in required.items() if not value.strip()]
    if missing:
        logger.error("Missing required .env variables: %s; exiting", ", ".join(missing))
        sys.exit(1)
    logger.info("Starting execute (demo=%s, url=%s)", config.DEMO, config.BASE_URL)

    trade = Trade(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY, config.BASE_URL)
    if not _run(args, trade):
        sys.exit(1)


if __name__ == "__main__":
    main()
