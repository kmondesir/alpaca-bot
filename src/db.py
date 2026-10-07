"""SQLite record of orders, so state carries over between cron runs."""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import config

STATUSES = ("NA", "SUBMITTED", "PARTIAL", "FILLED", "CANCELLED", "CLOSED", "EXPIRED", "SKIPPED")
PENDING_STATUSES = ("NA", "SUBMITTED", "PARTIAL")

_PARENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS parent (
    parent_id       TEXT PRIMARY KEY,
    hostname        TEXT NOT NULL,
    status          TEXT NOT NULL,
    entry_status    TEXT NOT NULL,
    created         TEXT NOT NULL,
    modified        TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    description     TEXT,
    costs           REAL,
    basis           REAL,
    side            TEXT,
    quantity        REAL,
    order_type      TEXT,
    close_order_id  TEXT UNIQUE,
    close_status    TEXT,
    close_costs     REAL,
    close_side      TEXT,
    close_quantity  REAL,
    close_type      TEXT,
    stop_price      REAL
)
"""

_CHILD_SCHEMA = """
CREATE TABLE IF NOT EXISTS child (
    child_id         TEXT PRIMARY KEY,
    parent_id        TEXT NOT NULL REFERENCES parent(parent_id),
    role             TEXT NOT NULL CHECK (role IN ('take_profit', 'trailing_stop')),
    hostname         TEXT NOT NULL,
    status           TEXT NOT NULL,
    created          TEXT NOT NULL,
    modified         TEXT NOT NULL,
    description      TEXT,
    costs            REAL,
    linked_child_id  TEXT,
    basis            REAL,
    side             TEXT,
    quantity         REAL,
    order_type       TEXT
)
"""


def utc_now() -> str:
    """Current time as ISO 8601 UTC, e.g. 2026-09-29T09:13:40.238Z."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class OrderDB:
    def __init__(self, path: Optional[Path] = None):
        path = Path(path or config.DB_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        with self.conn:
            self.conn.execute("DROP TABLE IF EXISTS orders")
            self.conn.execute(_PARENT_SCHEMA)
            self.conn.execute(_CHILD_SCHEMA)
            columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(parent)")}
            if "stop_price" not in columns:
                self.conn.execute("ALTER TABLE parent ADD COLUMN stop_price REAL")

    def create_parent(
        self,
        parent_id: str,
        symbol: str,
        description: Optional[str],
        side: str,
        quantity: Optional[float],
        order_type: str,
    ) -> None:
        now = utc_now()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO parent
                    (parent_id, hostname, status, entry_status, created, modified,
                     symbol, description, side, quantity, order_type)
                VALUES (?, ?, 'NA', 'NA', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    parent_id,
                    config.COMPUTER_NAME,
                    now,
                    now,
                    symbol,
                    description,
                    side,
                    quantity,
                    order_type,
                ),
            )

    def save_parent_entry(
        self,
        parent_id: str,
        status: str,
        description: Optional[str],
        costs: float,
        side: str,
        quantity: Optional[float],
        order_type: str,
    ) -> None:
        if status not in STATUSES:
            raise ValueError(f"Unknown entry status {status!r}")
        row = self.get_parent(parent_id)
        if row is None:
            raise KeyError(f"No parent order found for {parent_id!r}")
        overall_status = "OPEN" if costs > 0 else status
        if row["status"] in ("CLOSING", "CLOSED"):
            overall_status = row["status"]
        now = utc_now()
        with self.conn:
            self.conn.execute(
                """
                UPDATE parent
                SET status = ?, entry_status = ?, modified = ?,
                    description = COALESCE(?, description), costs = ?,
                    basis = CASE WHEN ? > 0 THEN ? ELSE basis END,
                    side = ?, quantity = COALESCE(?, quantity), order_type = ?
                WHERE parent_id = ?
                """,
                (
                    overall_status,
                    status,
                    now,
                    description,
                    costs,
                    costs,
                    costs,
                    side,
                    quantity,
                    order_type,
                    parent_id,
                ),
            )

    def create_child(
        self,
        child_id: str,
        parent_id: str,
        role: str,
        description: Optional[str],
        side: str,
        quantity: Optional[float],
        order_type: str,
    ) -> None:
        if role not in ("take_profit", "trailing_stop"):
            raise ValueError(f"Unknown protective child role {role!r}")
        now = utc_now()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO child
                    (child_id, parent_id, role, hostname, status, created, modified,
                     description, side, quantity, order_type)
                VALUES (?, ?, ?, ?, 'NA', ?, ?, ?, ?, ?, ?)
                """,
                (
                    child_id,
                    parent_id,
                    role,
                    config.COMPUTER_NAME,
                    now,
                    now,
                    description,
                    side,
                    quantity,
                    order_type,
                ),
            )

    def save_child_order(
        self,
        child_id: str,
        status: str,
        description: Optional[str],
        costs: float,
        side: str,
        quantity: Optional[float],
        order_type: str,
        basis: Optional[float] = None,
    ) -> None:
        if status not in STATUSES:
            raise ValueError(f"Unknown child status {status!r}")
        now = utc_now()
        with self.conn:
            cursor = self.conn.execute(
                """
                UPDATE child
                SET status = ?, modified = ?, description = COALESCE(?, description),
                    costs = ?, side = ?, quantity = COALESCE(?, quantity),
                    order_type = ?, basis = COALESCE(?, basis)
                WHERE child_id = ?
                """,
                (status, now, description, costs, side, quantity, order_type, basis, child_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"No child order found for {child_id!r}")

    def link_children(self, first_id: str, second_id: str, basis: float) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE child SET linked_child_id = ?, basis = ? WHERE child_id = ?",
                (second_id, basis, first_id),
            )
            self.conn.execute(
                "UPDATE child SET linked_child_id = ?, basis = ? WHERE child_id = ?",
                (first_id, basis, second_id),
            )

    def save_parent_close(
        self,
        parent_id: str,
        close_order_id: str,
        status: str,
        costs: float,
        side: str,
        quantity: Optional[float],
        order_type: str,
    ) -> None:
        if status not in STATUSES:
            raise ValueError(f"Unknown close status {status!r}")
        row = self.get_parent(parent_id)
        if row is None:
            raise KeyError(f"No parent order found for {parent_id!r}")
        overall_status = "CLOSED" if status == "FILLED" else "CLOSING"
        if status in ("CANCELLED", "EXPIRED", "SKIPPED"):
            overall_status = "OPEN" if (row["costs"] or 0) > 0 else status
        now = utc_now()
        with self.conn:
            self.conn.execute(
                """
                UPDATE parent
                SET status = ?, modified = ?, close_order_id = ?, close_status = ?,
                    close_costs = ?, close_side = ?, close_quantity = ?, close_type = ?
                WHERE parent_id = ?
                """,
                (
                    overall_status,
                    now,
                    close_order_id,
                    status,
                    costs,
                    side,
                    quantity,
                    order_type,
                    parent_id,
                ),
            )

    def get_parent(self, parent_id: str) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM parent WHERE parent_id = ?", (parent_id,)).fetchone()
        return dict(row) if row is not None else None

    def get_child(self, child_id: str) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM child WHERE child_id = ?", (child_id,)).fetchone()
        return dict(row) if row is not None else None

    def get(self, client_order_id: str) -> Optional[dict]:
        return self.get_parent(client_order_id) or self.get_child(client_order_id)

    def get_sibling(self, child_id: str) -> Optional[dict]:
        row = self.get_child(child_id)
        if row is None or row["linked_child_id"] is None:
            return None
        return self.get_child(row["linked_child_id"])

    def parents_by_entry_status(self, *statuses: str) -> list[dict]:
        placeholders = ", ".join("?" for _ in statuses)
        rows = self.conn.execute(
            f"SELECT * FROM parent WHERE entry_status IN ({placeholders}) ORDER BY created",
            statuses,
        ).fetchall()
        return [dict(row) for row in rows]

    def parents_by_close_status(self, *statuses: str) -> list[dict]:
        placeholders = ", ".join("?" for _ in statuses)
        rows = self.conn.execute(
            f"SELECT * FROM parent WHERE close_status IN ({placeholders}) ORDER BY created",
            statuses,
        ).fetchall()
        return [dict(row) for row in rows]

    def children_by_status(self, *statuses: str) -> list[dict]:
        placeholders = ", ".join("?" for _ in statuses)
        rows = self.conn.execute(
            f"SELECT * FROM child WHERE status IN ({placeholders}) ORDER BY created",
            statuses,
        ).fetchall()
        return [dict(row) for row in rows]

    def children_by_parent(self, parent_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM child WHERE parent_id = ? ORDER BY created", (parent_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def parents_by_status(self, *statuses: str) -> list[dict]:
        placeholders = ", ".join("?" for _ in statuses)
        rows = self.conn.execute(
            f"SELECT * FROM parent WHERE status IN ({placeholders}) ORDER BY created",
            statuses,
        ).fetchall()
        return [dict(row) for row in rows]

    def set_parent_stop(self, parent_id: str, stop_price: float) -> None:
        """Store the trailing stop the bot enforces itself (options have no Alpaca trailing stop)."""
        with self.conn:
            self.conn.execute(
                "UPDATE parent SET stop_price = ?, modified = ? WHERE parent_id = ?",
                (stop_price, utc_now(), parent_id),
            )

    def mark_parent_closed(self, parent_id: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE parent SET status = 'CLOSED', modified = ? WHERE parent_id = ?",
                (utc_now(), parent_id),
            )
