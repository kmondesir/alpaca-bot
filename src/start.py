"""Entry point for the alpaca project. Run once per cron tick; does not loop."""

import logging

import config
from db import OrderDB
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
    # Pick up fills, cancellations and expiries since the previous run.
    trade.sync_orders()


if __name__ == "__main__":
    main()
