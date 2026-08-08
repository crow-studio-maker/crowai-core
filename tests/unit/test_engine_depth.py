from __future__ import annotations

import importlib
import json
import os
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from models.registry import ModelRegistry

ROOT = Path(__file__).resolve().parents[2]


def _engine_module(model_id: str):
    registry = ModelRegistry(ROOT / "models", development=True, strict_capabilities=True)
    package = registry._load(model_id)
    return importlib.import_module(f"{package.__name__}.engine")


def _engine_class(module: Any, model_id: str):
    if model_id == "chat/v1.0":
        return module.LocalChatEngine
    if model_id == "code/v1.0":
        return module.LocalCodeEngine
    return module.LocalAgentEngine


def _error_class(module: Any, model_id: str):
    return module.LocalAgentError if model_id == "agent/v1.0" else module.LocalModelError


@pytest.mark.parametrize("model_id", ["chat/v1.0", "code/v1.0", "agent/v1.0"])
def test_engine_generate_builds_bounded_payload_without_starting_real_runtime(
    model_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _engine_module(model_id)
    engine = _engine_class(module, model_id)()
    captured: dict[str, Any] = {}

    monkeypatch.setattr(engine, "start", lambda: None)
    monkeypatch.setattr(engine, "_read_system_prompt", lambda: "system-policy")
    if hasattr(engine, "_cancel_idle_timer"):
        monkeypatch.setattr(engine, "_cancel_idle_timer", lambda: None)
    if hasattr(engine, "_schedule_idle_shutdown"):
        monkeypatch.setattr(engine, "_schedule_idle_shutdown", lambda: None)

    def fake_request(endpoint: str, *, payload: dict[str, Any] | None = None, timeout: float):
        captured.update(endpoint=endpoint, payload=payload, timeout=timeout)
        return {"choices": [{"message": {"content": "  generated answer  "}}]}

    monkeypatch.setattr(engine, "_request_json", fake_request)
    messages: list[Any] = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "previous"},
        {"role": "tool", "content": "ignored"},
        {"role": "user", "content": "   "},
        "invalid",
    ]
    if model_id == "agent/v1.0":
        messages.append({"role": "user", "content": [{"type": "text", "text": "vision"}]})
        answer = engine.generate(messages, maximum_tokens=99999, temperature=0.2, json_mode=True)
        assert captured["payload"]["response_format"] == {"type": "json_object"}
        assert captured["payload"]["messages"][-1]["content"][0]["text"] == "vision"
        assert captured["payload"]["temperature"] == 0.2
    elif model_id == "code/v1.0":
        answer = engine.generate(messages, maximum_tokens=99999, temperature=0.2, json_mode=True)
        assert captured["payload"]["response_format"] == {"type": "json_object"}
        assert captured["payload"]["temperature"] == 0.2
    else:
        answer = engine.generate(messages, maximum_tokens=99999)

    assert answer == "generated answer"
    assert captured["endpoint"] == "/v1/chat/completions"
    assert captured["payload"]["messages"][0] == {"role": "system", "content": "system-policy"}
    assert captured["payload"]["messages"][1:3] == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "previous"},
    ]
    configured_max = int(engine.config.get("absolute_max_output_tokens", engine.config.get("max_output_tokens", 1024)))
    assert 32 <= captured["payload"]["max_tokens"] <= configured_max
    assert captured["timeout"] >= 30


@pytest.mark.parametrize("model_id", ["chat/v1.0", "code/v1.0", "agent/v1.0"])
def test_engine_generate_rejects_missing_or_empty_answers(
    model_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _engine_module(model_id)
    error = _error_class(module, model_id)
    engine = _engine_class(module, model_id)()
    monkeypatch.setattr(engine, "start", lambda: None)
    monkeypatch.setattr(engine, "_read_system_prompt", lambda: "system")
    if hasattr(engine, "_cancel_idle_timer"):
        monkeypatch.setattr(engine, "_cancel_idle_timer", lambda: None)
    if hasattr(engine, "_schedule_idle_shutdown"):
        monkeypatch.setattr(engine, "_schedule_idle_shutdown", lambda: None)

    monkeypatch.setattr(engine, "_request_json", lambda *args, **kwargs: {})
    with pytest.raises(error, match="answer"):
        engine.generate([{"role": "user", "content": "x"}])

    monkeypatch.setattr(
        engine,
        "_request_json",
        lambda *args, **kwargs: {"choices": [{"message": {"content": "   "}}]},
    )
    with pytest.raises(error, match="empty|generated"):
        engine.generate([{"role": "user", "content": "x"}])


class _Response:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.payload


@pytest.mark.parametrize("model_id", ["chat/v1.0", "code/v1.0", "agent/v1.0"])
def test_engine_http_json_contract_handles_success_invalid_json_and_network_error(
    model_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _engine_module(model_id)
    error = _error_class(module, model_id)
    engine = _engine_class(module, model_id)()

    monkeypatch.setattr(module.urllib.request, "urlopen", lambda request, timeout: _Response(b'{"ok": true}'))
    assert engine._request_json("/health", payload={"x": 1}, timeout=2) == {"ok": True}

    monkeypatch.setattr(module.urllib.request, "urlopen", lambda request, timeout: _Response(b"not-json"))
    with pytest.raises(error, match="invalid JSON"):
        engine._request_json("/health", timeout=2)

    def broken(*args, **kwargs):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(module.urllib.request, "urlopen", broken)
    with pytest.raises(error, match="could not be reached"):
        engine._request_json("/health", timeout=2)


@pytest.mark.parametrize("model_id", ["chat/v1.0", "code/v1.0", "agent/v1.0"])
def test_engine_readiness_requires_owned_live_process_and_matching_alias(
    model_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _engine_module(model_id)
    engine = _engine_class(module, model_id)()
    assert engine.is_ready() is False

    engine._process = SimpleNamespace(poll=lambda: None)
    engine._owns_process = True
    alias = str(engine.config.get("model_alias", ""))
    monkeypatch.setattr(engine, "_request_json", lambda *args, **kwargs: {"data": [{"id": "other"}]})
    assert engine.is_ready() is (not alias)
    monkeypatch.setattr(engine, "_request_json", lambda *args, **kwargs: {"data": [{"model": alias}]})
    assert engine.is_ready() is True
    monkeypatch.setattr(engine, "_request_json", lambda *args, **kwargs: {"data": "bad"})
    assert engine.is_ready() is False

    error = _error_class(module, model_id)
    monkeypatch.setattr(engine, "_request_json", lambda *args, **kwargs: (_ for _ in ()).throw(error("no")))
    assert engine.is_ready() is False
    # Avoid leaving the synthetic process attached to the atexit-registered engine.
    engine._process = None
    engine._owns_process = False


@pytest.mark.parametrize("model_id", ["chat/v1.0", "code/v1.0", "agent/v1.0"])
def test_engine_file_validation_and_command_are_package_local(
    model_id: str,
    tmp_path: Path,
) -> None:
    module = _engine_module(model_id)
    error = _error_class(module, model_id)
    engine = _engine_class(module, model_id)()

    # Missing real private runtime/model files must fail before a process is started.
    with pytest.raises(error, match="missing"):
        engine._validate_files()

    runtime = tmp_path / "runtime" / "llama-server"
    model = tmp_path / "model" / "weights.gguf"
    prompt = tmp_path / "prompts" / "system.txt"
    runtime.parent.mkdir(parents=True)
    model.parent.mkdir(parents=True)
    prompt.parent.mkdir(parents=True)
    runtime.write_bytes(b"runtime")
    if os.name != "nt":
        runtime.chmod(0o755)
    model.write_bytes(b"GGUFmodel")
    prompt.write_text("system", encoding="utf-8")
    engine.runtime_path = runtime
    engine.model_path = model
    if model_id == "agent/v1.0":
        mmproj = model.parent / "mmproj.gguf"
        mmproj.write_bytes(b"GGUFmmproj")
        engine.mmproj_path = mmproj
        engine.system_prompt_path = prompt
    else:
        engine.prompt_path = prompt

    engine._validate_files()
    command = engine._build_command()
    assert command[0] == str(runtime)
    assert str(model) in command
    assert "--host" in command and engine.host in command
    assert "--port" in command and str(engine.port) in command
    assert "--alias" in command
    if model_id == "agent/v1.0":
        assert "--mmproj" in command and str(engine.mmproj_path) in command


def test_agent_vision_collects_only_safe_image_inputs_and_parses_model_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ModelRegistry(ROOT / "models", development=True, strict_capabilities=True)
    package = registry._load("agent/v1.0")
    vision = importlib.import_module(f"{package.__name__}.vision")

    image = tmp_path / "photo.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nsmall")
    attachments = [
        {"name": "photo.png", "media_type": "image/png", "_internal_path": str(image)},
        {"name": "doc.pdf", "derived_images": [{"name": "page 1", "data_url": "data:image/png;base64,QUJD"}]},
        {"name": "ignore.txt", "_internal_path": str(tmp_path / "ignore.txt")},
    ]
    images = vision.collect_visual_inputs(attachments, maximum_images=2)
    assert [item["name"] for item in images] == ["photo.png", "page 1"]
    assert images[0]["url"].startswith("data:image/png;base64,")

    captured: dict[str, Any] = {}

    def fake_generate(messages, **kwargs):
        captured["messages"] = messages
        captured.update(kwargs)
        return json.dumps({"description": "a photo", "search_queries": ["q"]})

    monkeypatch.setattr(vision, "generate_response", fake_generate)
    result = vision.analyze_images(question="what is this?", attachments=attachments, prompt="inspect", maximum_tokens=200)
    assert result["description"] == "a photo"
    assert result["images"] == ["photo.png", "page 1"]
    assert captured["json_mode"] is True
    assert captured["messages"][0]["content"][2]["type"] == "image_url"

    monkeypatch.setattr(vision, "generate_response", lambda *args, **kwargs: "plain description")
    fallback = vision.analyze_images(question="q", attachments=attachments[:1], prompt="inspect", maximum_tokens=100)
    assert fallback["description"] == "plain description"
    assert fallback["search_queries"] == []


def test_agent_vision_rejects_public_path_and_unsupported_type(tmp_path: Path) -> None:
    registry = ModelRegistry(ROOT / "models", development=True, strict_capabilities=True)
    package = registry._load("agent/v1.0")
    vision = importlib.import_module(f"{package.__name__}.vision")
    assert vision.attachment_image_url({"path": "/etc/passwd", "media_type": "image/png"}) is None

    file = tmp_path / "x.txt"
    file.write_text("x", encoding="utf-8")
    with pytest.raises(vision.LocalAgentError, match="Unsupported"):
        vision._data_url_from_path(file, "text/plain")
    assert vision.analyze_images(question="q", attachments=[], prompt="p", maximum_tokens=10) == {}

class _FakeProcess:
    def __init__(self) -> None:
        self.pid = 4242
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


@pytest.mark.parametrize("model_id", ["chat/v1.0", "code/v1.0", "agent/v1.0"])
def test_engine_start_and_stop_manage_only_package_owned_fake_process(
    model_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _engine_module(model_id)
    engine = _engine_class(module, model_id)()
    runtime = tmp_path / "runtime" / "llama-server"
    model = tmp_path / "model" / "weights.gguf"
    prompt = tmp_path / "prompts" / "system.txt"
    runtime.parent.mkdir(parents=True)
    model.parent.mkdir(parents=True)
    prompt.parent.mkdir(parents=True)
    runtime.write_bytes(b"runtime")
    if os.name != "nt":
        runtime.chmod(0o755)
    model.write_bytes(b"GGUFmodel")
    prompt.write_text("system", encoding="utf-8")
    engine.runtime_path = runtime
    engine.model_path = model
    if model_id == "agent/v1.0":
        mmproj = model.parent / "mmproj.gguf"
        mmproj.write_bytes(b"GGUFprojector")
        engine.mmproj_path = mmproj
        engine.system_prompt_path = prompt
    else:
        engine.prompt_path = prompt

    monkeypatch.setattr(module, "BASE_DIR", tmp_path)
    fake = _FakeProcess()
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def fake_popen(*args, **kwargs):
        calls.append((args, kwargs))
        return fake

    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(engine, "_wait_until_ready", lambda: None)
    if model_id in {"code/v1.0", "agent/v1.0"}:
        monkeypatch.setattr(module.os, "getpgid", lambda pid: pid)
        monkeypatch.setattr(module.os, "killpg", lambda pgid, sig: setattr(fake, "returncode", 0))

    engine.start()
    assert engine._owns_process is True
    assert len(calls) == 1
    assert calls[0][1]["cwd"] == str(runtime.parent)
    monkeypatch.setattr(engine, "is_ready", lambda: True)
    engine.start()
    assert len(calls) == 1
    engine.stop()
    assert engine._process is None
    assert engine._owns_process is False
    assert engine._log_handle is None


@pytest.mark.parametrize("model_id", ["chat/v1.0", "code/v1.0", "agent/v1.0"])
def test_engine_wait_ready_success_stopped_and_timeout_paths(
    model_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _engine_module(model_id)
    engine = _engine_class(module, model_id)()
    error = _error_class(module, model_id)

    live = _FakeProcess()
    engine._process = live
    engine._owns_process = True
    monkeypatch.setattr(engine, "is_ready", lambda: True)
    engine._wait_until_ready()

    stopped = _FakeProcess()
    stopped.returncode = 1
    engine._process = stopped
    times = iter([0.0, 1.0])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(engine, "is_ready", lambda: False)
    with pytest.raises(error, match="stopped"):
        engine._wait_until_ready()

    engine._process = live
    times = iter([0.0, 999.0])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(times))
    with pytest.raises(error, match="ready|not become ready"):
        engine._wait_until_ready()
    engine._process = None
    engine._owns_process = False


@pytest.mark.parametrize("model_id", ["chat/v1.0", "code/v1.0", "agent/v1.0"])
def test_engine_prompt_and_http_error_paths(
    model_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io

    module = _engine_module(model_id)
    engine = _engine_class(module, model_id)()
    error = _error_class(module, model_id)
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("  system text  ", encoding="utf-8")
    if model_id == "agent/v1.0":
        engine.system_prompt_path = prompt
    else:
        engine.prompt_path = prompt
    assert engine._read_system_prompt() == "system text"
    prompt.write_text("   ", encoding="utf-8")
    with pytest.raises(error, match="empty"):
        engine._read_system_prompt()

    http_error = urllib.error.HTTPError(
        url="http://127.0.0.1/test",
        code=500,
        msg="bad",
        hdrs=None,
        fp=io.BytesIO(b"backend detail"),
    )
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(http_error))
    with pytest.raises(error, match="500"):
        engine._request_json("/test", timeout=1)
