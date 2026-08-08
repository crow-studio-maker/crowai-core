from __future__ import annotations

import importlib
import importlib.util
import sys
import types
import os
import threading
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from crowai.conversations.repository import ConversationRepository
from crowai.storage.database import Database
from models import ModelRegistry
from models.runtime_state import configure_state_root, model_state_dir, open_private_log, write_private_text

ROOT = Path(__file__).resolve().parents[2]


def _engine_module(model_id: str) -> ModuleType:
    registry = ModelRegistry(ROOT / "models", development=True, strict_capabilities=True)
    package = registry._load(model_id)
    return importlib.import_module(f"{package.__name__}.engine")


def _engine_class(module: ModuleType, model_id: str):
    if model_id == "chat/v1.0":
        return module.LocalChatEngine
    if model_id == "code/v1.0":
        return module.LocalCodeEngine
    return module.LocalAgentEngine


class _LiveProcess:
    def __init__(self) -> None:
        self.pid = 424242
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
def test_cancel_does_not_wait_for_active_request_lock(
    model_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _engine_module(model_id)
    engine = _engine_class(module, model_id)()
    process = _LiveProcess()
    engine._process = process
    engine._owns_process = True

    if os.name == "posix":
        monkeypatch.setattr(module.os, "getpgid", lambda pid: pid)
        monkeypatch.setattr(module.os, "killpg", lambda pgid, sig: setattr(process, "returncode", 0))
    else:
        monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: setattr(process, "returncode", 0))

    engine._request_lock.acquire()
    finished = threading.Event()
    worker = threading.Thread(target=lambda: (engine.cancel(), finished.set()), daemon=True)
    started = time.monotonic()
    worker.start()
    try:
        assert finished.wait(0.75), "cancel() blocked behind the active inference request lock"
        assert time.monotonic() - started < 0.75
        assert engine._process is None
        assert engine._cancelled.is_set()
    finally:
        engine._request_lock.release()
        worker.join(timeout=1)


@pytest.mark.parametrize("model_id", ["chat/v1.0", "code/v1.0", "agent/v1.0"])
def test_model_runtime_logs_are_outside_immutable_package_tree(
    model_id: str,
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "instance" / "model_state"
    configure_state_root(state_root)
    try:
        module = _engine_module(model_id)
        engine = _engine_class(module, model_id)()
        package = Path(module.__file__).resolve().parent
        assert engine.state_dir == state_root.resolve() / model_id
        assert package not in engine.state_dir.parents
        handle = open_private_log(engine.state_dir / "engine.log")
        handle.write("test\n")
        handle.close()
        assert (state_root / model_id / "engine.log").is_file()
        assert not (package / "engine.log").exists()
    finally:
        configure_state_root(None)


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode assertion")
def test_model_runtime_state_is_private_and_package_tree_need_not_be_writable(tmp_path: Path) -> None:
    state_root = tmp_path / "instance" / "model_state"
    package = tmp_path / "models" / "code" / "v1.0"
    package.mkdir(parents=True)
    package.chmod(0o555)
    configure_state_root(state_root)
    try:
        state = model_state_dir(package, "code", "v1.0", create=True)
        debug = state / "debug" / "capture.txt"
        write_private_text(debug, "safe")
        assert (state_root / "code" / "v1.0" / "debug" / "capture.txt").read_text(encoding="utf-8") == "safe"
        assert (stat_mode(state_root)) == 0o700
        assert stat_mode(state_root / "code") == 0o700
        assert stat_mode(state_root / "code" / "v1.0" / "debug") == 0o700
        assert stat_mode(debug) == 0o600
        assert stat_mode(package) == 0o555
    finally:
        package.chmod(0o755)
        configure_state_root(None)


def stat_mode(path: Path) -> int:
    import stat
    return stat.S_IMODE(path.stat().st_mode)


def test_request_ledger_blocks_second_request_key_and_exposes_processing_after_refresh(tmp_path: Path) -> None:
    repository = ConversationRepository(Database(tmp_path / "instance" / "workspace.db"))
    owner = "user:1"
    operation = "ask:conversation-1"

    assert repository.claim_ledger(owner_key=owner, request_key="request-a", operation=operation, lease_seconds=60)
    processing = repository.processing_operation(owner_key=owner, operation=operation)
    assert processing["active"] is True
    assert processing["lease_expires_at"]
    assert repository.claim_ledger(owner_key=owner, request_key="request-b", operation=operation, lease_seconds=60) is False

    repository.release_operation(owner_key=owner, operation=operation)
    assert repository.processing_operation(owner_key=owner, operation=operation) == {"active": False}
    assert repository.claim_ledger(owner_key=owner, request_key="request-b", operation=operation, lease_seconds=60)


def test_registry_cancellation_targets_loaded_package_without_importing_unloaded_package(tmp_path: Path) -> None:
    models_root = tmp_path / "models"
    registry = ModelRegistry(models_root, development=False, strict_capabilities=False)
    calls: list[str] = []
    module = ModuleType("fake_loaded_model")
    module.cancel_conversation = lambda *, conversation_id: calls.append(conversation_id)
    registry._modules["code/v1.0"] = ("fingerprint", module)

    registry.cancel_conversation("conversation-1", model_id="code/v1.0")
    registry.cancel_conversation("conversation-2", model_id="chat/v1.0")

    assert calls == ["conversation-1"]


def test_request_ledger_heartbeat_renews_exact_processing_row(tmp_path: Path) -> None:
    repository = ConversationRepository(Database(tmp_path / "instance" / "workspace.db"))
    owner = "user:1"
    operation = "ask:conversation-1"
    assert repository.claim_ledger(owner_key=owner, request_key="request-a", operation=operation, lease_seconds=30)
    before = repository.processing_operation(owner_key=owner, operation=operation)["lease_expires_at"]
    time.sleep(0.002)
    assert repository.renew_ledger(owner_key=owner, request_key="request-a", operation=operation, lease_seconds=120)
    after = repository.processing_operation(owner_key=owner, operation=operation)["lease_expires_at"]
    assert after > before
    assert repository.renew_ledger(owner_key=owner, request_key="other", operation=operation, lease_seconds=120) is False


def test_delete_cancels_backend_before_deleting_conversation() -> None:
    # Load the service without requiring the optional Flask/Werkzeug web layer in
    # this unit-only validation environment. UploadService is only a type here.
    previous = sys.modules.get("crowai.uploads.service")
    upload_stub = types.ModuleType("crowai.uploads.service")
    upload_stub.UploadService = object  # type: ignore[attr-defined]
    sys.modules["crowai.uploads.service"] = upload_stub
    try:
        spec = importlib.util.spec_from_file_location(
            "_isolated_conversation_service", ROOT / "crowai" / "conversations" / "service.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        ConversationService = module.ConversationService
    finally:
        if previous is None:
            sys.modules.pop("crowai.uploads.service", None)
        else:
            sys.modules["crowai.uploads.service"] = previous

    events: list[str] = []

    class RepositoryStub:
        def get_for_owner(self, conversation_id, owner_key):
            return {"id": conversation_id, "owner_key": owner_key, "model_id": "code/v1.0"}

        def release_operation(self, **kwargs):
            events.append("release-operation")

        def delete(self, conversation_id, owner_key):
            events.append("delete-sqlite")
            return True, []

    class RegistryStub:
        def cancel_conversation(self, conversation_id, *, model_id=None):
            events.append("cancel-backend")

        def cleanup_conversation(self, conversation_id, *, model_id=None):
            events.append("cleanup-state")

    service = ConversationService(
        RepositoryStub(),
        SimpleNamespace(),
        SimpleNamespace(registry=RegistryStub()),
    )
    service.delete("conversation-1", "user:1")
    assert events == ["release-operation", "delete-sqlite", "cleanup-state"]


def test_service_refresh_lock_and_delete_cancel_active_generation(tmp_path: Path) -> None:
    """Exercise the Core lifecycle that backs F5 recovery and delete-to-cancel."""
    from crowai.conversations.schemas import AskRequest
    from crowai.errors import ConflictError, ResourceNotFound

    previous = sys.modules.get("crowai.uploads.service")
    upload_stub_module = types.ModuleType("crowai.uploads.service")
    upload_stub_module.UploadService = object  # type: ignore[attr-defined]
    sys.modules["crowai.uploads.service"] = upload_stub_module
    try:
        spec = importlib.util.spec_from_file_location(
            "_isolated_conversation_service_concurrency",
            ROOT / "crowai" / "conversations" / "service.py",
        )
        assert spec is not None and spec.loader is not None
        service_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(service_module)
        ConversationService = service_module.ConversationService
    finally:
        if previous is None:
            sys.modules.pop("crowai.uploads.service", None)
        else:
            sys.modules["crowai.uploads.service"] = previous

    repository = ConversationRepository(Database(tmp_path / "instance" / "workspace.db"))
    conversation_id = "conversation-active"
    owner_key = "user:1"
    repository.create(
        conversation_id=conversation_id,
        owner_key=owner_key,
        model_id="code/v1.0",
        request_key="create-1",
    )

    started = threading.Event()
    cancelled = threading.Event()
    cleanup_calls: list[str] = []

    class RegistryStub:
        def cancel_conversation(self, value, *, model_id=None):
            assert value == conversation_id
            assert model_id == "code/v1.0"
            cancelled.set()

        def cleanup_conversation(self, value, *, model_id=None):
            cleanup_calls.append(value)

    class ModelServiceStub:
        registry = RegistryStub()

        def list_models(self):
            return [{"id": "code/v1.0", "runnable": True}]

        def validate_selection(self, model_id):
            assert model_id == "code/v1.0"
            return model_id

        def execute(self, **kwargs):
            started.set()
            assert cancelled.wait(3), "delete did not signal the active model request"
            return {
                "status": "complete",
                "success": True,
                "answer": "late answer that must never be stored",
                "analysis": {},
                "sources": [],
                "artifacts": [],
                "warnings": [],
            }

    class UploadServiceStub:
        def for_model(self, attachment_ids, owner):
            assert owner == owner_key
            return []

    service = ConversationService(repository, UploadServiceStub(), ModelServiceStub())
    first_request = AskRequest(
        question="generate code",
        model_id="code/v1.0",
        language="en",
        interaction_mode="conversation",
        attachment_ids=(),
        request_key="turn-a",
        execution={"allow": False, "backend": "isolated"},
    )
    second_request = AskRequest(
        question="second message",
        model_id="code/v1.0",
        language="en",
        interaction_mode="conversation",
        attachment_ids=(),
        request_key="turn-b",
        execution={"allow": False, "backend": "isolated"},
    )

    first_error: list[BaseException] = []

    def run_first() -> None:
        try:
            service.ask(conversation_id, owner_key, first_request, request_id="http-a")
        except BaseException as exc:  # captured for assertion from the worker thread
            first_error.append(exc)

    worker = threading.Thread(target=run_first, daemon=True)
    worker.start()
    assert started.wait(2), "first generation did not reach the model service"

    refreshed = service.get(conversation_id, owner_key)
    assert refreshed["processing"]["active"] is True

    with pytest.raises(ConflictError) as conflict:
        service.ask(conversation_id, owner_key, second_request, request_id="http-b")
    assert conflict.value.status == 409
    assert conflict.value.details == {
        "conversation_processing": True,
        "conversation_id": conversation_id,
    }

    service.delete(conversation_id, owner_key)
    worker.join(timeout=3)
    assert not worker.is_alive(), "active generation did not unwind after conversation deletion"
    assert cancelled.is_set()
    assert cleanup_calls == [conversation_id]
    assert len(first_error) == 1
    assert isinstance(first_error[0], ResourceNotFound)
    assert repository.get_for_owner(conversation_id, owner_key) is None
    assert repository.processing_operation(
        owner_key=owner_key, operation=f"ask:{conversation_id}"
    ) == {"active": False}

@pytest.mark.parametrize("model_id", ["chat/v1.0", "code/v1.0", "agent/v1.0"])
def test_cancel_during_backend_startup_cannot_orphan_new_process(
    model_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io

    module = _engine_module(model_id)
    engine = _engine_class(module, model_id)()
    process = _LiveProcess()
    popen_entered = threading.Event()
    release_popen = threading.Event()

    monkeypatch.setattr(engine, "_validate_files", lambda: None)
    monkeypatch.setattr(engine, "_wait_until_ready", lambda: None)
    monkeypatch.setattr(module, "open_private_log", lambda path: io.StringIO())

    def fake_popen(*args, **kwargs):
        popen_entered.set()
        assert release_popen.wait(2)
        return process

    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)
    if os.name == "posix":
        monkeypatch.setattr(module.os, "getpgid", lambda pid: pid)
        monkeypatch.setattr(module.os, "killpg", lambda pgid, sig: setattr(process, "returncode", 0))
    else:
        monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: setattr(process, "returncode", 0))

    start_error: list[BaseException] = []

    def start_backend() -> None:
        try:
            engine.start()
        except BaseException as exc:
            start_error.append(exc)

    starter = threading.Thread(target=start_backend, daemon=True)
    starter.start()
    assert popen_entered.wait(1)

    cancelled = threading.Event()
    canceller = threading.Thread(target=lambda: (engine.cancel(), cancelled.set()), daemon=True)
    canceller.start()
    # cancel() sets the cancellation token immediately but must wait until Popen's
    # atomic publication section can expose both process + ownership together.
    assert engine._cancelled.wait(0.5)
    assert not cancelled.is_set()

    release_popen.set()
    starter.join(timeout=2)
    canceller.join(timeout=2)
    assert not starter.is_alive()
    assert not canceller.is_alive()
    assert cancelled.is_set()
    assert engine._process is None
    assert engine._owns_process is False
    assert process.poll() is not None
    assert not start_error
