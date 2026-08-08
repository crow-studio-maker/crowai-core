from __future__ import annotations

import re

from flask import Blueprint, g, jsonify, request

from crowai.auth import require_csrf
from crowai.errors import ConflictError, ModelUnavailable, ValidationError
from crowai.request_context import owner_key
from crowai.runtime import get_runtime

upload_api_bp = Blueprint("upload_api", __name__, url_prefix="/api")
_REQUEST_KEY = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


@upload_api_bp.post("/uploads")
def upload():
    require_csrf()
    runtime = get_runtime()
    if not runtime.model_service.status()["models_available"]:
        raise ModelUnavailable("No runnable local CrowAI model is available.", {"models_available": False})
    model_id = runtime.model_service.validate_selection(str(request.form.get("model_id") or ""))
    files = request.files.getlist("files")
    request_key = str(request.headers.get("X-Idempotency-Key") or request.form.get("request_id") or "").strip()
    if request_key and not _REQUEST_KEY.fullmatch(request_key):
        raise ValidationError("The upload request identifier is invalid.")
    key = owner_key()
    operation = "upload"
    replay = runtime.request_ledger.completed(key, request_key, operation)
    if replay is not None:
        replay["reused"] = True
        return jsonify(replay)
    if not runtime.request_ledger.claim(key, request_key, operation):
        raise ConflictError("This upload request is already being processed.")
    try:
        items = runtime.upload_service.save(files=files, owner_key=key, model_id=model_id)
        response = {"attachments": items, "reused": False}
        runtime.request_ledger.complete(key, request_key, operation, response)
    except Exception:
        runtime.request_ledger.release(key, request_key, operation)
        raise
    runtime.snapshot_service.sync(g.user, uploads=True)
    return jsonify(response), 201
