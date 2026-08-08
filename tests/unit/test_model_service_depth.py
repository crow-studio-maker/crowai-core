from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from crowai.errors import ModelExecutionError, ModelUnavailable
from crowai.models import service as service_module
from crowai.models.service import ModelService, sanitize_public_value
from models import ModelError, ModelInputError


class RegistryStub:
    def __init__(self) -> None:
        self.prepared = None
        self.finalized = None
        self.raise_prepare = None
        self.health_raises = False

    def runnable_descriptor(self, model_id):
        if model_id == "bad":
            raise ModelError("not runnable")
        return SimpleNamespace(id=model_id or "chat/v1.0", mode=(model_id or "chat/v1.0").split("/")[0])

    def descriptor(self, model_id):
        return SimpleNamespace(id=model_id, mode=model_id.split("/")[0])

    def list_models(self):
        return [{"id": "chat/v1.0"}]

    def list_modes(self):
        return [{"id": "chat"}]

    def status(self):
        return {"models": [{"id": "chat/v1.0"}], "models_available": True}

    def default_id(self):
        return "chat/v1.0"

    def inspect_file(self, **kwargs):
        if kwargs["model_id"] == "explode":
            raise ModelError("bad")
        return {"path": "/private/file", "name": kwargs["original_name"], "nested": {"stored_path": "/x", "ok": 1}}

    def prepare(self, **kwargs):
        self.prepared = kwargs
        if self.raise_prepare is not None:
            raise self.raise_prepare
        return {
            "request_question": kwargs["question"],
            "query_variations": [{"query": "q"}],
            "metadata": {"web_access": True},
        }

    def finalize(self, **kwargs):
        self.finalized = kwargs
        result = kwargs["result"]
        result["analysis"] = {"overview": "answer from analysis"}
        result["answer"] = None
        return result

    def health_check(self, model_id):
        if self.health_raises:
            raise RuntimeError("health failed")
        return {"model_id": model_id, "ok": True, "status": "runnable"}


def test_public_sanitizer_removes_private_paths_and_bounds_depth() -> None:
    value = {
        "local_path": "/secret",
        "path": "/absolute/secret",
        "safe": {"path": "relative/file.txt", "filesystem_path": "C:/secret", "items": [1, object()]},
    }
    cleaned = sanitize_public_value(value)
    assert "local_path" not in cleaned and "path" not in cleaned
    assert cleaned["safe"]["path"] == "relative/file.txt"
    assert "filesystem_path" not in cleaned["safe"]
    assert cleaned["safe"]["items"][0] == 1
    deep = current = {}
    for _ in range(11):
        nxt = {}
        current["x"] = nxt
        current = nxt
    assert sanitize_public_value(deep)["x"]["x"]["x"]["x"]["x"]["x"]["x"]["x"]["x"] is None


def test_model_service_delegates_basic_registry_queries_and_selection() -> None:
    registry = RegistryStub()
    service = ModelService(registry)
    assert service.list_models() == [{"id": "chat/v1.0"}]
    assert service.list_modes() == [{"id": "chat"}]
    assert service.status()["models_available"] is True
    assert service.default_id() == "chat/v1.0"
    assert service.validate_selection("chat/v1.0") == "chat/v1.0"
    with pytest.raises(ModelUnavailable, match="not runnable"):
        service.validate_selection("bad")


def test_model_service_inspection_sanitizes_paths_and_wraps_errors(tmp_path: Path) -> None:
    registry = RegistryStub()
    service = ModelService(registry)
    result = service.inspect_file(model_id="chat/v1.0", path=tmp_path / "x", original_name="x.txt", media_type="text/plain")
    assert "path" not in result
    assert "stored_path" not in result["nested"]
    assert result["name"] == "x.txt"
    with pytest.raises(ModelExecutionError):
        service.inspect_file(model_id="explode", path=tmp_path / "x", original_name="x", media_type="x")


def test_model_service_execute_searches_and_strips_private_plan_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = RegistryStub()
    service = ModelService(registry, enable_web_search=True)
    monkeypatch.setattr(service_module, "search", lambda variations, maximum: [{"title": "source", "url": "https://example.com"}])
    result = service.execute(
        model_id="chat/v1.0",
        question="current info",
        language="en",
        conversation=[{"role": "user", "content": "old"}],
        attachments=[{"name": "x", "_internal_path": "/private"}, "bad"],
        snapshot={"summary": "memory", "absolute_path": "/private"},
    )
    assert result["answer"] == "answer from analysis"
    assert result["status"] == "complete"
    assert result["sources"][0]["title"] == "source"
    assert "metadata" not in result
    assert "plan" not in result.get("meta", {})
    assert registry.prepared["snapshot"] == {"summary": "memory"}
    assert len(registry.prepared["attachments"]) == 1


def test_model_service_execute_package_managed_and_core_disabled_search_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = RegistryStub()
    service = ModelService(registry, enable_web_search=False)

    def package_prepare(**kwargs):
        return {"metadata": {"needs_current_information": True, "package_managed_search": True}, "query_variations": []}

    registry.prepare = package_prepare
    result = service.execute(model_id="agent/v1.0", question="q", language="en", conversation=[], attachments=[], snapshot={})
    assert result["status"] == "partial"
    assert "metadata" not in result
    assert "model" not in result.get("meta", {})

    registry2 = RegistryStub()
    service2 = ModelService(registry2, enable_web_search=False)
    result2 = service2.execute(model_id="chat/v1.0", question="q", language="en", conversation=[], attachments=[], snapshot={})
    assert any("disabled" in item for item in result2["warnings"])


def test_model_service_execute_maps_model_input_and_other_failures() -> None:
    registry = RegistryStub()
    service = ModelService(registry)
    registry.raise_prepare = ModelInputError("too large")
    invalid = service.execute(model_id="code/v1.0", question="q", language="en", conversation=[], attachments=[], snapshot={})
    assert invalid["success"] is False
    assert invalid["error"]["code"] == "MODEL_INPUT_INVALID"
    assert invalid["mode_id"] == "code"

    registry.raise_prepare = ModelError("backend")
    with pytest.raises(ModelExecutionError):
        service.execute(model_id="code/v1.0", question="q", language="en", conversation=[], attachments=[], snapshot={})

    registry.raise_prepare = RuntimeError("unexpected")
    with pytest.raises(ModelExecutionError):
        service.execute(model_id="code/v1.0", question="q", language="en", conversation=[], attachments=[], snapshot={})


def test_model_service_health_reports_callback_failures_without_crashing() -> None:
    registry = RegistryStub()
    service = ModelService(registry)
    assert service.health()["packages"][0]["status"] == "runnable"
    registry.health_raises = True
    health = service.health()
    assert health["packages"] == [{"model_id": "chat/v1.0", "ok": False, "status": "unavailable"}]
