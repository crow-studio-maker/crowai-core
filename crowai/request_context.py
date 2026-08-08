from __future__ import annotations

import logging
import secrets
import time
import uuid
from typing import Any

from flask import Flask, g, request, session

from crowai.runtime import get_runtime

_LOG = logging.getLogger("crowai.request")


def owner_key() -> str:
    if getattr(g, "user", None):
        return f"user:{g.user['id']}"
    token = session.get("guest_token")
    if not isinstance(token, str) or len(token) < 24:
        token = secrets.token_urlsafe(24)
        session["guest_token"] = token
    return f"guest:{token}"


def public_user() -> dict[str, Any] | None:
    user = getattr(g, "user", None)
    if not user:
        return None
    username = str(user["username"])
    return {
        "id": user["id"],
        "username": username,
        "email": user["email"],
        "display_name": user["display_name"],
        "routes": {"home": f"/{username}", "settings": f"/{username}/settings", "chat_base": f"/{username}/chat"},
    }


def register_request_hooks(app: Flask) -> None:
    @app.before_request
    def load_request_context() -> None:
        g.request_id = uuid.uuid4().hex
        g.request_started = time.monotonic()
        g.user = None
        user_id = session.get("user_id")
        if user_id:
            try:
                g.user = get_runtime().database.one(
                    "SELECT id,username,email,display_name,settings_json,created_at,last_login FROM users WHERE id=?",
                    (int(user_id),),
                )
            except (TypeError, ValueError):
                session.clear()

    @app.after_request
    def log_request(response):
        duration_ms = int((time.monotonic() - getattr(g, "request_started", time.monotonic())) * 1000)
        user = getattr(g, "user", None)
        owner_type = f"user:{user['id']}" if user else "guest"
        _LOG.info(
            "request_complete request_id=%s method=%s route=%s status=%s duration_ms=%s owner=%s",
            getattr(g, "request_id", ""), request.method, request.path, response.status_code, duration_ms, owner_type,
        )
        return response
