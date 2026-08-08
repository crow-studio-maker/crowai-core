from __future__ import annotations

from pathlib import Path

from crowai.storage.database import Database


def test_numbered_migrations_are_recorded(tmp_path: Path) -> None:
    database = Database(tmp_path / "workspace.db")
    versions = database.all("SELECT version FROM schema_migrations ORDER BY version")
    assert [row["version"] for row in versions] == [1, 2, 3, 4, 5, 6]
    assert database.one("SELECT name FROM sqlite_master WHERE type='table' AND name='request_ledger'")
