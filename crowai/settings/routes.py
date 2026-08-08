from __future__ import annotations

from flask import Blueprint, g, jsonify, request

from crowai.auth import require_csrf
from crowai.errors import AuthenticationRequired, ValidationError
from crowai.runtime import get_runtime

settings_api_bp = Blueprint("settings_api", __name__, url_prefix="/api")


@settings_api_bp.get("/settings")
def get_settings():
    user_id = int(g.user["id"]) if g.user else None
    return jsonify({"settings": get_runtime().settings_service.get(user_id)})


@settings_api_bp.put("/settings")
def put_settings():
    require_csrf()
    if not g.user:
        raise AuthenticationRequired("Sign in to save settings.")
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValidationError("The request body must be a JSON object.")
    runtime = get_runtime()
    settings = runtime.settings_service.put(int(g.user["id"]), data)
    runtime.snapshot_service.sync(g.user, settings=True)
    return jsonify({"ok": True, "settings": settings})
