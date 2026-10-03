"""CLI and cron entry point for the alpaca project."""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Optional

from alpaca.common.exceptions import APIError
from alpaca.trading.enums import AssetClass, AssetStatus
from alpaca.trading.requests import GetAssetsRequest

import config
import risk
from db import OrderDB
from strategy import MacdPsarStrategy, Signal
from symbols import is_crypto_symbol, normalize_symbol
from trade import Trade

logger = logging.getLogger(__name__)


def _parse_assets(value: str) -> tuple[str, ...]:
    assets = tuple(dict.fromkeys(normalize_symbol(item) for item in value.split(",") if item.strip()))
    if not assets:
        raise argparse.ArgumentTypeError("expected at least one asset")
    return assets


def _resolve_assets(trade: Trade, assets: tuple[str, ...]) -> tuple[str, ...]:
    """Convert bare crypto names to USD pairs and drop assets Alpaca can't trade.

    Without the slash a name is treated as a stock ticker, and several crypto
    names are also listed stocks or ETFs (ETH, XRP). A bare name becomes
    NAME/USD when Alpaca trades that crypto pair. Any remaining stock ticker
    that Alpaca doesn't list as tradable is skipped with an error.
    """
    if all(is_crypto_symbol(asset) for asset in assets):
        return assets
    pairs = {
        normalize_symbol(asset.symbol)
        for asset in trade.client.get_all_assets(
            GetAssetsRequest(asset_class=AssetClass.CRYPTO, status=AssetStatus.ACTIVE)
        )
        if asset.tradable
    }
    resolved = []
    for asset in assets:
        pair = f"{asset}/USD"
        if not is_crypto_symbol(asset) and pair in pairs:
            logger.warning("Treating %s as crypto %s; pass %s in --assets to silence this", asset, pair, pair)
            asset = pair
        elif not is_crypto_symbol(asset) and not _tradable_stock(trade, asset):
            continue
        resolved.append(asset)
    return tuple(dict.fromkeys(resolved))


def _tradable_stock(trade: Trade, symbol: str) -> bool:
    try:
        asset = trade.client.get_asset(symbol)
    except APIError as error:
        if error.status_code != 404:
            raise
        logger.error("Skipping %s: Alpaca has no stock or crypto pair by that name", symbol)
        return False
    if not asset.tradable:
        logger.error("Skipping %s: Alpaca lists %s as not tradable", symbol, asset.name)
        return False
    return True


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect account state or run Alpaca trading actions.")
    parser.add_argument(
        "--assets",
        type=_parse_assets,
        help="required comma-separated assets for a scheduled strategy run, e.g. SPY,AAPL,BTC/USD",
    )
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("get_balance", help="show account balances")
    commands.add_parser("get_positions", help="show open positions")
    commands.add_parser("get_market_status", aliases=["market_status"], help="show market open/closed status")

    asset_parser = commands.add_parser("asset", aliases=["get_asset_data"], help="show asset details and latest quote")
    asset_parser.add_argument("symbol")

    position_parser = commands.add_parser("get_position", help="show one open position")
    position_parser.add_argument("symbol")

    open_parser = commands.add_parser("open_position", help="open a risk-sized market position")
    open_parser.add_argument("symbol")
    open_parser.add_argument("--side", choices=("buy", "sell"), default="buy")

    close_parser = commands.add_parser("close_position", help="close a tracked position at market")
    close_parser.add_argument("symbol")
    return parser


def _print_result(value) -> None:
    def serialize(item):
        if hasattr(item, "model_dump"):
            return item.model_dump(mode="json")
        return str(item)

    print(json.dumps(value, indent=2, default=serialize))


def _manual_signal(symbol: str, side: str) -> Signal:
    direction = "long" if side == "buy" else "short"
    return Signal(normalize_symbol(symbol), direction, datetime.now(timezone.utc))


def _get_open_position(trade: Trade, symbol: str):
    try:
        return trade.client.get_open_position(symbol)
    except APIError as error:
        if error.status_code == 404:
            return None
        raise


def _run_command(args, trade: Trade, db: OrderDB) -> None:
    if args.command == "get_balance":
        _print_result(trade.get_balance())
    elif args.command in ("asset", "get_asset_data"):
        _print_result(trade.get_asset_data(normalize_symbol(args.symbol)))
    elif args.command == "get_positions":
        _print_result(trade.client.get_all_positions())
    elif args.command == "get_position":
        symbol = normalize_symbol(args.symbol)
        position = _get_open_position(trade, symbol)
        _print_result(position if position is not None else {"symbol": symbol, "status": "not_open"})
    elif args.command in ("get_market_status", "market_status"):
        print("open" if trade.is_market_open() else "closed")
    elif args.command == "open_position":
        trade.sync_orders()
        orders = risk.process_strategy_signals(trade, [_manual_signal(args.symbol, args.side)])
        _print_result(orders)
    elif args.command == "close_position":
        symbol = normalize_symbol(args.symbol)
        position = _get_open_position(trade, symbol)
        if position is None:
            _print_result({"symbol": symbol, "status": "not_open"})
            return
        position_side = getattr(position.side, "value", str(position.side)).lower()
        close_side = "sell" if position_side.endswith("long") else "buy"
        tracked_parents = [
            row
            for row in db.parents_by_status("OPEN", "CLOSING", "FILLED")
            if row["symbol"].upper() == symbol
        ]
        if not tracked_parents:
            _print_result({"symbol": symbol, "status": "untracked_position_not_closed"})
            return
        trade.sync_orders()
        risk.process_strategy_signals(trade, [_manual_signal(symbol, close_side)])
        refreshed = [db.get_parent(row["parent_id"]) for row in tracked_parents]
        _print_result(
            [
                {
                    "parent_id": row["parent_id"],
                    "status": row["status"],
                    "close_order_id": row["close_order_id"],
                    "close_status": row["close_status"],
                }
                for row in refreshed
                if row is not None
            ]
        )


def main(argv: Optional[list[str]] = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command is None and not args.assets:
        parser.error("--assets is required when running the scheduled strategy")
    if args.command is None and not config.STATE:
        logger.info("STATE is off; exiting without running")
        return
    required = {
        "ALPACA_API_KEY": config.ALPACA_API_KEY,
        "ALPACA_SECRET_KEY": config.ALPACA_SECRET_KEY,
        "ALPACA_BASE_URL": config.BASE_URL,
    }
    missing = [name for name, value in required.items() if not value.strip()]
    if missing:
        logger.error("Missing required .env variables: %s; exiting", ", ".join(missing))
        sys.exit(1)
    logger.info("Starting alpaca (demo=%s, url=%s)", config.DEMO, config.BASE_URL)

    db = OrderDB()
    trade = Trade(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY, config.BASE_URL, db)
    if args.command is None:
        trade.manage_crypto_stops()
        trade.sync_orders()
        try:
            assets = _resolve_assets(trade, args.assets)
        except APIError as error:
            logger.error("Could not check --assets against Alpaca; skipping strategy: %s", error)
            return
        if not assets:
            logger.error("No tradable assets left in --assets; skipping strategy")
            return
        signals = MacdPsarStrategy(trade.stock_data, assets).generate_signals()
        if not signals:
            logger.info("No strategy signal for %s this run", ", ".join(assets))
        risk.process_strategy_signals(trade, signals)
    else:
        _run_command(args, trade, db)


if __name__ == "__main__":
    main()
