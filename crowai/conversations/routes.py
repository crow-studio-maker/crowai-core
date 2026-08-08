from __future__ import annotations

from flask import Blueprint, current_app, g, jsonify, request, session

from crowai.auth import require_csrf
from crowai.conversations.schemas import AskRequest, CreateConversationRequest, RenameConversationRequest
from crowai.errors import CoreError, ModelUnavailable
from crowai.request_context import owner_key
from crowai.runtime import get_runtime

conversation_api_bp = Blueprint("conversation_api", __name__, url_prefix="/api")


@conversation_api_bp.get("/conversations")
def conversations():
    return jsonify({"conversations": get_runtime().conversation_service.list(owner_key())})


@conversation_api_bp.post("/conversations")
def create_conversation():
    require_csrf()
    runtime = get_runtime()
    if not runtime.model_service.status()["models_available"]:
        raise ModelUnavailable("No runnable local CrowAI model is available.", {"models_available": False})
    parsed = CreateConversationRequest.parse(request.get_json(silent=True))
    result = runtime.conversation_service.create(owner_key(), parsed)
    runtime.snapshot_service.sync(g.user, conversations=True)
    return jsonify(result), (200 if result["reused"] else 201)


@conversation_api_bp.get("/conversations/<conversation_id>")
def get_conversation(conversation_id: str):
    return jsonify(get_runtime().conversation_service.get(conversation_id, owner_key()))


@conversation_api_bp.patch("/conversations/<conversation_id>")
def rename_conversation(conversation_id: str):
    require_csrf()
    parsed = RenameConversationRequest.parse(request.get_json(silent=True))
    runtime = get_runtime()
    conversation = runtime.conversation_service.rename(conversation_id, owner_key(), parsed)
    runtime.snapshot_service.sync(g.user, conversations=True)
    return jsonify({"ok": True, "conversation": conversation})


@conversation_api_bp.delete("/conversations/<conversation_id>")
def delete_conversation(conversation_id: str):
    require_csrf()
    runtime = get_runtime()
    runtime.conversation_service.delete(conversation_id, owner_key())
    runtime.snapshot_service.sync(g.user, conversations=True, uploads=True)
    return jsonify({"ok": True})


@conversation_api_bp.post("/conversations/<conversation_id>/ask")
def ask(conversation_id: str):
    require_csrf()
    if not g.user and session.get("guest_used"):
        raise CoreError("The guest question has been used. Sign in to continue.", "GUEST_LIMIT_REACHED", 403)
    parsed = AskRequest.parse(
        request.get_json(silent=True),
        maximum_message_length=int(current_app.config["MAX_MESSAGE_LENGTH"]),
        maximum_attachments=int(current_app.config["MAX_UPLOAD_FILES"]),
    )
    runtime = get_runtime()
    response = runtime.conversation_service.ask(
        conversation_id,
        owner_key(),
        parsed,
        request_id=str(g.request_id),
    )
    if not g.user:
        session["guest_used"] = True
    runtime.snapshot_service.sync(g.user, conversations=True, uploads=True)
    return jsonify(response)
