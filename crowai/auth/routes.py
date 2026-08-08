from __future__ import annotations

from flask import Blueprint, g, jsonify, request, session

from crowai.auth.security import csrf_token, require_csrf, rotate_session
from crowai.conversations.schemas import object_payload
from crowai.errors import RateLimitExceeded
from crowai.request_context import public_user
from crowai.runtime import get_runtime

auth_bp = Blueprint("auth_api", __name__, url_prefix="/api")


def _rate_limit(action: str, identity: str) -> None:
    runtime = get_runtime()
    remote = request.remote_addr or "unknown"
    limit = 8 if action == "login" else 5
    checks = (
        f"{action}:ip:{remote}",
        f"{action}:account:{remote}:{identity.casefold()[:254]}",
    )
    retry_after = 0
    for key in checks:
        allowed, retry = runtime.auth_limiter.allow(key, limit=limit, window_seconds=60)
        if not allowed:
            retry_after = max(retry_after, retry)
    if retry_after:
        raise RateLimitExceeded("Too many account attempts. Try again later.", retry_after)


@auth_bp.post("/auth/register")
def register():
    require_csrf()
    data = object_payload(request.get_json(silent=True))
    _rate_limit("register", str(data.get("email") or ""))
    runtime = get_runtime()
    user = runtime.auth_service.register(data)
    rotate_session(user_id=int(user["id"]))
    session.permanent = True
    g.user = user
    runtime.snapshot_service.sync(user, settings=True, conversations=True, uploads=True)
    return jsonify({"ok": True, "user": public_user(), "csrf_token": csrf_token()})


@auth_bp.post("/auth/login")
def login():
    require_csrf()
    data = object_payload(request.get_json(silent=True))
    _rate_limit("login", str(data.get("email") or ""))
    runtime = get_runtime()
    user = runtime.auth_service.login(data)
    if not user:
        from crowai.errors import CoreError
        raise CoreError("Invalid email or password.", "INVALID_CREDENTIALS", 401)
    rotate_session(user_id=int(user["id"]))
    session.permanent = True
    g.user = user
    runtime.snapshot_service.sync(user, settings=True, conversations=True, uploads=True)
    return jsonify({"ok": True, "user": public_user(), "csrf_token": csrf_token()})


@auth_bp.post("/auth/logout")
def logout():
    require_csrf()
    rotate_session()
    g.user = None
    return jsonify({"ok": True, "csrf_token": csrf_token()})
