from __future__ import annotations

import json
from typing import Any

from crowai.storage.database import Database, utcnow


class UploadRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(self, *, upload_id: str, owner_key: str, original_name: str, stored_path: str, media_type: str, size_bytes: int, sha256: str, metadata: dict[str, Any]) -> None:
        self.database.execute(
            "INSERT INTO uploads(id,owner_key,original_name,stored_path,media_type,size_bytes,sha256,metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (upload_id, owner_key, original_name, stored_path, media_type, size_bytes, sha256, json.dumps(metadata, ensure_ascii=False), utcnow()),
        )

    def get_for_owner(self, upload_id: str, owner_key: str) -> dict[str, Any] | None:
        return self.database.one(
            "SELECT id,original_name,stored_path,media_type,size_bytes,sha256,metadata_json,created_at FROM uploads WHERE id=? AND owner_key=?",
            (upload_id, owner_key),
        )

    def list_for_owner(self, owner_key: str) -> list[dict[str, Any]]:
        return self.database.all(
            "SELECT id,original_name,stored_path,media_type,size_bytes,sha256,metadata_json,created_at FROM uploads WHERE owner_key=? ORDER BY created_at DESC",
            (owner_key,),
        )

    def delete(self, upload_id: str, owner_key: str) -> None:
        self.database.execute("DELETE FROM uploads WHERE id=? AND owner_key=?", (upload_id, owner_key))

    def delete_all(self, owner_key: str) -> list[str]:
        rows = self.database.all("SELECT stored_path FROM uploads WHERE owner_key=?", (owner_key,))
        self.database.execute("DELETE FROM uploads WHERE owner_key=?", (owner_key,))
        return [str(row["stored_path"]) for row in rows]
