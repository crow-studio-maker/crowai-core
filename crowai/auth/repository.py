from __future__ import annotations

from typing import Any

from crowai.storage.database import Database, utcnow


class UserRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(self, *, username: str, email: str, password_hash: str, display_name: str) -> int:
        return self.database.execute(
            "INSERT INTO users(username,email,password_hash,display_name,created_at) VALUES(?,?,?,?,?)",
            (username, email, password_hash, display_name, utcnow()),
        )

    def by_email(self, email: str) -> dict[str, Any] | None:
        return self.database.one("SELECT * FROM users WHERE email=?", (email,))

    def public_by_id(self, user_id: int) -> dict[str, Any] | None:
        return self.database.one(
            "SELECT id,username,email,display_name,settings_json,created_at,last_login FROM users WHERE id=?",
            (user_id,),
        )

    def update_last_login(self, user_id: int) -> None:
        self.database.execute("UPDATE users SET last_login=? WHERE id=?", (utcnow(), user_id))
