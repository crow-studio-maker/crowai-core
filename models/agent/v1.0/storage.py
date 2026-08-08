"""Bounded SQLite cache and session storage for Agent V1.0."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
import threading
import time
from pathlib import Path
from typing import Any

from models.runtime_state import ensure_private_file, harden_state_file


class AgentStorage:
    """Thread-safe bounded cache for web pages and compact Agent follow-up state."""

    def __init__(
        self,
        database_path: Path,
        *,
        max_page_rows: int = 500,
        max_product_rows: int = 500,
        max_session_rows: int = 500,
        session_ttl_seconds: int = 7 * 24 * 3600,
        maintenance_interval: int = 32,
    ) -> None:
        self.database_path = database_path.resolve()
        self.max_page_rows = max(16, int(max_page_rows))
        self.max_product_rows = max(16, int(max_product_rows))
        self.max_session_rows = max(16, int(max_session_rows))
        self.session_ttl_seconds = max(300, int(session_ttl_seconds))
        self.maintenance_interval = max(1, int(maintenance_interval))
        self._lock = threading.RLock()
        self._operations = 0
        self._initialized = False

    def _harden_database_files(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(self.database_path) + suffix)
            if path.exists():
                harden_state_file(path)

    @contextmanager
    def _raw_connect(self):
        ensure_private_file(self.database_path)
        connection = sqlite3.connect(self.database_path, timeout=20, isolation_level=None, check_same_thread=False)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=20000")
            yield connection
        finally:
            connection.close()
            self._harden_database_files()

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            ensure_private_file(self.database_path)
            with self._raw_connect() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS page_cache (
                        cache_key TEXT PRIMARY KEY,
                        url TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        expires_at REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS session_state (
                        session_key TEXT PRIMARY KEY,
                        payload_json TEXT NOT NULL,
                        updated_at REAL NOT NULL,
                        expires_at REAL
                    );
                    CREATE TABLE IF NOT EXISTS product_cache (
                        cache_key TEXT PRIMARY KEY,
                        payload_json TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        expires_at REAL NOT NULL
                    );
                    """
                )
                columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(session_state)").fetchall()}
                if "expires_at" not in columns:
                    connection.execute("ALTER TABLE session_state ADD COLUMN expires_at REAL")
                connection.executescript(
                    """
                    CREATE INDEX IF NOT EXISTS idx_page_cache_expires ON page_cache(expires_at);
                    CREATE INDEX IF NOT EXISTS idx_session_state_expires ON session_state(expires_at);
                    CREATE INDEX IF NOT EXISTS idx_product_cache_expires ON product_cache(expires_at);
                    """
                )
            self._initialized = True

    @contextmanager
    def _connect(self):
        self._ensure_initialized()
        with self._raw_connect() as connection:
            yield connection

    @staticmethod
    def key_for(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()

    def _maybe_maintain(self) -> None:
        self._operations += 1
        if self._operations % self.maintenance_interval == 0:
            self.cleanup()

    def get_page(self, url: str) -> dict[str, Any] | None:
        key = self.key_for(url)
        now = time.time()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json,expires_at FROM page_cache WHERE cache_key=?",
                (key,),
            ).fetchone()
            if row is not None and float(row["expires_at"] or 0) <= now:
                connection.execute("DELETE FROM page_cache WHERE cache_key=?", (key,))
                row = None
        self._maybe_maintain()
        if row is None:
            return None
        try:
            value = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None

    def put_page(self, url: str, payload: dict[str, Any], *, ttl_seconds: int) -> None:
        now = time.time()
        expires = now + max(1, int(ttl_seconds))
        key = self.key_for(url)
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO page_cache(cache_key,url,payload_json,created_at,expires_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(cache_key) DO UPDATE SET url=excluded.url,payload_json=excluded.payload_json,created_at=excluded.created_at,expires_at=excluded.expires_at",
                (key, url, json.dumps(payload, ensure_ascii=False), now, expires),
            )
        self._maybe_maintain()

    def load_session(self, session_key: str) -> dict[str, Any]:
        key = str(session_key or "").strip()
        if not key:
            return {}
        now = time.time()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json,expires_at FROM session_state WHERE session_key=?",
                (key,),
            ).fetchone()
            if row is not None and row["expires_at"] is not None and float(row["expires_at"]) <= now:
                connection.execute("DELETE FROM session_state WHERE session_key=?", (key,))
                row = None
        self._maybe_maintain()
        if row is None:
            return {}
        try:
            value = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}

    def save_session(self, session_key: str, payload: dict[str, Any]) -> None:
        key = str(session_key or "").strip()
        if not key:
            return
        # Keep Core-owned Agent state compact and non-authoritative.
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(raw) > 24_000:
            payload = {
                "last_question": str(payload.get("last_question") or "")[:2000],
                "last_answer": str(payload.get("last_answer") or "")[:4000],
                "plan": payload.get("plan") if isinstance(payload.get("plan"), dict) else {},
                "products": (payload.get("products") or [])[:8] if isinstance(payload.get("products"), list) else [],
                "sources": (payload.get("sources") or [])[:8] if isinstance(payload.get("sources"), list) else [],
                "updated_at": payload.get("updated_at"),
            }
            raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            if len(raw) > 24_000:
                payload = {
                    "last_question": str(payload.get("last_question") or "")[:1200],
                    "last_answer": str(payload.get("last_answer") or "")[:2400],
                    "updated_at": payload.get("updated_at"),
                }
                raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        now = time.time()
        expires = now + self.session_ttl_seconds
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO session_state(session_key,payload_json,updated_at,expires_at) VALUES(?,?,?,?) "
                "ON CONFLICT(session_key) DO UPDATE SET payload_json=excluded.payload_json,updated_at=excluded.updated_at,expires_at=excluded.expires_at",
                (key, raw, now, expires),
            )
        self._maybe_maintain()

    def delete_session(self, session_key: str) -> None:
        key = str(session_key or "").strip()
        if key:
            with self._lock, self._connect() as connection:
                connection.execute("DELETE FROM session_state WHERE session_key=?", (key,))

    def cleanup(self) -> dict[str, int]:
        now = time.time()
        counts: dict[str, int] = {}
        with self._lock, self._connect() as connection:
            counts["expired_pages"] = int(connection.execute("DELETE FROM page_cache WHERE expires_at<=?", (now,)).rowcount or 0)
            counts["expired_products"] = int(connection.execute("DELETE FROM product_cache WHERE expires_at<=?", (now,)).rowcount or 0)
            counts["expired_sessions"] = int(connection.execute("DELETE FROM session_state WHERE expires_at IS NOT NULL AND expires_at<=?", (now,)).rowcount or 0)
            for table, maximum, order_column in (
                ("page_cache", self.max_page_rows, "created_at"),
                ("product_cache", self.max_product_rows, "created_at"),
                ("session_state", self.max_session_rows, "updated_at"),
            ):
                row = connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
                excess = max(0, int(row["count"] if row else 0) - maximum)
                if excess:
                    cursor = connection.execute(
                        f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} ORDER BY {order_column} ASC LIMIT ?)",
                        (excess,),
                    )
                    counts[f"pruned_{table}"] = int(cursor.rowcount or 0)
        return counts

    def stats(self) -> dict[str, int]:
        with self._lock, self._connect() as connection:
            return {
                "page_rows": int(connection.execute("SELECT COUNT(*) FROM page_cache").fetchone()[0]),
                "product_rows": int(connection.execute("SELECT COUNT(*) FROM product_cache").fetchone()[0]),
                "session_rows": int(connection.execute("SELECT COUNT(*) FROM session_state").fetchone()[0]),
            }
