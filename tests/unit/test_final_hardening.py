from __future__ import annotations

import importlib
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from crowai.conversations.schemas import AskRequest
from crowai.errors import ModelUnavailable, ValidationError
from crowai.models.service import ModelService
from models.registry import ModelRegistry
from tools.check_dependencies import pyproject_dependencies, requirements_txt
from tools.validate_model_package import validate as validate_model_package

ROOT = Path(__file__).resolve().parents[2]


def _write_package(
    root: Path,
    mode: str,
    *,
    runnable: bool,
    order: int = 100,
    mmproj: bool = False,
) -> Path:
    directory = root / mode / "v1.0"
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {
        "id": "v1.0",
        "name": f"{mode.title()} V1.0",
        "version": "1.0",
        "description": "Test package",
        "display_order": order,
        "capabilities": ["conversation"],
        "model_contract_version": 1,
        "minimum_core_version": "4.0.0",
    }
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (directory / "__init__.py").write_text(
        "def prepare_request(**kwargs): return {'metadata': {}}\n"
        "def finalize_result(**kwargs): return {'answer': 'ok', 'success': True}\n",
        encoding="utf-8",
    )
    config = {
        "runtime_file": "runtime/llama-server.exe",
        "model_file": "model/weights.gguf",
    }
    if mmproj:
        config["mmproj_file"] = "model/mmproj.gguf"
    (directory / "config.json").write_text(json.dumps(config), encoding="utf-8")
    if runnable:
        (directory / "runtime").mkdir()
        (directory / "model").mkdir()
        runtime = directory / "runtime" / "llama-server.exe"
        runtime.write_bytes(b"runtime")
        if os.name != "nt":
            runtime.chmod(0o755)
        (directory / "model" / "weights.gguf").write_bytes(b"GGUFmodel")
        if mmproj:
            (directory / "model" / "mmproj.gguf").write_bytes(b"GGUFprojector")
    return directory


def _code_runner_module():
    registry = ModelRegistry(ROOT / "models", development=True, strict_capabilities=True)
    package = registry._load("code/v1.0")
    return importlib.import_module(f"{package.__name__}.runner")


def test_installed_package_is_not_treated_as_runnable_when_local_files_are_missing(tmp_path: Path) -> None:
    _write_package(tmp_path, "chat", runnable=False)
    registry = ModelRegistry(tmp_path)
    status = registry.status()
    assert len(status["installed_models"]) == 1
    assert status["runnable_models"] == []
    assert status["models_available"] is False
    assert status["models"][0]["runnable"] is False
    assert status["models"][0]["status"] == "missing_local_files"
    assert registry.default_id() == ""
    with pytest.raises(Exception, match="not runnable"):
        registry.runnable_descriptor("chat/v1.0")
    with pytest.raises(ModelUnavailable, match="not runnable"):
        ModelService(registry).validate_selection("chat/v1.0")


def test_one_runnable_model_is_selected_even_when_earlier_packages_are_unavailable(tmp_path: Path) -> None:
    _write_package(tmp_path, "chat", runnable=False, order=1)
    _write_package(tmp_path, "code", runnable=True, order=50)
    _write_package(tmp_path, "agent", runnable=False, order=100, mmproj=True)
    registry = ModelRegistry(tmp_path)
    status = registry.status()
    assert status["models_available"] is True
    assert [item["id"] for item in status["runnable_models"]] == ["code/v1.0"]
    assert registry.default_id() == "code/v1.0"
    assert registry.runnable_descriptor("code/v1.0").id == "code/v1.0"


def test_model_readiness_public_state_never_leaks_local_paths(tmp_path: Path) -> None:
    _write_package(tmp_path, "agent", runnable=False, mmproj=True)
    registry = ModelRegistry(tmp_path)
    serialized = json.dumps(registry.status())
    assert str(tmp_path.resolve()) not in serialized
    assert "/tmp/" not in serialized
    model = registry.list_models()[0]
    assert model["missing_requirements"] == ["runtime", "model", "vision_projector"]
    assert model["availability_message"] == "Local files missing"


def test_execution_request_contract_defaults_disabled_and_requires_explicit_boolean() -> None:
    base = {"question": "run it", "model_id": "code/v1.0"}
    parsed = AskRequest.parse(base, maximum_message_length=12000)
    assert parsed.execution == {"allow": False, "backend": "isolated"}
    explicit = AskRequest.parse(
        {**base, "execution": {"allow": True, "backend": "isolated"}},
        maximum_message_length=12000,
    )
    assert explicit.execution == {"allow": True, "backend": "isolated"}
    with pytest.raises(ValidationError):
        AskRequest.parse({**base, "execution": {"allow": "true"}}, maximum_message_length=12000)
    with pytest.raises(ValidationError):
        AskRequest.parse({**base, "execution": {"allow": True, "backend": "host"}}, maximum_message_length=12000)


def test_code_execution_is_disabled_by_default_even_for_run_language(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _code_runner_module()
    monkeypatch.setattr(
        runner.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("host subprocess must not start")),
    )
    result = runner.execute_python_artifacts([{"path": "main.py", "code": "print('run it')\n"}])
    assert result["executed"] is False
    assert result["requested"] is False
    assert result["backend"] == "disabled"
    assert result["isolation"]["enforced"] is False


def test_isolated_request_does_not_fall_back_to_host_when_docker_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _code_runner_module()
    monkeypatch.setattr(runner.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        runner.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("host subprocess must not start")),
    )
    result = runner.execute_python_artifacts(
        [{"path": "main.py", "code": "print('ok')\n"}],
        execution_policy={"allow": True, "backend": "isolated"},
        config={"python_isolated_backend": "docker", "python_docker_image": "python:3.13-slim"},
    )
    assert result["requested"] is True
    assert result["executed"] is False
    assert result["backend"] == "docker"
    assert result["reason"] == "isolated_backend_unavailable"
    assert result["isolation"]["host_filesystem_isolated"] is False
    assert result["isolation"]["network_isolated"] is False
    assert result["backend_capabilities"]["host_filesystem_isolated"] is True


def test_trusted_local_backend_requires_double_opt_in_and_is_never_production_default(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _code_runner_module()
    policy = {"allow": True, "backend": "trusted-local"}
    config = {"python_trusted_local_enabled": True}

    monkeypatch.delenv("CROWAI_CODE_TRUSTED_LOCAL_EXECUTION", raising=False)
    monkeypatch.setenv("CROWAI_ENV", "development")
    assert runner.select_backend(policy, config).name == "disabled"

    monkeypatch.setenv("CROWAI_CODE_TRUSTED_LOCAL_EXECUTION", "1")
    monkeypatch.setenv("CROWAI_ENV", "production")
    assert runner.select_backend(policy, config).name == "disabled"

    monkeypatch.setenv("CROWAI_ENV", "development")
    backend = runner.select_backend(policy, config)
    assert backend.name == "trusted-local"
    assert backend.trusted_local is True
    assert backend.host_filesystem_isolated is False


def test_docker_command_enforces_declared_isolation_controls(tmp_path: Path) -> None:
    runner = _code_runner_module()
    backend = runner.DockerRunner(docker_binary="docker", image="python:3.13-slim")
    command = backend._command(tmp_path.resolve(), "main.py")
    joined = " ".join(command)
    assert "--pull never" in joined
    assert "--network none" in joined
    assert "--ipc none" in joined
    assert "--ulimit nofile=128:128" in joined
    assert f"--ulimit fsize={backend.file_size_limit_bytes}:{backend.file_size_limit_bytes}" in joined
    assert "--ulimit core=0:0" in joined
    assert "--read-only" in command
    assert "--cap-drop ALL" in joined
    assert "--security-opt no-new-privileges" in joined
    assert f"--user {backend._container_identity()[0]}" in joined
    assert "--pids-limit" in command
    assert "--memory" in command
    assert "--cpus" in command
    assert "--tmpfs" in command
    assert "--privileged" not in command
    assert "/var/run/docker.sock" not in joined
    assert "--pid=host" not in joined and "--pid host" not in joined
    assert command.count("--mount") == 1
    host_mount = command[command.index("--mount") + 1]
    assert "dst=/input" in host_mount
    assert "readonly" in host_mount
    tmpfs_values = [command[index + 1] for index, value in enumerate(command) if value == "--tmpfs"]
    assert any(value.startswith("/workspace:rw,") for value in tmpfs_values)
    assert str(backend.workspace_limit_bytes) in " ".join(tmpfs_values)
    assert backend.image_digest_pinned is False
    pinned = runner.DockerRunner(
        docker_binary="docker",
        image="python@sha256:" + ("a" * 64),
    )
    assert pinned.image_digest_pinned is True
    source = (ROOT / "models" / "code" / "v1.0" / "runner.py").read_text(encoding="utf-8")
    assert "shell=True" not in source
    assert '"shell": False' in source


def test_trusted_local_evidence_admits_host_and_network_are_not_isolated(tmp_path: Path) -> None:
    runner = _code_runner_module()
    sentinel = tmp_path / "outside.txt"
    sentinel.write_text("visible-to-host", encoding="utf-8")
    code = f"from pathlib import Path\nprint(Path({str(sentinel)!r}).read_text())\n"
    result = runner.TrustedLocalRunner(enabled=True).run([{"path": "main.py", "code": code}])
    assert result["executed"] is True
    assert "visible-to-host" in result["stdout"]
    assert result["backend"] == "trusted-local"
    assert result["isolation"]["host_filesystem_isolated"] is False
    assert result["isolation"]["network_isolated"] is False
    assert "not a security sandbox" in result["limitations"][0]


def test_code_package_validator_accepts_current_execution_capabilities() -> None:
    assert validate_model_package(ROOT / "models" / "code" / "v1.0") == []


def test_requirements_and_pyproject_runtime_dependencies_match() -> None:
    assert requirements_txt(ROOT / "requirements.txt") == pyproject_dependencies(ROOT / "pyproject.toml")
    assert any(item.startswith("pymupdf") for item in requirements_txt(ROOT / "requirements.txt"))


def test_real_docker_backend_blocks_external_sentinel_and_network_when_available(tmp_path: Path) -> None:
    runner = _code_runner_module()
    backend = runner.DockerRunner(image=runner.DEFAULT_DOCKER_IMAGE, workspace_limit_bytes=8 * 1024 * 1024)
    if not backend.available():
        pytest.skip("Docker plus the configured local Python image are not available in this validation environment.")

    sentinel = tmp_path / "host-sentinel.txt"
    sentinel.write_text("HOST-SECRET", encoding="utf-8")
    code = f'''from pathlib import Path\nimport urllib.request\ntry:\n    print("sentinel=" + Path({str(sentinel)!r}).read_text())\nexcept Exception:\n    print("sentinel=blocked")\ntry:\n    urllib.request.urlopen("https://example.com", timeout=1).read(1)\n    print("network=reachable")\nexcept Exception:\n    print("network=blocked")\n'''
    result = backend.run([{"path": "main.py", "code": code}], timeout_seconds=5)
    assert result["executed"] is True
    assert "sentinel=blocked" in result["stdout"]
    assert "HOST-SECRET" not in result["stdout"]
    assert "network=blocked" in result["stdout"]
    assert result["isolation"]["host_filesystem_isolated"] is True
    assert result["isolation"]["network_isolated"] is True
    assert result["limits"]["individual_file_size_limit_bytes"] == backend.file_size_limit_bytes
    assert result["limits"]["core_dump_limit_bytes"] == 0
    assert result["limits"]["aggregate_workspace_limit_enforced"] is True
    assert result["limits"]["aggregate_workspace_limit_backend"] == "tmpfs"
    assert result["limits"]["aggregate_workspace_limit_bytes"] == backend.workspace_limit_bytes
    assert result["limits"]["host_workspace_mount_read_only"] is True

    oversized = backend.run([
        {
            "path": "main.py",
            "code": (
                "with open('oversized.bin', 'wb') as handle:\n"
                f"    handle.write(b'x' * {backend.file_size_limit_bytes + 1024})\n"
            ),
        }
    ], timeout_seconds=5)
    assert oversized["executed"] is True
    assert oversized["passed"] is False

    aggregate_overflow = backend.run([
        {
            "path": "main.py",
            "code": (
                "chunk = b'x' * (256 * 1024)\n"
                "for index in range(64):\n"
                "    with open(f'piece-{index}.bin', 'wb') as handle:\n"
                "        handle.write(chunk)\n"
            ),
        }
    ], timeout_seconds=8)
    assert aggregate_overflow["executed"] is True
    assert aggregate_overflow["passed"] is False
    assert aggregate_overflow["limits"]["aggregate_workspace_limit_enforced"] is True


def test_registry_runtime_fallbacks_and_invalid_gguf(tmp_path: Path) -> None:
    package = _write_package(tmp_path, "chat", runnable=True)
    configured = package / "runtime" / "llama-server.exe"
    linux = package / "runtime" / "llama-server"
    configured.rename(linux)
    registry = ModelRegistry(tmp_path)
    assert registry.readiness("chat/v1.0")["runnable"] is True

    config = json.loads((package / "config.json").read_text())
    config["runtime_file"] = "runtime/llama-server"
    (package / "config.json").write_text(json.dumps(config))
    linux.rename(configured)
    registry.refresh()
    assert registry.readiness("chat/v1.0")["runnable"] is True

    (package / "model" / "weights.gguf").write_bytes(b"")
    empty = registry.readiness("chat/v1.0")
    assert empty["runnable"] is False
    assert empty["status"] == "invalid_local_files"
    assert empty["invalid_requirements"] == ["model"]
    (package / "model" / "weights.gguf").write_bytes(b"NOPEplaceholder")
    invalid = registry.readiness("chat/v1.0")
    assert invalid["runnable"] is False
    assert invalid["status"] == "invalid_local_files"
    assert registry.list_models()[0]["availability_message"] == "Local files invalid"


def test_runtime_symlink_escape_is_not_runnable(tmp_path: Path) -> None:
    package = _write_package(tmp_path, "chat", runnable=True)
    outside = tmp_path / "outside-runtime"
    outside.write_bytes(b"runtime")
    runtime = package / "runtime" / "llama-server.exe"
    runtime.unlink()
    try:
        runtime.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    registry = ModelRegistry(tmp_path)
    assert registry.list_models() == []
    assert any(issue.get("code") == "invalid_model_package" for issue in registry.issues())


def test_public_trusted_local_compatibility_helper_is_gone() -> None:
    runner = _code_runner_module()
    assert not hasattr(runner, "run_python_artifacts")
    assert not hasattr(runner, "_run_python_artifacts_trusted_for_tests")
    pipeline_source = (ROOT / "models" / "code" / "v1.0" / "pipeline.py").read_text(encoding="utf-8")
    engine_source = (ROOT / "models" / "code" / "v1.0" / "engine.py").read_text(encoding="utf-8")
    assert "TrustedLocalRunner" not in pipeline_source
    assert "TrustedLocalRunner" not in engine_source


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable-bit readiness is not applicable on Windows")
def test_registry_rejects_non_executable_runtime_but_uses_executable_linux_fallback(tmp_path: Path) -> None:
    package = _write_package(tmp_path, "chat", runnable=True)
    configured = package / "runtime" / "llama-server.exe"
    configured.chmod(0o644)
    registry = ModelRegistry(tmp_path)
    not_executable = registry.readiness("chat/v1.0")
    assert not_executable["runnable"] is False
    assert not_executable["status"] == "invalid_local_files"
    assert not_executable["invalid_requirements"] == ["runtime"]

    linux = package / "runtime" / "llama-server"
    linux.write_bytes(b"runtime")
    linux.chmod(0o755)
    registry.refresh()
    assert registry.readiness("chat/v1.0")["runnable"] is True


def test_runtime_candidate_resolution_never_searches_outside_package(tmp_path: Path) -> None:
    from models.local_files import package_local_file, runtime_candidates

    package = tmp_path / "chat" / "v1.0"
    (package / "runtime").mkdir(parents=True)
    with pytest.raises(ValueError):
        runtime_candidates(package, "../llama-server")
    with pytest.raises(ValueError):
        runtime_candidates(package, str((tmp_path / "llama-server").resolve()))
    with pytest.raises(ValueError):
        package_local_file(package, "../../outside.gguf", area="model")


def test_registry_reports_mixed_missing_and_invalid_requirements_without_paths(tmp_path: Path) -> None:
    package = _write_package(tmp_path, "agent", runnable=True, mmproj=True)
    (package / "model" / "weights.gguf").write_bytes(b"BAD!")
    (package / "model" / "mmproj.gguf").unlink()
    registry = ModelRegistry(tmp_path)
    readiness = registry.readiness("agent/v1.0")
    assert readiness["runnable"] is False
    assert readiness["status"] == "missing_local_files"
    assert readiness["missing_requirements"] == ["vision_projector", "model"] or set(readiness["missing_requirements"]) == {"model", "vision_projector"}
    assert readiness["invalid_requirements"] == ["model"]
    assert str(tmp_path) not in json.dumps(readiness)


def test_docker_workspace_uses_read_only_host_input_and_hard_tmpfs_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _code_runner_module()
    backend = runner.DockerRunner(docker_binary="docker", image="python:3.13-slim", workspace_limit_bytes=16 * 1024 * 1024)
    monkeypatch.setattr(backend, "available", lambda: True)

    def fake_run(command, *, cwd, env, timeout_seconds, output_bytes, preexec_limits):
        mount = command[command.index("--mount") + 1]
        assert "dst=/input" in mount and "readonly" in mount
        tmpfs_values = [command[index + 1] for index, value in enumerate(command) if value == "--tmpfs"]
        workspace_tmpfs = next(value for value in tmpfs_values if value.startswith("/workspace:rw,"))
        assert str(backend.workspace_limit_bytes) in workspace_tmpfs
        if os.name == "posix":
            input_root = Path(mount.split("src=", 1)[1].split(",dst=", 1)[0])
            assert stat.S_IMODE(input_root.stat().st_mode) == 0o700
            assert stat.S_IMODE((input_root / "main.py").stat().st_mode) == 0o600
        return {
            "exit_code": 0,
            "passed": True,
            "timed_out": False,
            "output_limit_exceeded": False,
            "duration_ms": 1,
            "stdout": "ok",
            "stderr": "",
            "stdout_truncated": False,
            "stderr_truncated": False,
            "timeout_seconds": timeout_seconds,
            "output_bytes_per_stream": output_bytes,
        }

    monkeypatch.setattr(runner, "_run_to_files", fake_run)
    result = backend.run([{"path": "main.py", "code": "print('ok')\n"}])

    assert result["passed"] is True
    assert result["limits"]["individual_file_size_limit_bytes"] == backend.file_size_limit_bytes
    assert result["limits"]["core_dump_limit_bytes"] == 0
    assert result["limits"]["aggregate_workspace_limit_enforced"] is True
    assert result["limits"]["aggregate_workspace_limit_backend"] == "tmpfs"
    assert result["limits"]["aggregate_workspace_limit_bytes"] == backend.workspace_limit_bytes
    assert result["limits"]["host_workspace_mount_read_only"] is True
    assert result["limits"]["workspace_writable_by_unprivileged_user"] is True
    if os.name == "posix":
        assert result["limits"]["host_workspace_private_posix_modes"] is True
        assert result["limits"]["host_workspace_world_writable"] is False
    assert "hard size cap" in result["limitations"][2]


def test_docker_workspace_limit_is_bounded() -> None:
    runner = _code_runner_module()
    low = runner.DockerRunner(docker_binary="docker", workspace_limit_bytes=1)
    high = runner.DockerRunner(docker_binary="docker", workspace_limit_bytes=10**12)
    assert low.workspace_limit_bytes == 8 * 1024 * 1024
    assert high.workspace_limit_bytes == 128 * 1024 * 1024


def test_trusted_local_runner_disables_site_initialization_for_portable_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _code_runner_module()
    captured: dict[str, object] = {}

    def fake_run(command, *, cwd, env, timeout_seconds, output_bytes, preexec_limits):
        captured["command"] = list(command)
        return {
            "exit_code": 0,
            "passed": True,
            "timed_out": False,
            "output_limit_exceeded": False,
            "duration_ms": 1,
            "stdout": "ok\n",
            "stderr": "",
            "stdout_truncated": False,
            "stderr_truncated": False,
            "timeout_seconds": timeout_seconds,
            "output_bytes_per_stream": output_bytes,
        }

    monkeypatch.setattr(runner, "_run_to_files", fake_run)
    result = runner.TrustedLocalRunner(enabled=True).run([{"path": "main.py", "code": "print('ok')\n"}])

    command = captured["command"]
    assert isinstance(command, list)
    assert command[1:4] == ["-E", "-s", "-S"]
    assert result["limits"]["site_initialization_disabled"] is True
    assert result["limits"]["python_environment_ignored"] is True
    assert result["limits"]["user_site_disabled"] is True
