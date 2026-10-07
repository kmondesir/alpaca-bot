"""Load static config from config.json and secrets from .env, and set up logging.

Importing this module configures the root logger, so every other module only
needs `logger = logging.getLogger(__name__)`.
"""

import json
import logging
import os
import secrets
import socket
import sys
import time
import uuid
from datetime import time as clock_time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

SRC_DIR = Path(__file__).resolve().parent
ROOT_DIR = SRC_DIR.parent

load_dotenv(ROOT_DIR / ".env")

with open(SRC_DIR / "config.json", encoding="utf-8") as f:
    CONFIG = json.load(f)

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
# Only required for CoinMarketCapClient; Binance, Coinbase, and Yahoo Finance need no key.
COINMARKETCAP_API_KEY = os.getenv("COINMARKETCAP_API_KEY", "")
# When set, the 0DTE strategy reads option chains (open interest, gamma, bid/ask) from
# MarketData.app; otherwise from Yahoo Finance.
MARKETDATA_API_KEY = os.getenv("MARKETDATA_API_KEY", "")
DEMO = os.getenv("DEMO", "true").lower() == "true"
# Trading API base URL, e.g. https://paper-api.alpaca.markets or https://api.alpaca.markets.
BASE_URL = os.getenv("ALPACA_BASE_URL", "")
# Kill switch: the script only runs when STATE=true.
STATE = os.getenv("STATE", "false").lower() == "true"

# client_order_id is "<PREFIX>-<uuid7>"; Alpaca allows at most 128 characters.
PREFIX = os.getenv("PREFIX", "alpaca")
if len(PREFIX) > 128 - 37:
    raise ValueError("PREFIX must be at most 91 characters")

# Relative DB_PATH paths resolve from the project root.
DB_PATH = ROOT_DIR / os.getenv("DB_PATH", "data/alpaca.db")

# Risk management
# WAGER: fraction of buying power risked per new position (scales with balance).
WAGER = float(os.getenv("WAGER", "0.04"))
# TAKE_PROFIT: fraction above the fill price at which the take-profit sell exits.
TAKE_PROFIT = float(os.getenv("TAKE_PROFIT", "0.5"))
# STOP_LOSS: trailing-stop distance below the running high for stocks, crypto and options.
STOP_LOSS = float(os.getenv("STOP_LOSS", "0.05"))
# MAX_CONSECUTIVE_LOSSES: after this many losing trades in a row, STATE is switched off.
MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSS", "5"))
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "3"))
# OPTION_FLATTEN_TIME: US/Eastern HH:MM after which same-day-expiry options are closed.
OPTION_FLATTEN_TIME = os.getenv("OPTION_FLATTEN_TIME", "15:45")
# GEX_FILTER: 0DTE entries only when the underlying's dealer net gamma exposure is negative.
GEX_FILTER = os.getenv("GEX_FILTER", "true").lower() == "true"
# GEX_THRESHOLD: net GEX (dollars per 1% move) must be below -GEX_THRESHOLD; 0 accepts any negative value.
GEX_THRESHOLD = float(os.getenv("GEX_THRESHOLD", "0"))


def _window_time(name: str) -> Optional[clock_time]:
    """Parse a US/Eastern HH:MM setting; 0 or blank disables it."""
    value = os.getenv(name, "0").strip()
    if value in ("", "0"):
        return None
    try:
        hour, minute = (int(part) for part in value.split(":"))
        return clock_time(hour, minute)
    except ValueError:
        raise ValueError(f"{name} must be HH:MM (US/Eastern) or 0 to disable, not {value!r}") from None


# Trading window for new entries, in US/Eastern. Either bound may be 0 to
# disable it; a TRADING_START later than TRADING_STOP wraps past midnight.
TRADING_START = _window_time("TRADING_START")
TRADING_STOP = _window_time("TRADING_STOP")

# Relative LOSS_DIRECTORY paths resolve from the project root.
LOSS_DIRECTORY = ROOT_DIR / os.getenv("LOSS_DIRECTORY", "status")

# Relative LOG_DIRECTORY paths resolve from the project root.
LOG_DIRECTORY = ROOT_DIR / os.getenv("LOG_DIRECTORY", "logs")
LOG_NAME = os.getenv("LOG_NAME", "alpaca.log")
MAX_SIZE_IN_MB = float(os.getenv("MAX_SIZE_IN_MB", "10"))
MAX_BACKUP = int(os.getenv("MAX_BACKUP", "5"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

COMPUTER_NAME = socket.gethostname()


def uuid7() -> uuid.UUID:
    """Time-ordered UUID (RFC 9562); stdlib only has uuid.uuid7 from Python 3.14."""
    if hasattr(uuid, "uuid7"):
        return uuid.uuid7()
    value = (time.time_ns() // 1_000_000) << 80 | secrets.randbits(80)
    value = (value & ~(0xF << 76)) | (0x7 << 76)  # version 7
    value = (value & ~(0x3 << 62)) | (0x2 << 62)  # RFC 4122 variant
    return uuid.UUID(int=value)


def new_client_order_id() -> str:
    """New order tag in the form <PREFIX>-<uuid7>."""
    return f"{PREFIX}-{uuid7()}"


_base_record_factory = logging.getLogRecordFactory()


def _record_factory(*args, **kwargs) -> logging.LogRecord:
    """Stamp each record once with the computer name and a fresh uuid7."""
    record = _base_record_factory(*args, **kwargs)
    record.computername = COMPUTER_NAME
    record.uuid7 = str(uuid7())
    return record


def _setup_logging() -> None:
    LOG_DIRECTORY.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        fmt="%(asctime)s.%(msecs)03dZ %(computername)s %(uuid7)s "
        "%(module)s %(lineno)d [%(message)s]",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    formatter.converter = time.gmtime  # timestamps in UTC

    file_handler = RotatingFileHandler(
        LOG_DIRECTORY / LOG_NAME,
        maxBytes=int(MAX_SIZE_IN_MB * 1024 * 1024),
        backupCount=MAX_BACKUP,
        encoding="utf-8",
    )
    console_handler = logging.StreamHandler(sys.stdout)

    logging.setLogRecordFactory(_record_factory)
    root = logging.getLogger()
    root.setLevel(LOG_LEVEL)
    for handler in (file_handler, console_handler):
        handler.setFormatter(formatter)
        root.addHandler(handler)

    # Scheduled runs discard stderr, so log crashes or they leave no trace.
    def log_uncaught(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        root.critical("Uncaught exception; run aborted", exc_info=(exc_type, exc_value, exc_traceback))

    sys.excepthook = log_uncaught


_setup_logging()
