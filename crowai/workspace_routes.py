from __future__ import annotations

from typing import Any

from flask import Blueprint, abort, g, jsonify, redirect, render_template, request

from crowai.auth import require_csrf
from crowai.errors import AuthenticationRequired, ValidationError
from crowai.request_context import owner_key
from crowai.runtime import get_runtime

workspace_bp = Blueprint("workspace", __name__)
workspace_api_bp = Blueprint("workspace_api", __name__, url_prefix="/api")


@workspace_bp.get("/")
def index():
    if g.user:
        return redirect(f"/{g.user['username']}")
    return render_template("workspace.html")


def _user_page(username: str, *, panel: str = "workspace", conversation_id: str = ""):
    if not g.user:
        return redirect("/")
    if str(g.user["username"]).casefold() != str(username).casefold():
        return redirect(f"/{g.user['username']}")
    if conversation_id and not get_runtime().database.one(
        "SELECT id FROM conversations WHERE id=? AND owner_key=?", (conversation_id, owner_key())
    ):
        abort(404)
    return render_template("workspace.html", initial_panel=panel, initial_conversation_id=conversation_id)


@workspace_bp.get("/<username>")
def user_home(username: str):
    return _user_page(username)


@workspace_bp.get("/<username>/settings")
def user_settings_page(username: str):
    return _user_page(username, panel="settings")


@workspace_bp.get("/<username>/chat/<conversation_id>")
def user_conversation_page(username: str, conversation_id: str):
    return _user_page(username, conversation_id=conversation_id)


@workspace_api_bp.get("/state")
def get_state():
    if not g.user:
        return jsonify({"draft": {}})
    runtime = get_runtime()
    draft = runtime.user_store.load_draft(g.user)
    attachment_ids = draft.get("attachment_ids") if isinstance(draft.get("attachment_ids"), list) else []
    draft["attachments"] = runtime.upload_service.public(attachment_ids, owner_key())
    return jsonify({"draft": draft})


@workspace_api_bp.put("/state")
def put_state():
    require_csrf()
    if not g.user:
        raise AuthenticationRequired("Sign in to save workspace state.")
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValidationError("The request body must be a JSON object.")
    runtime = get_runtime()
    prompt = str(data.get("prompt") or "")
    if len(prompt) > int(runtime_config("MAX_MESSAGE_LENGTH")):
        raise ValidationError(f"Drafts may contain at most {runtime_config('MAX_MESSAGE_LENGTH')} characters.")
    panel = str(data.get("panel") or "workspace")
    if panel not in {"workspace", "settings"}:
        panel = "workspace"
    current_id = str(data.get("current_id") or "")[:64]
    if current_id and not runtime.database.one(
        "SELECT id FROM conversations WHERE id=? AND owner_key=?", (current_id, owner_key())
    ):
        current_id = ""
    raw_attachment_ids = data.get("attachment_ids") if isinstance(data.get("attachment_ids"), list) else []
    safe_attachments = runtime.upload_service.public([str(value) for value in raw_attachment_ids[:10]], owner_key())
    attachment_ids = [str(item["id"]) for item in safe_attachments]
    draft_model_id = str(data.get("draft_model_id") or "")[:140]
    if draft_model_id:
        try:
            draft_model_id = runtime.model_service.validate_selection(draft_model_id)
        except Exception:
            draft_model_id = ""
    draft_request_id = str(data.get("draft_request_id") or "")[:100]
    draft: dict[str, Any] = {
        "prompt": prompt,
        "panel": panel,
        "current_id": current_id,
        "draft_model_id": draft_model_id,
        "draft_request_id": draft_request_id,
        "attachment_ids": attachment_ids,
    }
    runtime.user_store.save_draft(g.user, draft)
    return jsonify({"ok": True, "draft": {**draft, "attachments": safe_attachments}})


def runtime_config(name: str) -> Any:
    from flask import current_app

    return current_app.config[name]
