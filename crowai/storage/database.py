from __future__ import annotations

import importlib.util
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from crowai.storage.permissions import (
    PrivatePermissionError,
    harden_private_directory,
    harden_private_directory_chain,
    harden_private_file,
    harden_sqlite_sidecars,
)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(
        self,
        path: Path,
        *,
        busy_timeout_ms: int = 15_000,
        private_root: Path | None = None,
        strict_permissions: bool = False,
    ) -> None:
        self.path = Path(path).resolve()
        self.busy_timeout_ms = int(busy_timeout_ms)
        self.strict_permissions = bool(strict_permissions)
        self.private_root = Path(private_root).resolve() if private_root is not None else self.path.parent
        try:
            self.path.relative_to(self.private_root)
            inside_private_root = True
        except ValueError:
            inside_private_root = False

        if inside_private_root:
            harden_private_directory_chain(
                self.private_root,
                self.path.parent,
                strict=self.strict_permissions,
            )
        elif self.strict_permissions:
            raise PrivatePermissionError(
                "Production database path must remain under the configured private instance root."
            )
        else:
            # Development may explicitly place the DB elsewhere.  Harden the
            # immediate database directory, but do not recursively chmod
            # unrelated ancestors outside CrowAI's configured private root.
            harden_private_directory(self.path.parent, strict=False, create=True)
        # Pre-create the database as 0600 so SQLite never has to create a
        # private file using the process umask. Existing databases are hardened
        # before migrations are opened.
        harden_private_file(self.path, strict=self.strict_permissions, create=True)
        self.apply_migrations()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        harden_private_file(self.path, strict=self.strict_permissions, create=True)
        connection = sqlite3.connect(self.path, timeout=max(1, self.busy_timeout_ms // 1000))
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        harden_sqlite_sidecars(self.path, strict=self.strict_permissions)
        try:
            yield connection
            connection.commit()
            harden_sqlite_sidecars(self.path, strict=self.strict_permissions)
        except Exception:
            connection.rollback()
            harden_sqlite_sidecars(self.path, strict=self.strict_permissions)
            raise
        finally:
            # WAL/SHM may be created after the initial PRAGMA when the first
            # write occurs, so harden both before and after closing.
            harden_sqlite_sidecars(self.path, strict=self.strict_permissions)
            connection.close()
            harden_sqlite_sidecars(self.path, strict=self.strict_permissions)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            yield connection

    def apply_migrations(self) -> None:
        migration_dir = Path(__file__).with_name("migrations")
        files = sorted(path for path in migration_dir.glob("[0-9][0-9][0-9][0-9]_*.py") if path.is_file())
        with self.connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            applied = {int(row[0]) for row in connection.execute("SELECT version FROM schema_migrations")}
            for path in files:
                version = int(path.name.split("_", 1)[0])
                if version in applied:
                    continue
                spec = importlib.util.spec_from_file_location(f"crowai_migration_{version}", path)
                if spec is None or spec.loader is None:
                    raise RuntimeError(f"Unable to load database migration {path.name}.")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                connection.execute("SAVEPOINT crowai_migration")
                try:
                    module.apply(connection)
                    connection.execute(
                        "INSERT INTO schema_migrations(version,applied_at) VALUES(?,?)", (version, utcnow())
                    )
                    connection.execute("RELEASE SAVEPOINT crowai_migration")
                except Exception:
                    connection.execute("ROLLBACK TO SAVEPOINT crowai_migration")
                    connection.execute("RELEASE SAVEPOINT crowai_migration")
                    raise

    def one(self, sql: str, values: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(sql, values).fetchone()
        return dict(row) if row else None

    def all(self, sql: str, values: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(sql, values).fetchall()
        return [dict(row) for row in rows]

    def execute(self, sql: str, values: tuple[Any, ...] = ()) -> int:
        with self.connect() as connection:
            cursor = connection.execute(sql, values)
            return int(cursor.lastrowid or 0)

    def settings(self, user_id: int) -> dict[str, Any]:
        row = self.one("SELECT settings_json FROM users WHERE id=?", (user_id,))
        if not row:
            return {}
        try:
            value = json.loads(row["settings_json"])
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def health(self) -> dict[str, Any]:
        try:
            row = self.one("PRAGMA integrity_check")
            value = next(iter(row.values())) if row else "unknown"
            return {"ok": str(value).casefold() == "ok", "integrity": str(value)}
        except Exception:
            return {"ok": False, "integrity": "unavailable"}
