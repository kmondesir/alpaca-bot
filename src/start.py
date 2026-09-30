"""Entry point for the alpaca project. Run once per cron tick; does not loop."""

import logging

import config
import risk
from db import OrderDB
from strategy import MacdPsarStrategy
from trade import Trade

logger = logging.getLogger(__name__)


def main() -> None:
    if not config.STATE:
        logger.info("STATE is off; exiting without running")
        return
    if not (config.ALPACA_API_KEY and config.ALPACA_SECRET_KEY):
        logger.error("ALPACA_API_KEY and ALPACA_SECRET_KEY must be set in .env")
        return
    logger.info("Starting alpaca (demo=%s, url=%s)", config.DEMO, config.BASE_URL)

    db = OrderDB()
    trade = Trade(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY, config.BASE_URL, db)
    trade.sync_orders()
    signals = MacdPsarStrategy(trade.stock_data, config.STRATEGY_SYMBOLS).generate_signals()
    risk.process_strategy_signals(trade, signals)


if __name__ == "__main__":
    main()
