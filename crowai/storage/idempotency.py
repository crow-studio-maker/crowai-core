from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from crowai.storage.database import Database, utcnow


class RequestLedgerRepository:
    def __init__(self, database: Database, *, lease_seconds: int = 300, maintenance_interval: int = 64) -> None:
        self.database = database
        self.lease_seconds = max(30, int(lease_seconds))
        self.maintenance_interval = max(8, int(maintenance_interval))
        self._operations = 0

    def _maybe_cleanup(self) -> None:
        self._operations += 1
        if self._operations % self.maintenance_interval == 0:
            self.cleanup_stale()

    def completed(self, owner_key: str, request_key: str, operation: str) -> dict[str, Any] | None:
        if not request_key:
            return None
        row = self.database.one(
            "SELECT state,response_json FROM request_ledger WHERE owner_key=? AND request_key=? AND operation=?",
            (owner_key, request_key, operation),
        )
        if not row or row["state"] != "complete" or not row["response_json"]:
            return None
        try:
            value = json.loads(row["response_json"])
        except (TypeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def claim(self, owner_key: str, request_key: str, operation: str) -> bool:
        if not request_key:
            return True
        self._maybe_cleanup()
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        lease = (now_dt + timedelta(seconds=self.lease_seconds)).isoformat()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT state,lease_expires_at FROM request_ledger WHERE owner_key=? AND request_key=? AND operation=?",
                (owner_key, request_key, operation),
            ).fetchone()
            if row:
                if str(row["state"]) == "complete":
                    return False
                expires = str(row["lease_expires_at"] or "")
                try:
                    parsed = datetime.fromisoformat(expires)
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    stale = parsed <= now_dt
                except ValueError:
                    stale = True
                if not stale:
                    return False
                connection.execute(
                    "UPDATE request_ledger SET state='processing',response_json=NULL,updated_at=?,lease_expires_at=? "
                    "WHERE owner_key=? AND request_key=? AND operation=?",
                    (now, lease, owner_key, request_key, operation),
                )
                return True
            connection.execute(
                "INSERT INTO request_ledger(owner_key,request_key,operation,state,response_json,created_at,updated_at,lease_expires_at) VALUES(?,?,?,?,?,?,?,?)",
                (owner_key, request_key, operation, "processing", None, now, now, lease),
            )
        return True

    def complete(self, owner_key: str, request_key: str, operation: str, response: dict[str, Any]) -> None:
        if request_key:
            self.database.execute(
                "UPDATE request_ledger SET state='complete',response_json=?,updated_at=?,lease_expires_at=NULL "
                "WHERE owner_key=? AND request_key=? AND operation=? AND state='processing'",
                (json.dumps(response, ensure_ascii=False), utcnow(), owner_key, request_key, operation),
            )

    def release(self, owner_key: str, request_key: str, operation: str) -> None:
        if request_key:
            self.database.execute(
                "DELETE FROM request_ledger WHERE owner_key=? AND request_key=? AND operation=? AND state='processing'",
                (owner_key, request_key, operation),
            )

    def cleanup_stale(self, *, retention_days: int = 7) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, retention_days))).isoformat()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM request_ledger WHERE (state='complete' AND updated_at<?) OR "
                "(state='processing' AND lease_expires_at IS NOT NULL AND lease_expires_at<?)",
                (cutoff, datetime.now(timezone.utc).isoformat()),
            )
            return int(cursor.rowcount or 0)
