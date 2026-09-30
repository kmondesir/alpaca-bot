"""Tracks consecutive losing trades and trips the STATE kill switch after too many.

The loss file (config.LOSS_DIRECTORY/losses.json) is recreated automatically
if missing. It is never reset by this module: once STATE is switched off, a
human must review the losses, delete the file, and set STATE back to true.
"""

import json
import logging
from typing import Optional

from dotenv import set_key

import config
from db import utc_now

logger = logging.getLogger(__name__)

LOSS_FILE = config.LOSS_DIRECTORY / "losses.json"
ENV_FILE = config.ROOT_DIR / ".env"


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
