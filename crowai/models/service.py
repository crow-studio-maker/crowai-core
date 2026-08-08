from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from crowai.errors import ModelExecutionError, ModelUnavailable
from crowai.search import search
from models import ModelError, ModelInputError, ModelRegistry

_LOG = logging.getLogger(__name__)
_PRIVATE_KEYS = {"local_path", "stored_path", "filesystem_path", "absolute_path"}


def sanitize_public_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        return None
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for raw_key, raw_value in list(value.items())[:200]:
            key = str(raw_key)[:120]
            lowered = key.casefold()
            if lowered in _PRIVATE_KEYS or lowered.endswith("_path"):
                continue
            if lowered == "path":
                path_value = str(raw_value or "").replace("\\", "/").strip()
                parts = [part for part in path_value.split("/") if part]
                is_absolute = path_value.startswith("/") or bool(re.match(r"^[A-Za-z]:/", path_value))
                if is_absolute or ".." in parts or not path_value:
                    continue
            output[key] = sanitize_public_value(raw_value, depth=depth + 1)
        return output
    if isinstance(value, (list, tuple)):
        return [sanitize_public_value(item, depth=depth + 1) for item in list(value)[:500]]
    if isinstance(value, str):
        return value[:500_000]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:1000]


def _json_compatible(value: Any) -> Any:
    sanitized = sanitize_public_value(value)
    try:
        json.dumps(sanitized, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ModelExecutionError("The selected model returned a non-serializable response.") from exc
    return sanitized


class ModelService:
    def __init__(self, registry: ModelRegistry, *, enable_web_search: bool = True) -> None:
        self.registry = registry
        self.enable_web_search = enable_web_search

    def list_models(self) -> list[dict[str, Any]]:
        return self.registry.list_models()

    def list_modes(self) -> list[dict[str, Any]]:
        return self.registry.list_modes()

    def status(self) -> dict[str, Any]:
        return self.registry.status()

    def default_id(self) -> str:
        return self.registry.default_id()

    def validate_selection(self, model_id: str) -> str:
        try:
            resolver = getattr(self.registry, "runnable_descriptor", None)
            descriptor = resolver(model_id) if callable(resolver) else self.registry.descriptor(model_id)
            return descriptor.id
        except ModelError as exc:
            raise ModelUnavailable(str(exc)) from exc

    def inspect_file(self, *, model_id: str, path: Path, original_name: str, media_type: str) -> dict[str, Any]:
        try:
            return sanitize_public_value(
                self.registry.inspect_file(model_id=model_id, path=path, original_name=original_name, media_type=media_type)
            )
        except ModelError as exc:
            raise ModelExecutionError("The selected model could not inspect the file.") from exc

    def execute(
        self,
        *,
        model_id: str,
        question: str,
        language: str,
        conversation: list[dict[str, str]],
        attachments: list[dict[str, Any]],
        snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        descriptor_id = self.validate_selection(model_id)
        # Attachment paths are trusted Core-owned inputs and are visible only to the
        # selected local model package for this invocation. They are stripped from
        # the plan immediately afterwards and can never reach the public result.
        trusted_attachments: list[dict[str, Any]] = []
        for item in attachments[:100]:
            if not isinstance(item, dict):
                continue
            trusted_attachments.append(dict(item))
        try:
            plan = self.registry.prepare(
                model_id=descriptor_id,
                question=question,
                language=language,
                conversation=conversation,
                attachments=trusted_attachments,
                snapshot=sanitize_public_value(snapshot),
            )
            plan = _json_compatible(plan)
            metadata = plan.get("metadata") if isinstance(plan.get("metadata"), dict) else {}
            wants_search = bool(metadata.get("web_access") or metadata.get("needs_current_information") or metadata.get("network"))
            package_managed_search = bool(metadata.get("package_managed_search"))
            sources = []
            warnings: list[str] = []
            if wants_search and package_managed_search:
                # Agent V1.0 owns its provider fallback/fetch lifecycle so Core does
                # not duplicate the same network work through generic search.
                if not self.enable_web_search:
                    metadata["network_disabled_by_core"] = True
            elif wants_search and self.enable_web_search:
                sources = search(plan.get("query_variations") or [], maximum=16)
                if not sources:
                    warnings.append("No usable web source was returned.")
            elif wants_search:
                warnings.append("Web search is disabled by Core configuration.")
            base_result = {
                "status": "complete" if not wants_search or sources else "partial",
                "success": True,
                "answer": "",
                "analysis": {},
                "sources": sources,
                "artifacts": [],
                "warnings": warnings,
                "meta": {"model": {"metadata": metadata}, "plan": plan},
                "metadata": metadata,
            }
            result = self.registry.finalize(
                model_id=descriptor_id,
                question=question,
                language=language,
                result=base_result,
            )
            # Preparation metadata can contain bounded memory and attachment excerpts.
            # It is private execution context, not part of the public response contract.
            if result.get("metadata") is metadata:
                result.pop("metadata", None)
            public_meta = result.get("meta")
            if isinstance(public_meta, dict):
                public_meta.pop("plan", None)
                model_meta = public_meta.get("model")
                if isinstance(model_meta, dict):
                    model_meta.pop("metadata", None)
                    if not model_meta:
                        public_meta.pop("model", None)
            result = _json_compatible(result)
            answer = result.get("answer")
            if not isinstance(answer, str):
                analysis = result.get("analysis") if isinstance(result.get("analysis"), dict) else {}
                answer = analysis.get("overview") or analysis.get("conclusion") or ""
            result["answer"] = str(answer or "No answer was produced.")[:500_000]
            return result
        except ModelUnavailable:
            raise
        except ModelInputError as exc:
            message = str(exc).strip() or "The request does not fit within the selected model context."
            return {
                "status": "error",
                "success": False,
                "answer": message[:500],
                "analysis": {},
                "sources": [],
                "artifacts": [],
                "warnings": ["The selected model rejected this request before generation."],
                "error": {"code": "MODEL_INPUT_INVALID", "message": message[:500]},
                "model_id": descriptor_id,
                "mode_id": self.registry.descriptor(descriptor_id).mode,
            }
        except ModelError as exc:
            _LOG.exception("Model execution failed for %s", descriptor_id)
            raise ModelExecutionError() from exc
        except ModelExecutionError:
            raise
        except Exception as exc:
            _LOG.exception("Unexpected model execution failure for %s", descriptor_id)
            raise ModelExecutionError() from exc

    def health(self) -> dict[str, Any]:
        status = self.status()
        model_health: list[dict[str, Any]] = []
        for model in status["models"]:
            try:
                model_health.append(self.registry.health_check(str(model["id"])))
            except Exception:
                model_health.append({"model_id": model["id"], "ok": False, "status": "unavailable"})
        return {**status, "packages": model_health}
