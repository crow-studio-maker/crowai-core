from __future__ import annotations

import json
from typing import Any

from crowai.models.service import ModelService
from crowai.storage.database import Database


class SettingsService:
    """Frozen Settings schema and semantics for the Core release."""

    def __init__(self, database: Database, model_service: ModelService) -> None:
        self.database = database
        self.model_service = model_service

    def get(self, user_id: int | None) -> dict[str, Any]:
        return self.database.settings(user_id) if user_id else {}

    def put(self, user_id: int, data: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "appearance": str(data.get("appearance") or "system") if str(data.get("appearance") or "system") in {"system", "dark", "light"} else "system",
            "language": str(data.get("language") or "auto")[:20],
            "default_model": str(data.get("default_model") or "")[:140],
            "compact_sidebar": bool(data.get("compact_sidebar")),
            "save_history": bool(data.get("save_history", True)),
        }
        if allowed["default_model"]:
            try:
                allowed["default_model"] = self.model_service.validate_selection(allowed["default_model"])
            except Exception:
                allowed["default_model"] = ""
        self.database.execute("UPDATE users SET settings_json=? WHERE id=?", (json.dumps(allowed), user_id))
        return allowed
