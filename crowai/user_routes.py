from __future__ import annotations

from flask import Blueprint, g, jsonify

from crowai.auth import require_csrf
from crowai.request_context import owner_key
from crowai.runtime import get_runtime

user_api_bp = Blueprint("user_api", __name__, url_prefix="/api/me")


@user_api_bp.delete("/data")
def clear_data():
    require_csrf()
    runtime = get_runtime()
    key = owner_key()
    for conversation in runtime.conversation_service.list(key):
        runtime.conversation_service.delete(str(conversation["id"]), key)
    runtime.upload_service.clear_owner(key)
    if g.user:
        runtime.user_store.clear_content(g.user)
    return jsonify({"ok": True})
