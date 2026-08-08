from __future__ import annotations

import logging
from typing import Any

from crowai.storage.database import Database
from crowai.user_store import UserJSONStore

_LOG = logging.getLogger(__name__)


class UserSnapshotService:
    """Maintains rebuildable human-readable JSON snapshots; SQLite remains authoritative."""

    def __init__(self, store: UserJSONStore, database: Database) -> None:
        self.store = store
        self.database = database

    def sync(self, user: dict[str, Any] | None, *, conversations: bool = False, uploads: bool = False, settings: bool = False) -> None:
        if not user:
            return
        try:
            self.store.ensure_user(user)
            if settings:
                self.store.save_settings(user, self.database.settings(int(user["id"])))
            if conversations:
                self.store.sync_conversations(user)
            if uploads:
                self.store.sync_uploads(user)
        except Exception:
            _LOG.exception("User JSON snapshot synchronization failed")

    def sync_all(self, user: dict[str, Any] | None) -> None:
        if not user:
            return
        try:
            self.store.sync_all(user)
        except Exception:
            _LOG.exception("User JSON snapshot bootstrap failed")
