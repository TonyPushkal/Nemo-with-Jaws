"""Spend ledgers: durable (SQLite) and in-memory. Amounts are integer micro-USD."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from nemo.budget import BudgetExceeded


class InMemoryLedger:
    def __init__(self) -> None:
        self._rows: list[dict] = []

    def reserve(self, *, run_id, provider, amount_micro, month, cap_micro) -> int:
        if cap_micro is not None and self.month_total_micro(month) + amount_micro > cap_micro:
            raise BudgetExceeded("monthly_usd_cap")
        self._rows.append({"month": month, "reserved": amount_micro, "settled": None})
        return len(self._rows) - 1

    def settle(self, entry_id: int, amount_micro: int) -> None:
        self._rows[entry_id]["settled"] = amount_micro

    def month_total_micro(self, month: str) -> int:
        return sum(r["settled"] if r["settled"] is not None else r["reserved"]
                   for r in self._rows if r["month"] == month)


class SqliteLedger:
    """Unsettled reservations (e.g. after a crash) keep counting at their reserved
    maximum, so a crash can never hide spend."""

    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), isolation_level=None, timeout=30)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS spend_ledger ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, provider TEXT NOT NULL, "
            "month TEXT NOT NULL, reserved_micro INTEGER NOT NULL, settled_micro INTEGER, "
            "created_at TEXT NOT NULL)")

    def month_total_micro(self, month: str) -> int:
        row = self._db.execute(
            "SELECT COALESCE(SUM(COALESCE(settled_micro, reserved_micro)), 0) "
            "FROM spend_ledger WHERE month = ?", (month,)).fetchone()
        return int(row[0])

    def reserve(self, *, run_id, provider, amount_micro, month, cap_micro) -> int:
        self._db.execute("BEGIN IMMEDIATE")  # check + insert are atomic across processes
        try:
            if cap_micro is not None and self.month_total_micro(month) + amount_micro > cap_micro:
                raise BudgetExceeded("monthly_usd_cap")
            cur = self._db.execute(
                "INSERT INTO spend_ledger (run_id, provider, month, reserved_micro, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (run_id, provider, month, amount_micro, datetime.now(timezone.utc).isoformat()))
            self._db.execute("COMMIT")
            return int(cur.lastrowid)
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    def settle(self, entry_id: int, amount_micro: int) -> None:
        self._db.execute("UPDATE spend_ledger SET settled_micro = ? WHERE id = ?", (amount_micro, entry_id))

    def close(self) -> None:
        self._db.close()
