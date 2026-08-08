"""Explicit Python execution backends for CrowAI Code V1.0.

Syntax/AST validation is handled separately by the Code pipeline.  Generated
Python is never executed on the host by default.  Actual execution requires an
explicit request policy and a backend with a clearly stated trust boundary.
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from abc import ABC, abstractmethod
from pathlib import Path, PurePosixPath
from typing import Any

MAX_FILES = 24
MAX_WORKSPACE_BYTES = 2_000_000
MAX_OUTPUT_BYTES = 64_000
DEFAULT_TIMEOUT_SECONDS = 5
DEFAULT_MEMORY_MB = 512
DEFAULT_CPUS = 1.0
DEFAULT_PIDS = 64
DEFAULT_FILE_SIZE_LIMIT_BYTES = 8 * 1024 * 1024
DEFAULT_WORKSPACE_LIMIT_BYTES = 32 * 1024 * 1024
DEFAULT_DOCKER_IMAGE = "python:3.13-slim@sha256:9662417aace5ae7b8e2609cce472b72a8958e134ba372808abe9cc1a0c0125e6"
_DOCKER_IMAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,200}$")
_DOCKER_DIGEST = re.compile(r"@sha256:[0-9a-fA-F]{64}$")


class RunnerError(ValueError):
    pass


class RunnerBackend(ABC):
    """Small execution backend interface with explicit trust metadata."""

    name = "unknown"
    isolated = False
    network_isolated = False
    host_filesystem_isolated = False
    trusted_local = False

    @abstractmethod
    def available(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def run(
        self,
        artifacts: list[dict[str, Any]],
        *,
        support_files: list[dict[str, Any]] | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        output_bytes: int = MAX_OUTPUT_BYTES,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def _base_evidence(self, *, enforced: bool = False) -> dict[str, Any]:
        return {
            "backend": self.name,
            "isolation": {
                "enforced": enforced,
                "isolated_backend": self.isolated if enforced else False,
                "host_filesystem_isolated": self.host_filesystem_isolated if enforced else False,
                "network_isolated": self.network_isolated if enforced else False,
                "trusted_local": self.trusted_local,
            },
            "backend_capabilities": {
                "isolated_backend": self.isolated,
                "host_filesystem_isolated": self.host_filesystem_isolated,
                "network_isolated": self.network_isolated,
            },
        }


class DisabledRunner(RunnerBackend):
    name = "disabled"

    def available(self) -> bool:
        return True

    def run(
        self,
        artifacts: list[dict[str, Any]],
        *,
        support_files: list[dict[str, Any]] | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        output_bytes: int = MAX_OUTPUT_BYTES,
    ) -> dict[str, Any]:
        return {
            "executed": False,
            "reason": "execution_disabled",
            **self._base_evidence(),
        }


def _safe_path(value: Any) -> PurePosixPath:
    raw = str(value or "").replace("\\", "/").strip()
    path = PurePosixPath(raw)
    if not raw or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise RunnerError("Artifact path is unsafe.")
    if any(":" in part for part in path.parts):
        raise RunnerError("Artifact path contains an unsupported drive or stream marker.")
    return path


def _minimal_environment() -> dict[str, str]:
    allowed = {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TMP", "TEMP", "LANG", "LC_ALL"}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["NO_PROXY"] = "*"
    env["no_proxy"] = "*"
    return env


def _posix_limits() -> None:
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (3, 3))
        resource.setrlimit(resource.RLIMIT_FSIZE, (1_000_000, 1_000_000))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
        if hasattr(resource, "RLIMIT_NPROC"):
            resource.setrlimit(resource.RLIMIT_NPROC, (32, 32))
        if hasattr(resource, "RLIMIT_AS"):
            resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    except Exception:
        # TrustedLocalRunner explicitly reports these limits as best-effort.
        pass


def _choose_entrypoint(paths: list[str]) -> str | None:
    py = [value for value in paths if value.casefold().endswith(".py")]
    if not py:
        return None
    preferred = ["main.py", "app.py", "run.py"]
    by_lower = {value.casefold(): value for value in py}
    for candidate in preferred:
        if candidate in by_lower:
            return by_lower[candidate]
    non_tests = [value for value in py if "test" not in PurePosixPath(value).name.casefold()]
    return non_tests[0] if non_tests else py[0]


def _prepare_workspace(
    root: Path,
    artifacts: list[dict[str, Any]],
    support_files: list[dict[str, Any]] | None,
) -> str | None:
    generated = [
        item
        for item in artifacts
        if isinstance(item, dict) and str(item.get("path") or item.get("filename") or "").casefold().endswith(".py")
    ]
    if not generated:
        return None
    supports = [
        item
        for item in (support_files or [])
        if isinstance(item, dict)
        and str(item.get("path") or item.get("filename") or "").casefold().endswith(".py")
        and isinstance(item.get("content"), str)
    ]
    if len(generated) + len(supports) > MAX_FILES:
        raise RunnerError(f"Python runner accepts at most {MAX_FILES} workspace files.")

    merged: dict[str, tuple[PurePosixPath, str, bool]] = {}
    order: list[str] = []
    total = 0
    for item, is_generated in [*((value, False) for value in supports), *((value, True) for value in generated)]:
        relative = _safe_path(item.get("path") or item.get("filename"))
        key = relative.as_posix().casefold()
        content = str(item.get("code") if is_generated else item.get("content") or "")
        if key in merged:
            prior_path, prior_content, prior_generated = merged[key]
            if prior_path.as_posix() != relative.as_posix():
                raise RunnerError("Python workspace contains a case-insensitive path collision.")
            if prior_generated and is_generated:
                raise RunnerError("Generated artifacts contain a duplicate path.")
            total -= len(prior_content.encode("utf-8"))
        else:
            order.append(key)
        total += len(content.encode("utf-8"))
        if total > MAX_WORKSPACE_BYTES:
            raise RunnerError("Python runner workspace exceeds the size limit.")
        merged[key] = (relative, content, is_generated or merged.get(key, (None, None, False))[2])

    paths = [relative.as_posix() for relative, _, is_generated in merged.values() if is_generated]
    entrypoint = _choose_entrypoint(paths)
    if not entrypoint:
        return ""

    resolved_root = root.resolve()
    for key in order:
        relative, content, _ = merged[key]
        target = (resolved_root / Path(*relative.parts)).resolve()
        try:
            target.relative_to(resolved_root)
        except ValueError as exc:
            raise RunnerError("Generated artifact escaped the temporary workspace.") from exc
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.is_symlink():
            raise RunnerError("Symlinks are not permitted in the execution workspace.")
        target.write_text(content, encoding="utf-8")
    return entrypoint


def _bounded_values(timeout_seconds: int, output_bytes: int) -> tuple[int, int]:
    return max(1, min(int(timeout_seconds), 30)), max(1, min(int(output_bytes), MAX_OUTPUT_BYTES))


def _terminate(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    if os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            process.kill()
    else:
        process.kill()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()


def _run_to_files(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None,
    timeout_seconds: int,
    output_bytes: int,
    preexec_limits: bool,
) -> dict[str, Any]:
    timeout_limit, output_limit = _bounded_values(timeout_seconds, output_bytes)
    stdout_path = cwd / ".runner.stdout"
    stderr_path = cwd / ".runner.stderr"
    kwargs: dict[str, Any] = {
        "cwd": cwd,
        "env": env,
        "stdin": subprocess.DEVNULL,
        "shell": False,
        "start_new_session": os.name != "nt",
    }
    if preexec_limits and os.name != "nt":
        kwargs["preexec_fn"] = _posix_limits
    elif os.name == "nt" and hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    started = time.monotonic()
    timed_out = False
    output_limit_exceeded = False
    with stdout_path.open("wb") as stdout_handle, stderr_path.open("wb") as stderr_handle:
        try:
            process = subprocess.Popen(command, stdout=stdout_handle, stderr=stderr_handle, **kwargs)
        except OSError as exc:
            raise RunnerError("Python runner backend could not start.") from exc
        deadline = time.monotonic() + timeout_limit
        while process.poll() is None:
            if time.monotonic() >= deadline:
                timed_out = True
                break
            try:
                if stdout_path.stat().st_size > output_limit + 8192 or stderr_path.stat().st_size > output_limit + 8192:
                    output_limit_exceeded = True
                    break
            except OSError:
                pass
            time.sleep(0.02)
        if timed_out or output_limit_exceeded:
            _terminate(process)

    duration_ms = int((time.monotonic() - started) * 1000)
    stdout_raw = stdout_path.read_bytes()[:output_limit]
    stderr_raw = stderr_path.read_bytes()[:output_limit]
    return {
        "exit_code": process.returncode,
        "passed": bool(process.returncode == 0 and not timed_out and not output_limit_exceeded),
        "timed_out": timed_out,
        "output_limit_exceeded": output_limit_exceeded,
        "duration_ms": duration_ms,
        "stdout": stdout_raw.decode("utf-8", errors="replace"),
        "stderr": stderr_raw.decode("utf-8", errors="replace"),
        "stdout_truncated": stdout_path.stat().st_size > len(stdout_raw),
        "stderr_truncated": stderr_path.stat().st_size > len(stderr_raw),
        "timeout_seconds": timeout_limit,
        "output_bytes_per_stream": output_limit,
    }


class TrustedLocalRunner(RunnerBackend):
    """Development-only constrained host subprocess.  This is not a sandbox."""

    name = "trusted-local"
    trusted_local = True

    def __init__(self, *, enabled: bool = False) -> None:
        self.enabled = bool(enabled)

    def available(self) -> bool:
        return self.enabled

    def run(
        self,
        artifacts: list[dict[str, Any]],
        *,
        support_files: list[dict[str, Any]] | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        output_bytes: int = MAX_OUTPUT_BYTES,
    ) -> dict[str, Any]:
        if not self.enabled:
            return {"executed": False, "reason": "trusted_local_not_enabled", **self._base_evidence()}
        with tempfile.TemporaryDirectory(prefix="crowai-code-trusted-") as temporary:
            root = Path(temporary).resolve()
            root.chmod(0o755)
            entrypoint = _prepare_workspace(root, artifacts, support_files)
            for workspace_file in root.rglob("*"):
                if workspace_file.is_file():
                    workspace_file.chmod(0o644)
                elif workspace_file.is_dir():
                    workspace_file.chmod(0o755)
            if entrypoint is None:
                return {"executed": False, "reason": "no_python_artifact", **self._base_evidence()}
            if not entrypoint:
                return {"executed": False, "reason": "no_python_entrypoint", **self._base_evidence()}
            outcome = _run_to_files(
                [sys.executable, "-E", "-s", "-S", entrypoint],
                cwd=root,
                env=_minimal_environment(),
                timeout_seconds=timeout_seconds,
                output_bytes=output_bytes,
                preexec_limits=True,
            )
            return {
                "executed": True,
                "command_class": "python_generated_artifact",
                "entrypoint": entrypoint,
                **outcome,
                **self._base_evidence(enforced=True),
                "limits": {
                    "timeout_seconds": outcome["timeout_seconds"],
                    "output_bytes_per_stream": outcome["output_bytes_per_stream"],
                    "maximum_files": MAX_FILES,
                    "maximum_workspace_bytes": MAX_WORKSPACE_BYTES,
                    "environment_filtered": True,
                    "python_environment_ignored": True,
                    "user_site_disabled": True,
                    "site_initialization_disabled": True,
                    "path_escape_blocked": True,
                    "symlink_escape_blocked": True,
                    "posix_resource_limits_best_effort": os.name != "nt",
                    "network_isolated": False,
                    "host_filesystem_isolated": False,
                    "memory_mb": DEFAULT_MEMORY_MB if os.name != "nt" else None,
                    "process_limit": 32 if os.name != "nt" else None,
                },
                "limitations": [
                    "Trusted-local execution is a constrained host subprocess, not a security sandbox.",
                    "Host filesystem and network access are not isolated.",
                ],
            }


class DockerRunner(RunnerBackend):
    """Ephemeral Docker execution with no network and only the temp workspace mounted."""

    name = "docker"
    isolated = True
    network_isolated = True
    host_filesystem_isolated = True

    def __init__(
        self,
        *,
        image: str = DEFAULT_DOCKER_IMAGE,
        memory_mb: int = DEFAULT_MEMORY_MB,
        cpus: float = DEFAULT_CPUS,
        pids: int = DEFAULT_PIDS,
        file_size_limit_bytes: int = DEFAULT_FILE_SIZE_LIMIT_BYTES,
        workspace_limit_bytes: int = DEFAULT_WORKSPACE_LIMIT_BYTES,
        docker_binary: str | None = None,
    ) -> None:
        if not _DOCKER_IMAGE.fullmatch(str(image).strip()):
            raise RunnerError("Docker image reference is invalid.")
        self.image = str(image).strip()
        self.memory_mb = max(128, min(int(memory_mb), 2048))
        self.cpus = max(0.25, min(float(cpus), 4.0))
        self.pids = max(16, min(int(pids), 256))
        self.file_size_limit_bytes = max(1_000_000, min(int(file_size_limit_bytes), 64 * 1024 * 1024))
        self.workspace_limit_bytes = max(8 * 1024 * 1024, min(int(workspace_limit_bytes), 128 * 1024 * 1024))
        self.docker_binary = docker_binary or shutil.which("docker") or ""
        self.image_digest_pinned = bool(_DOCKER_DIGEST.search(self.image))

    def available(self) -> bool:
        if not self.docker_binary:
            return False
        try:
            result = subprocess.run(
                [self.docker_binary, "image", "inspect", self.image],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                timeout=3,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    def _container_identity(self) -> tuple[str, int | None, int | None]:
        """Choose a container UID/GID that can use a private host workspace.

        On POSIX, normal users are mirrored into the container so the bind mount
        can stay 0700/0600.  If CrowAI itself runs as root, the temporary tree is
        handed to nobody (65534) rather than running generated code as root.
        """
        if os.name == "posix" and hasattr(os, "getuid") and hasattr(os, "getgid"):
            uid = int(os.getuid())
            gid = int(os.getgid())
            if uid == 0:
                return "65534:65534", 65534, 65534
            return f"{uid}:{gid}", uid, gid
        return "65534:65534", None, None

    @staticmethod
    def _private_workspace_modes(root: Path, *, uid: int | None, gid: int | None) -> None:
        if os.name != "posix":
            # Docker Desktop/Windows ACL semantics are outside chmod's security
            # model; keep compatibility without claiming POSIX host isolation.
            root.chmod(0o777)
            for item in root.rglob("*"):
                item.chmod(0o666 if item.is_file() else 0o777)
            return

        items = [root, *root.rglob("*")]
        for item in items:
            if item.is_symlink():
                raise RunnerError("Symlinks are not permitted in the Docker execution workspace.")
            try:
                if uid is not None and gid is not None and os.getuid() == 0:
                    os.chown(item, uid, gid)
                item.chmod(0o600 if item.is_file() else 0o700)
            except OSError as exc:
                raise RunnerError("Unable to secure the Docker execution workspace.") from exc

    def _command(
        self,
        input_workspace: Path,
        entrypoint: str,
        *,
        container_user: str | None = None,
    ) -> list[str]:
        # Host input is mounted read-only. Generated code runs in a bounded tmpfs
        # workspace, so it cannot mutate the host tree and cannot consume more
        # than the configured aggregate workspace capacity.
        mount = f"type=bind,src={input_workspace},dst=/input,readonly"
        user = container_user or self._container_identity()[0]
        uid_text, gid_text = user.split(":", 1)
        workspace_tmpfs = (
            f"/workspace:rw,nosuid,nodev,size={self.workspace_limit_bytes},"
            f"uid={uid_text},gid={gid_text},mode=0700"
        )
        bootstrap = (
            "import os,pathlib,shutil,sys;"
            "src=pathlib.Path('/input');dst=pathlib.Path('/workspace');"
            "[(shutil.copytree(p,dst/p.name,symlinks=False) if p.is_dir() "
            "else shutil.copy2(p,dst/p.name,follow_symlinks=False)) for p in src.iterdir()];"
            "os.chdir(dst);os.execv(sys.executable,[sys.executable,'-E','-s',sys.argv[1]])"
        )
        return [
            self.docker_binary,
            "run",
            "--rm",
            "--pull",
            "never",
            "--network",
            "none",
            "--ipc",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            user,
            "--pids-limit",
            str(self.pids),
            "--ulimit",
            "nofile=128:128",
            "--ulimit",
            f"fsize={self.file_size_limit_bytes}:{self.file_size_limit_bytes}",
            "--ulimit",
            "core=0:0",
            "--memory",
            f"{self.memory_mb}m",
            "--cpus",
            str(self.cpus),
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,noexec,size=64m,mode=1777",
            "--tmpfs",
            workspace_tmpfs,
            "--mount",
            mount,
            "--workdir",
            "/workspace",
            "--env",
            "PYTHONIOENCODING=utf-8",
            "--env",
            "PYTHONUNBUFFERED=1",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            self.image,
            "python",
            "-I",
            "-s",
            "-c",
            bootstrap,
            entrypoint,
        ]

    def run(
        self,
        artifacts: list[dict[str, Any]],
        *,
        support_files: list[dict[str, Any]] | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        output_bytes: int = MAX_OUTPUT_BYTES,
    ) -> dict[str, Any]:
        if not self.available():
            return {"executed": False, "reason": "isolated_backend_unavailable", **self._base_evidence()}
        with tempfile.TemporaryDirectory(prefix="crowai-code-docker-") as temporary:
            root = Path(temporary).resolve()
            input_workspace = root / "input"
            input_workspace.mkdir(mode=0o700)
            entrypoint = _prepare_workspace(input_workspace, artifacts, support_files)
            container_user, workspace_uid, workspace_gid = self._container_identity()
            self._private_workspace_modes(input_workspace, uid=workspace_uid, gid=workspace_gid)
            if entrypoint is None:
                return {"executed": False, "reason": "no_python_artifact", **self._base_evidence()}
            if not entrypoint:
                return {"executed": False, "reason": "no_python_entrypoint", **self._base_evidence()}
            outcome = _run_to_files(
                self._command(input_workspace, entrypoint, container_user=container_user),
                cwd=root,
                env=_minimal_environment(),
                timeout_seconds=timeout_seconds,
                output_bytes=output_bytes,
                preexec_limits=False,
            )
            self._private_workspace_modes(input_workspace, uid=workspace_uid, gid=workspace_gid)
            return {
                "executed": True,
                "command_class": "python_generated_artifact",
                "entrypoint": entrypoint,
                **outcome,
                **self._base_evidence(enforced=True),
                "limits": {
                    "timeout_seconds": outcome["timeout_seconds"],
                    "output_bytes_per_stream": outcome["output_bytes_per_stream"],
                    "maximum_files": MAX_FILES,
                    "maximum_workspace_bytes": MAX_WORKSPACE_BYTES,
                    "environment_filtered": True,
                    "path_escape_blocked": True,
                    "symlink_escape_blocked": True,
                    "memory_mb": self.memory_mb,
                    "cpus": self.cpus,
                    "process_limit": self.pids,
                    "read_only_container_root": True,
                    "no_new_privileges": True,
                    "linux_capabilities_dropped": True,
                    "workspace_only_host_mount": False,
                    "input_only_host_mount": True,
                    "host_workspace_mount_read_only": True,
                    "workspace_writable_by_unprivileged_user": True,
                    "container_user": container_user,
                    "host_workspace_private_posix_modes": os.name == "posix",
                    "host_input_private_posix_modes": os.name == "posix",
                    "host_workspace_world_writable": False if os.name == "posix" else None,
                    "host_input_permissions_rehardened_after_run": os.name == "posix",
                    "ipc_isolated": True,
                    "open_file_limit": 128,
                    "individual_file_size_limit_bytes": self.file_size_limit_bytes,
                    "core_dump_limit_bytes": 0,
                    "aggregate_workspace_limit_enforced": True,
                    "aggregate_workspace_limit_backend": "tmpfs",
                    "aggregate_workspace_limit_bytes": self.workspace_limit_bytes,
                    "pull_policy": "never",
                    "network_isolated": True,
                    "host_filesystem_isolated": True,
                    "image_digest_pinned": self.image_digest_pinned,
                },
                "limitations": [
                    "Isolation relies on the locally installed Docker engine and its security boundary.",
                    "The configured image must already exist locally; CrowAI uses --pull never.",
                    "The writable /workspace is an in-container tmpfs with a hard size cap; the host input mount is read-only.",
                    *(
                        []
                        if self.image_digest_pinned
                        else ["The configured Docker image is tag-based; use an @sha256 digest for reproducible high-trust execution."]
                    ),
                ],
            }


def trusted_local_opt_in_enabled(config: dict[str, Any] | None = None) -> bool:
    configured = bool((config or {}).get("python_trusted_local_enabled", False))
    environment = os.getenv("CROWAI_CODE_TRUSTED_LOCAL_EXECUTION", "").strip().casefold() in {"1", "true", "yes", "on"}
    development = os.getenv("CROWAI_ENV", "development").strip().casefold() not in {"production", "prod"}
    return configured and environment and development


def select_backend(policy: dict[str, Any] | None, config: dict[str, Any] | None = None) -> RunnerBackend:
    policy = policy if isinstance(policy, dict) else {}
    config = config if isinstance(config, dict) else {}
    if policy.get("allow") is not True:
        return DisabledRunner()
    requested = str(policy.get("backend") or "isolated").strip().casefold()
    if requested == "isolated":
        backend = str(config.get("python_isolated_backend") or "docker").strip().casefold()
        if backend != "docker":
            return DisabledRunner()
        return DockerRunner(
            image=str(config.get("python_docker_image") or DEFAULT_DOCKER_IMAGE),
            memory_mb=int(config.get("python_runner_memory_mb", DEFAULT_MEMORY_MB)),
            cpus=float(config.get("python_runner_cpus", DEFAULT_CPUS)),
            pids=int(config.get("python_runner_process_limit", DEFAULT_PIDS)),
            file_size_limit_bytes=int(config.get("python_runner_file_size_limit_bytes", DEFAULT_FILE_SIZE_LIMIT_BYTES)),
            workspace_limit_bytes=int(config.get("python_runner_workspace_limit_bytes", DEFAULT_WORKSPACE_LIMIT_BYTES)),
        )
    if requested == "trusted-local" and trusted_local_opt_in_enabled(config):
        return TrustedLocalRunner(enabled=True)
    return DisabledRunner()


def execute_python_artifacts(
    artifacts: list[dict[str, Any]],
    *,
    execution_policy: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    support_files: list[dict[str, Any]] | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    output_bytes: int = MAX_OUTPUT_BYTES,
) -> dict[str, Any]:
    backend = select_backend(execution_policy, config)
    result = backend.run(
        artifacts,
        support_files=support_files,
        timeout_seconds=timeout_seconds,
        output_bytes=output_bytes,
    )
    result.setdefault("requested", bool(isinstance(execution_policy, dict) and execution_policy.get("allow") is True))
    return result
