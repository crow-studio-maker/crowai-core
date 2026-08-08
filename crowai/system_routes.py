from __future__ import annotations

from typing import Any

from flask import Blueprint, g, jsonify, session

from crowai.auth import csrf_token
from crowai.request_context import owner_key, public_user
from crowai.runtime import get_runtime

system_bp = Blueprint("system_api", __name__)


@system_bp.get("/health")
def health():
    runtime = get_runtime()
    if runtime.registry.development:
        runtime.registry.refresh()
    status = runtime.model_service.status()
    database_health = runtime.database.health()
    models_available = bool(status["models_available"])
    installed = list(status.get("installed_models") or status.get("models") or [])
    runnable = list(status.get("runnable_models") or [])
    issues = list(status["model_issues"])
    if not installed:
        issues.insert(0, {"code": "NO_MODELS_INSTALLED", "message": "No valid CrowAI model packages are installed.", "severity": "warning"})
    elif not models_available:
        issues.insert(0, {"code": "NO_RUNNABLE_MODELS", "message": "Model packages are installed, but required local model/runtime files are unavailable.", "severity": "warning"})
    if not database_health["ok"]:
        issues.append({"code": "DATABASE_UNHEALTHY", "message": "The database integrity check failed.", "severity": "error"})
    state = "healthy" if models_available and database_health["ok"] else "degraded"
    return jsonify({
        "ok": database_health["ok"],
        "status": state,
        "core_ready": database_health["ok"],
        "models_available": models_available,
        "installed_models": installed,
        "runnable_models": runnable,
        "installed_model_count": len(installed),
        "runnable_model_count": len(runnable),
        "model_count": len(installed),
        "model_error": status["model_error"],
        "model_issues": status["model_issues"],
        "issues": issues,
        "database": database_health,
    })


@system_bp.get("/api/bootstrap")
def bootstrap():
    runtime = get_runtime()
    if runtime.registry.development:
        runtime.registry.refresh()
    status = runtime.model_service.status()
    settings = runtime.settings_service.get(int(g.user["id"])) if g.user else {}
    draft: dict[str, Any] = {}
    if g.user:
        runtime.snapshot_service.sync_all(g.user)
        draft = runtime.user_store.load_draft(g.user)
        attachment_ids = draft.get("attachment_ids") if isinstance(draft.get("attachment_ids"), list) else []
        draft["attachments"] = runtime.upload_service.public(attachment_ids, owner_key())
    requested_default = settings.get("default_model") or runtime.model_service.default_id()
    runnable_ids = {str(item.get("id") or "") for item in status.get("runnable_models", [])}
    default_model = requested_default if requested_default in runnable_ids else runtime.model_service.default_id()
    from flask import current_app
    return jsonify({
        "user": public_user(),
        "guest_remaining": 0 if session.get("guest_used") else 1,
        "models": status["models"],
        "installed_models": status.get("installed_models", status["models"]),
        "runnable_models": status.get("runnable_models", []),
        "modes": status["modes"],
        "models_available": status["models_available"],
        "model_error": status["model_error"],
        "model_issues": status["model_issues"],
        "default_model": default_model,
        "settings": settings,
        "draft": draft,
        "csrf_token": csrf_token(),
        "message_limit": int(current_app.config["MAX_MESSAGE_LENGTH"]),
    })
