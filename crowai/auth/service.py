from __future__ import annotations

import sqlite3
from typing import Any

from crowai.auth.repository import UserRepository
from crowai.auth.security import password_hash, valid_login, validate_email
from crowai.errors import ConflictError, ValidationError
from crowai.user_store import validate_username


class AuthService:
    def __init__(self, repository: UserRepository) -> None:
        self.repository = repository

    def register(self, data: dict[str, Any]) -> dict[str, Any]:
        try:
            email = validate_email(str(data.get("email") or ""))
            digest = password_hash(str(data.get("password") or ""))
            username = validate_username(str(data.get("username") or data.get("display_name") or email.split("@", 1)[0]))
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        display_name = " ".join(str(data.get("display_name") or username).split()).strip()
        if not display_name or len(display_name) > 80:
            raise ValidationError("Display name must be between 1 and 80 characters.")
        try:
            user_id = self.repository.create(username=username, email=email, password_hash=digest, display_name=display_name)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("Unable to create an account with those details.") from exc
        user = self.repository.public_by_id(user_id)
        if not user:
            raise ConflictError("The account could not be loaded after creation.")
        return user

    def login(self, data: dict[str, Any]) -> dict[str, Any] | None:
        try:
            email = validate_email(str(data.get("email") or ""))
        except ValueError:
            email = ""
        row = self.repository.by_email(email) if email else None
        if not valid_login(row, str(data.get("password") or "")):
            return None
        user_id = int(row["id"])
        self.repository.update_last_login(user_id)
        return self.repository.public_by_id(user_id)
