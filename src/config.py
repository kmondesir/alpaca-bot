"""Load settings from .env and set up logging.

Call setup_logging() once at startup; every other module only needs
`logger = logging.getLogger(__name__)`.
"""

import logging
import os
import secrets
import socket
import sys
import time
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

SRC_DIR = Path(__file__).resolve().parent
ROOT_DIR = SRC_DIR.parent

load_dotenv(ROOT_DIR / ".env")

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
DEMO = os.getenv("DEMO", "true").lower() == "true"
# Trading API base URL, e.g. https://paper-api.alpaca.markets or https://api.alpaca.markets.
BASE_URL = os.getenv("ALPACA_BASE_URL", "")

# Log settings. A relative LOG_DIRECTORY resolves from the project root;
# --logdirectory and --logname override the first two for a single run.
LOG_DIRECTORY = "logs"
LOG_NAME = "alpaca.log"
MAX_SIZE_IN_MB = 10
MAX_BACKUP = 5
LOG_LEVEL = "INFO"

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
    """Random UUID used as the client_order_id when the caller doesn't provide one."""
    return str(uuid.uuid4())


_base_record_factory = logging.getLogRecordFactory()


def _record_factory(*args, **kwargs) -> logging.LogRecord:
    """Stamp each record once with the computer name and a fresh uuid7."""
    record = _base_record_factory(*args, **kwargs)
    record.computername = COMPUTER_NAME
    record.uuid7 = str(uuid7())
    return record


def setup_logging(directory: Optional[str] = None, name: Optional[str] = None) -> Path:
    """Log to a rotating file and to stderr; returns the log file path.

    directory and name override LOG_DIRECTORY and LOG_NAME. A relative
    directory resolves from the project root. The console handler writes to
    stderr so stdout carries only the command's JSON result.
    """
    log_directory = ROOT_DIR / (directory or LOG_DIRECTORY)
    log_directory.mkdir(parents=True, exist_ok=True)
    log_file = log_directory / (name or LOG_NAME)

    formatter = logging.Formatter(
        fmt="%(asctime)s.%(msecs)03dZ %(computername)s %(uuid7)s "
        "%(module)s %(lineno)d [%(message)s]",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    formatter.converter = time.gmtime  # timestamps in UTC

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=int(MAX_SIZE_IN_MB * 1024 * 1024),
        backupCount=MAX_BACKUP,
        encoding="utf-8",
    )
    console_handler = logging.StreamHandler(sys.stderr)

    logging.setLogRecordFactory(_record_factory)
    root = logging.getLogger()
    root.setLevel(LOG_LEVEL)
    for handler in (file_handler, console_handler):
        handler.setFormatter(formatter)
        root.addHandler(handler)

    # Log crashes too, or a scheduled run that discards stderr leaves no trace.
    def log_uncaught(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        root.critical("Uncaught exception; run aborted", exc_info=(exc_type, exc_value, exc_traceback))

    sys.excepthook = log_uncaught
    return log_file
