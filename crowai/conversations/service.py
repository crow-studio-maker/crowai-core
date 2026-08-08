from __future__ import annotations

import logging
import threading
import uuid
from pathlib import Path
from typing import Any

from crowai.conversations.repository import ConversationRepository
from crowai.conversations.schemas import AskRequest, CreateConversationRequest, RenameConversationRequest
from crowai.errors import ConflictError, ModelExecutionError, ModelUnavailable, ResourceNotFound
from crowai.models.service import ModelService
from crowai.uploads.service import UploadService

_LOG = logging.getLogger(__name__)


class _LedgerHeartbeat:
    """Keep one persistent conversation-operation lease live during long inference."""

    def __init__(
        self,
        repository: ConversationRepository,
        *,
        owner_key: str,
        request_key: str,
        operation: str,
        lease_seconds: int = 600,
        interval_seconds: int = 60,
    ) -> None:
        self.repository = repository
        self.owner_key = owner_key
        self.request_key = request_key
        self.operation = operation
        self.lease_seconds = max(60, int(lease_seconds))
        self.interval_seconds = max(5, min(int(interval_seconds), self.lease_seconds // 2))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="crowai-request-lease",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                alive = self.repository.renew_ledger(
                    owner_key=self.owner_key,
                    request_key=self.request_key,
                    operation=self.operation,
                    lease_seconds=self.lease_seconds,
                )
                if not alive:
                    return
            except Exception:
                # A transient SQLite failure must not crash the model request. The
                # original lease remains valid and the next interval retries.
                _LOG.exception("Conversation processing lease heartbeat failed")

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
        self._thread = None


class _ActiveGeneration:
    """In-process handle for one conversation turn that may need cancellation."""

    def __init__(self, model_id: str) -> None:
        self.model_id = model_id
        self.cancel_requested = threading.Event()
        self.done = threading.Event()


class ConversationService:
    _CANCEL_WAIT_SECONDS = 20.0

    def __init__(self, repository: ConversationRepository, upload_service: UploadService, model_service: ModelService) -> None:
        self.repository = repository
        self.upload_service = upload_service
        self.model_service = model_service
        self._generation_lock = threading.RLock()
        self._active_generations: dict[str, _ActiveGeneration] = {}

    def _register_generation(self, conversation_id: str, model_id: str) -> _ActiveGeneration:
        with self._generation_lock:
            existing = self._active_generations.get(conversation_id)
            if existing is not None and not existing.done.is_set():
                raise ConflictError(
                    "This conversation is still generating a response. Wait for it to finish before sending another message.",
                    {"conversation_processing": True, "conversation_id": conversation_id},
                )
            active = _ActiveGeneration(model_id)
            self._active_generations[conversation_id] = active
            return active

    def _active_generation(self, conversation_id: str) -> _ActiveGeneration | None:
        with self._generation_lock:
            active = self._active_generations.get(conversation_id)
            if active is None or active.done.is_set():
                return None
            return active

    def _finish_generation(self, conversation_id: str, active: _ActiveGeneration) -> None:
        active.done.set()
        with self._generation_lock:
            if self._active_generations.get(conversation_id) is active:
                self._active_generations.pop(conversation_id, None)

    def list(self, owner_key: str) -> list[dict[str, Any]]:
        return self.repository.list_for_owner(owner_key)

    def get(self, conversation_id: str, owner_key: str) -> dict[str, Any]:
        conversation = self.repository.get_for_owner(conversation_id, owner_key)
        if not conversation:
            raise ResourceNotFound()
        available = any(
            item["id"] == conversation["model_id"] and item.get("runnable") is True
            for item in self.model_service.list_models()
        )
        processing = self.repository.processing_operation(
            owner_key=owner_key, operation=f"ask:{conversation_id}"
        )
        return {
            "conversation": conversation,
            "messages": self.repository.messages(conversation_id),
            "model_available": available,
            "processing": processing,
        }

    def create(self, owner_key: str, request: CreateConversationRequest) -> dict[str, Any]:
        selected = self.model_service.validate_selection(request.model_id)
        conversation_id, reused = self.repository.create(
            conversation_id=uuid.uuid4().hex,
            owner_key=owner_key,
            model_id=selected,
            request_key=request.request_key,
        )
        return {"id": conversation_id, "model_id": selected, "reused": reused}

    def rename(self, conversation_id: str, owner_key: str, request: RenameConversationRequest) -> dict[str, Any]:
        if not self.repository.rename(conversation_id, owner_key, request.title):
            raise ResourceNotFound()
        return {"id": conversation_id, "title": request.title}

    def delete(self, conversation_id: str, owner_key: str) -> None:
        conversation = self.repository.get_for_owner(conversation_id, owner_key)
        if not conversation:
            raise ResourceNotFound()
        bound_model_id = str(conversation.get("model_id") or "")
        operation = f"ask:{conversation_id}"
        active = self._active_generation(conversation_id)

        if active is not None:
            # Mark the Core turn cancelled before touching the model process. This
            # prevents a response that races with process termination from being
            # persisted while deletion is waiting for the request thread to unwind.
            active.cancel_requested.set()
            self.model_service.registry.cancel_conversation(
                conversation_id, model_id=bound_model_id
            )
            if not active.done.wait(self._CANCEL_WAIT_SECONDS):
                raise ConflictError(
                    "The active model request did not stop in time. The conversation was not deleted; retry after the backend finishes shutting down.",
                    {"conversation_processing": True, "conversation_id": conversation_id},
                )

        self.repository.release_operation(owner_key=owner_key, operation=operation)

        deleted, paths = self.repository.delete(conversation_id, owner_key)
        if not deleted:
            raise ResourceNotFound()
        for path in paths:
            Path(path).unlink(missing_ok=True)
        # SQLite deletion is authoritative. Model-owned mutable state lives under
        # Core's private instance/model_state tree; package cleanup is best-effort for
        # the exact trusted package that owned this conversation, including after a
        # process restart where that package has not yet been imported.
        self.model_service.registry.cleanup_conversation(conversation_id, model_id=bound_model_id)

    def ask(self, conversation_id: str, owner_key: str, request: AskRequest, *, request_id: str) -> dict[str, Any]:
        conversation = self.repository.get_for_owner(conversation_id, owner_key)
        if not conversation:
            raise ResourceNotFound()
        bound_model_id = str(conversation["model_id"])
        if request.model_id != bound_model_id:
            raise ConflictError("This conversation is bound to its original model. Start a new draft to use another model.")
        self.model_service.validate_selection(bound_model_id)

        operation = f"ask:{conversation_id}"
        replay = self.repository.ledger_response(
            owner_key=owner_key, request_key=request.request_key, operation=operation
        )
        if replay is not None:
            replay["reused"] = True
            return replay

        # A browser refresh generates a fresh request id, so idempotency alone is
        # not enough to serialize turns. The request ledger also acts as an
        # operation-wide lease: exactly one active ask is allowed per conversation.
        ledger_key = request.request_key or request_id
        if not self.repository.claim_ledger(
            owner_key=owner_key,
            request_key=ledger_key,
            operation=operation,
            lease_seconds=600,
        ):
            raise ConflictError(
                "This conversation is still generating a response. Wait for it to finish before sending another message.",
                {"conversation_processing": True, "conversation_id": conversation_id},
            )

        try:
            active = self._register_generation(conversation_id, bound_model_id)
        except Exception:
            self.repository.release_ledger(
                owner_key=owner_key, request_key=ledger_key, operation=operation
            )
            raise

        heartbeat = _LedgerHeartbeat(
            self.repository,
            owner_key=owner_key,
            request_key=ledger_key,
            operation=operation,
            lease_seconds=600,
            interval_seconds=60,
        )
        heartbeat.start()
        try:
            if active.cancel_requested.is_set():
                raise ResourceNotFound("The conversation was cancelled while the model was generating.")
            attachments = self.upload_service.for_model(request.attachment_ids, owner_key)
            snapshot = self.repository.memory_snapshot(conversation_id, owner_key)
            history = snapshot["recent_messages"]
            request_snapshot = dict(snapshot)
            request_snapshot["request_options"] = {"execution": dict(request.execution)}
            user_message_id = self.repository.add_user_message(
                conversation_id=conversation_id,
                content=request.question,
                attachment_ids=request.attachment_ids,
            )
            try:
                result = self.model_service.execute(
                    model_id=bound_model_id,
                    question=request.question,
                    language=request.language,
                    conversation=history,
                    attachments=attachments,
                    snapshot=request_snapshot,
                )
            except (ModelExecutionError, ModelUnavailable):
                if active.cancel_requested.is_set():
                    raise ResourceNotFound("The conversation was cancelled while the model was generating.")
                _LOG.exception("Safe model failure [%s]", request_id)
                result = {
                    "status": "error",
                    "success": False,
                    "answer": "The selected model could not complete the request.",
                    "analysis": {},
                    "sources": [],
                    "artifacts": [],
                    "warnings": ["The model request failed. Review server logs using the request ID."],
                    "model_id": bound_model_id,
                    "request_id": request_id,
                }

            # Deletion marks this turn cancelled before killing the package-owned
            # backend. Even if the HTTP/model call races and returns a final payload,
            # never persist it while deletion is waiting for this thread to unwind.
            if active.cancel_requested.is_set():
                raise ResourceNotFound("The conversation was cancelled while the model was generating.")

            # The conversation can also disappear through an external cleanup path.
            # Never recreate state after deletion.
            if not self.repository.get_for_owner(conversation_id, owner_key):
                raise ResourceNotFound("The conversation was deleted while the model was generating.")

            memory_update = result.pop("memory_update", {}) if isinstance(result.get("memory_update"), dict) else {}
            answer = str(result.get("answer") or "No answer was produced.")
            self.repository.add_assistant_message(
                conversation_id=conversation_id,
                answer=answer,
                result=result,
                title_source=request.question,
            )
            try:
                self.repository.update_memory(
                    conversation_id=conversation_id, owner_key=owner_key, user_message_id=user_message_id,
                    question=request.question, result=result, memory_update=memory_update,
                )
            except Exception:
                _LOG.exception("Conversation memory update failed [%s]", request_id)
            response = {"result": result, "answer": answer, "reused": False}
            self.repository.complete_ledger(
                owner_key=owner_key,
                request_key=ledger_key,
                operation=operation,
                response=response,
            )
            return response
        except Exception:
            self.repository.release_ledger(
                owner_key=owner_key, request_key=ledger_key, operation=operation
            )
            raise
        finally:
            heartbeat.stop()
            self._finish_generation(conversation_id, active)
