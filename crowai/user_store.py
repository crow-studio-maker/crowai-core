from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

from crowai.db import Database, utcnow
from crowai.storage.permissions import atomic_write_private_text, harden_private_directory, harden_private_tree

_SAFE_USERNAME = re.compile(r"^[a-z0-9][a-z0-9_-]{2,31}$")
_RESERVED_USERNAMES = {"api", "static", "health", "favicon", "favicon.ico"}


def validate_username(value: str) -> str:
    username = str(value or "").strip().casefold()
    if not _SAFE_USERNAME.fullmatch(username):
        raise ValueError("Username must be 3-32 characters and use only letters, numbers, _ or -.")
    if username in _RESERVED_USERNAMES:
        raise ValueError("This username is reserved by CrowAI.")
    return username


class UserJSONStore:
    """Human-readable, atomic per-user snapshots stored under users/user <name>."""

    def __init__(self, root: Path, database: Database, *, strict_permissions: bool = False) -> None:
        self.root = root.resolve()
        self.database = database
        self.strict_permissions = bool(strict_permissions)
        harden_private_tree(self.root, strict=self.strict_permissions)
        self._lock = threading.RLock()

    def directory(self, username: str) -> Path:
        safe = validate_username(username)
        return self.root / f"user {safe}"

    def _atomic_write(self, path: Path, value: Any) -> None:
        atomic_write_private_text(
            path,
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
            strict=self.strict_permissions,
        )

    @staticmethod
    def _read(path: Path, fallback: Any) -> Any:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError):
            return fallback
        return value

    def ensure_user(self, user: dict[str, Any]) -> Path:
        username = validate_username(str(user.get("username") or ""))
        directory = self.directory(username)
        harden_private_directory(directory, strict=self.strict_permissions, create=True)
        harden_private_directory(directory / "conversations", strict=self.strict_permissions, create=True)
        account = {
            "version": 1,
            "user_id": int(user["id"]),
            "username": username,
            "email": str(user.get("email") or ""),
            "display_name": str(user.get("display_name") or username),
            "created_at": user.get("created_at"),
            "last_login": user.get("last_login"),
            "updated_at": utcnow(),
        }
        with self._lock:
            self._atomic_write(directory / "account.json", account)
            for name, default in (
                ("settings.json", {"version": 1, "settings": {}, "updated_at": utcnow()}),
                ("draft.json", {"version": 1, "draft": {}, "updated_at": utcnow()}),
                ("conversations.json", {"version": 1, "conversations": [], "updated_at": utcnow()}),
                ("uploads.json", {"version": 1, "uploads": [], "updated_at": utcnow()}),
            ):
                path = directory / name
                if not path.exists():
                    self._atomic_write(path, default)
        return directory

    def save_settings(self, user: dict[str, Any], settings: dict[str, Any]) -> None:
        directory = self.ensure_user(user)
        with self._lock:
            self._atomic_write(directory / "settings.json", {
                "version": 1,
                "username": user["username"],
                "settings": settings,
                "updated_at": utcnow(),
            })

    def load_draft(self, user: dict[str, Any]) -> dict[str, Any]:
        directory = self.ensure_user(user)
        value = self._read(directory / "draft.json", {})
        draft = value.get("draft") if isinstance(value, dict) else {}
        return draft if isinstance(draft, dict) else {}

    def save_draft(self, user: dict[str, Any], draft: dict[str, Any]) -> None:
        directory = self.ensure_user(user)
        with self._lock:
            self._atomic_write(directory / "draft.json", {
                "version": 1,
                "username": user["username"],
                "draft": draft,
                "updated_at": utcnow(),
            })

    def sync_conversations(self, user: dict[str, Any]) -> None:
        directory = self.ensure_user(user)
        owner = f"user:{int(user['id'])}"
        conversations = self.database.all(
            "SELECT id,title,model_id,created_at,updated_at FROM conversations "
            "WHERE owner_key=? ORDER BY updated_at DESC",
            (owner,),
        )
        complete: list[dict[str, Any]] = []
        live_files: set[str] = set()
        for conversation in conversations:
            messages = self.database.all(
                "SELECT id,role,content,payload_json,created_at FROM messages "
                "WHERE conversation_id=? ORDER BY id",
                (conversation["id"],),
            )
            for message in messages:
                try:
                    payload = json.loads(message.pop("payload_json"))
                except (TypeError, ValueError):
                    payload = {}
                message["payload"] = payload if isinstance(payload, dict) else {}
                message["attachments"] = self.database.all(
                    "SELECT u.id,u.original_name AS name,u.media_type,u.size_bytes,u.sha256,u.created_at "
                    "FROM uploads u JOIN message_uploads mu ON mu.upload_id=u.id WHERE mu.message_id=?",
                    (message["id"],),
                )
            item = {**conversation, "messages": messages, "incomplete": bool(messages and messages[-1]["role"] == "user")}
            complete.append(item)
            filename = f"conversation_{conversation['id']}.json"
            live_files.add(filename)
            with self._lock:
                self._atomic_write(directory / "conversations" / filename, {
                    "version": 1,
                    "username": user["username"],
                    "conversation": item,
                    "updated_at": utcnow(),
                })

        conversation_directory = directory / "conversations"
        for stale in conversation_directory.glob("conversation_*.json"):
            if stale.name not in live_files:
                stale.unlink(missing_ok=True)

        with self._lock:
            self._atomic_write(directory / "conversations.json", {
                "version": 1,
                "username": user["username"],
                "conversations": complete,
                "updated_at": utcnow(),
            })

    def sync_uploads(self, user: dict[str, Any]) -> None:
        directory = self.ensure_user(user)
        owner = f"user:{int(user['id'])}"
        uploads = self.database.all(
            "SELECT id,original_name AS name,media_type,size_bytes,sha256,metadata_json,created_at "
            "FROM uploads WHERE owner_key=? ORDER BY created_at DESC",
            (owner,),
        )
        for item in uploads:
            try:
                metadata = json.loads(item.pop("metadata_json"))
            except (TypeError, ValueError):
                metadata = {}
            item["metadata"] = metadata if isinstance(metadata, dict) else {}
        with self._lock:
            self._atomic_write(directory / "uploads.json", {
                "version": 1,
                "username": user["username"],
                "uploads": uploads,
                "updated_at": utcnow(),
            })

    def sync_all(self, user: dict[str, Any]) -> None:
        self.ensure_user(user)
        self.save_settings(user, self.database.settings(int(user["id"])))
        self.sync_conversations(user)
        self.sync_uploads(user)

    def clear_content(self, user: dict[str, Any]) -> None:
        directory = self.ensure_user(user)
        with self._lock:
            for path in (directory / "conversations").glob("conversation_*.json"):
                path.unlink(missing_ok=True)
            self._atomic_write(directory / "conversations.json", {
                "version": 1, "username": user["username"], "conversations": [], "updated_at": utcnow(),
            })
            self._atomic_write(directory / "uploads.json", {
                "version": 1, "username": user["username"], "uploads": [], "updated_at": utcnow(),
            })
            self._atomic_write(directory / "draft.json", {
                "version": 1, "username": user["username"], "draft": {}, "updated_at": utcnow(),
            })
