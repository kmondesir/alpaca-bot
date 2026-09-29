"""SQLite record of orders, so state carries over between cron runs."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import config

# NA        recorded locally, not yet confirmed at Alpaca
# SUBMITTED accepted by Alpaca, nothing filled yet
# PARTIAL   partly filled, still working
# FILLED    completely filled
# CANCELLED cancelled or rejected (costs > 0 if it partly filled first)
# CLOSED    filled quantity has been closed out by close_order(s)
# EXPIRED   expired at Alpaca (costs > 0 if it partly filled first)
# SKIPPED   never reached Alpaca
STATUSES = (
    "NA",
    "SUBMITTED",
    "PARTIAL",
    "FILLED",
    "CANCELLED",
    "CLOSED",
    "EXPIRED",
    "SKIPPED",
)

# Orders whose state can still change at Alpaca, so each run re-checks them.
PENDING_STATUSES = ("NA", "SUBMITTED", "PARTIAL")

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY,
    hostname        TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ({", ".join(f"'{s}'" for s in STATUSES)})),
    created         TEXT NOT NULL,
    modified        TEXT NOT NULL,
    description     TEXT,
    costs           REAL
)
"""


def utc_now() -> str:
    """Current time as ISO 8601 UTC, e.g. 2026-09-29T09:13:40.238Z."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class OrderDB:
    def __init__(self, path: Optional[Path] = None):
        path = Path(path or config.DB_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        with self.conn:
            self.conn.execute(_SCHEMA)

    def save(
        self,
        client_order_id: str,
        status: str,
        description: Optional[str] = None,
        costs: Optional[float] = None,
    ) -> None:
        """Insert or update an order row.

        created and hostname are set on insert only; modified is always
        refreshed. description and costs are left unchanged when None.
        """
        if status not in STATUSES:
            raise ValueError(f"Unknown status {status!r}")
        now = utc_now()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO orders
                    (client_order_id, hostname, status, created, modified, description, costs)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (client_order_id) DO UPDATE SET
                    status      = excluded.status,
                    modified    = excluded.modified,
                    description = COALESCE(excluded.description, orders.description),
                    costs       = COALESCE(excluded.costs, orders.costs)
                """,
                (client_order_id, config.COMPUTER_NAME, status, now, now, description, costs),
            )

    def get(self, client_order_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM orders WHERE client_order_id = ?", (client_order_id,)
        ).fetchone()

    def by_status(self, *statuses: str) -> list[sqlite3.Row]:
        placeholders = ", ".join("?" for _ in statuses)
        return self.conn.execute(
            f"SELECT * FROM orders WHERE status IN ({placeholders}) ORDER BY created",
            statuses,
        ).fetchall()
