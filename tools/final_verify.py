"""One-command CrowAI V1.0 release-candidate verification.

This orchestrator never deletes user files or cleans the working directory. It
validates a temporary clean source representation, then runs the same release
integrity checks that CI is expected to enforce. Every spawned child process has
an explicit timeout and is started in a killable process group/session.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Running the canonical verifier itself must not dirty the checkout with Python
# bytecode before it stages/validates source.  Pre-existing bytecode in a source
# tree remains a validation error; this only prevents the verifier process from
# manufacturing new contamination through its own local imports.
sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from crowai.version import CORE_VERSION
from tools.validate_release import validate_directory, validate_zip
from tools.source_policy import deterministic_file_mode, is_source_release_file

CORE_ZIP = ROOT / "dist" / f"CrowAI-Core-{CORE_VERSION}.zip"

FAST_CHECK_TIMEOUT = 90.0
STATIC_TOOL_TIMEOUT = 120.0
COMPILE_TIMEOUT = 120.0
PYTEST_TIMEOUT = 300.0
CORE_BUILD_TIMEOUT = 120.0
DOCKER_TIMEOUT = 180.0
MODEL_SMOKE_TIMEOUT = 900.0
DEFAULT_OVERALL_TIMEOUT = 1800.0


@dataclass(frozen=True)
class ProcessResult:
    """Raw child-process evidence, deliberately separate from check semantics."""

    command: list[str]
    returncode: int | None
    stdout: str
    duration_ms: int
    timeout_seconds: float
    timed_out: bool = False
    unavailable_reason: str = ""


def _check(
    name: str,
    status: str,
    *,
    required: bool,
    detail: str = "",
    command: list[str] | None = None,
    duration_ms: int = 0,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "required": required,
        "detail": detail[:4000],
        "command": command or [],
        "duration_ms": int(duration_ms),
        "timeout_seconds": timeout_seconds,
    }


def _remaining_timeout(requested: float, deadline: float | None) -> float:
    requested = max(0.05, float(requested))
    if deadline is None:
        return requested
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return 0.0
    return max(0.05, min(requested, remaining))


def _terminate_process_tree(process: subprocess.Popen[Any]) -> None:
    """Terminate a timed-out child and, where practical, its descendants."""
    if process.poll() is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            process.kill()
    elif os.name == "nt":
        taskkill = shutil.which("taskkill")
        if taskkill:
            try:
                subprocess.run(
                    [taskkill, "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        if process.poll() is None:
            process.kill()
    else:
        process.kill()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()


def _run_process(
    name: str,
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: Path = ROOT,
    timeout_seconds: float = STATIC_TOOL_TIMEOUT,
    deadline: float | None = None,
) -> ProcessResult:
    """Run one bounded child and return process facts without inventing PASS.

    The caller owns semantic interpretation.  In particular, exit code 0 can
    still mean SKIPPED or another non-PASS check outcome when the tool's output
    says so.  This function emits START progress only; exactly one final DONE
    line is emitted after semantic classification by the caller.
    """
    merged = os.environ.copy()
    merged["PYTHONDONTWRITEBYTECODE"] = "1"
    if env:
        merged.update(env)
    effective_timeout = _remaining_timeout(timeout_seconds, deadline)
    started = time.monotonic()
    if effective_timeout <= 0:
        print(f"[START] {name} (timeout=0s)", flush=True)
        return ProcessResult(
            command=command,
            returncode=None,
            stdout="Overall verifier deadline was exhausted before this check could start.",
            duration_ms=0,
            timeout_seconds=0.0,
            timed_out=True,
        )

    print(f"[START] {name} (timeout={effective_timeout:g}s)", flush=True)
    kwargs: dict[str, Any] = {
        "cwd": cwd,
        "env": merged,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "shell": False,
    }
    if os.name == "posix":
        kwargs["start_new_session"] = True
    elif os.name == "nt" and hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    try:
        process = subprocess.Popen(command, **kwargs)
    except OSError as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        return ProcessResult(
            command=command,
            returncode=None,
            stdout="",
            duration_ms=duration_ms,
            timeout_seconds=effective_timeout,
            unavailable_reason=str(exc),
        )

    timed_out = False
    output = ""
    try:
        output, _ = process.communicate(timeout=effective_timeout)
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        partial = exc.output or ""
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", errors="replace")
        _terminate_process_tree(process)
        try:
            tail, _ = process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            _terminate_process_tree(process)
            tail = ""
        output = str(partial) + str(tail or "")

    duration_ms = int((time.monotonic() - started) * 1000)
    if timed_out:
        return ProcessResult(
            command=command,
            returncode=process.returncode,
            stdout=output.strip(),
            duration_ms=duration_ms,
            timeout_seconds=effective_timeout,
            timed_out=True,
        )

    return ProcessResult(
        command=command,
        returncode=process.returncode,
        stdout=output.strip(),
        duration_ms=duration_ms,
        timeout_seconds=effective_timeout,
    )


def _done(name: str, result: dict[str, Any]) -> dict[str, Any]:
    suffix = f" ({result['duration_ms']}ms)" if result.get("duration_ms") is not None else ""
    print(f"[DONE] {name}: {result['status']}{suffix}", flush=True)
    return result


def _process_check(
    name: str,
    process: ProcessResult,
    *,
    required: bool,
    emit_done: bool = True,
) -> dict[str, Any]:
    if process.timed_out:
        detail = f"Timed out after {process.timeout_seconds:g}s; process tree terminated."
        if process.stdout:
            detail += "\n" + process.stdout[-3500:]
        result = _check(
            name,
            "TIMEOUT",
            required=required,
            detail=detail,
            command=process.command,
            duration_ms=process.duration_ms,
            timeout_seconds=process.timeout_seconds,
        )
    elif process.unavailable_reason:
        result = _check(
            name,
            "NOT_AVAILABLE",
            required=required,
            detail=process.unavailable_reason,
            command=process.command,
            duration_ms=process.duration_ms,
            timeout_seconds=process.timeout_seconds,
        )
    else:
        detail = f"exit={process.returncode}"
        if process.stdout:
            detail += "\n" + process.stdout[-3500:]
        result = _check(
            name,
            "PASS" if process.returncode == 0 else "FAIL",
            required=required,
            detail=detail,
            command=process.command,
            duration_ms=process.duration_ms,
            timeout_seconds=process.timeout_seconds,
        )
    return _done(name, result) if emit_done else result


def _run(
    name: str,
    command: list[str],
    *,
    required: bool = True,
    env: dict[str, str] | None = None,
    cwd: Path = ROOT,
    timeout_seconds: float = STATIC_TOOL_TIMEOUT,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Compatibility wrapper for checks whose semantics are exactly exit-code based."""
    process = _run_process(
        name,
        command,
        env=env,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        deadline=deadline,
    )
    return _process_check(name, process, required=required)


def _tool_check(
    name: str,
    command: list[str],
    *,
    allow_missing: bool,
    timeout_seconds: float = STATIC_TOOL_TIMEOUT,
    deadline: float | None = None,
) -> dict[str, Any]:
    executable = command[0]
    if shutil.which(executable) is None:
        result = _check(
            name,
            "NOT_AVAILABLE",
            required=not allow_missing,
            detail=f"Required executable is not installed: {executable}",
            command=command,
            timeout_seconds=timeout_seconds,
        )
        print(f"[DONE] {name}: NOT_AVAILABLE", flush=True)
        return result
    return _run(
        name,
        command,
        required=not allow_missing,
        timeout_seconds=timeout_seconds,
        deadline=deadline,
    )


def _flask_runtime_check(
    *,
    allow_missing: bool,
    deadline: float | None = None,
) -> dict[str, Any]:
    name = "flask_werkzeug_import"
    process = _run_process(
        name,
        [sys.executable, "-c", "import flask, werkzeug; print('Flask/Werkzeug import OK')"],
        timeout_seconds=FAST_CHECK_TIMEOUT,
        deadline=deadline,
    )
    result = _process_check(name, process, required=not allow_missing, emit_done=False)
    if result["status"] == "FAIL" and allow_missing:
        result["status"] = "NOT_AVAILABLE"
        result["required"] = False
    return _done(name, result)


def _pytest_coverage_check(
    *,
    allow_missing_runtime_deps: bool,
    coverage_gate: float,
    coverage_file: Path,
    deadline: float | None = None,
) -> dict[str, Any]:
    name = "pytest_branch_coverage"
    process = _run_process(
        name,
        [
            sys.executable, "-m", "pytest", "-q", "-ra", "-p", "no:cacheprovider", "-p", "pytest_cov",
            "--cov=crowai", "--cov=models", "--cov-branch", "--cov-report=term-missing",
            f"--cov-fail-under={coverage_gate}",
        ],
        env={"COVERAGE_FILE": str(coverage_file), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        timeout_seconds=PYTEST_TIMEOUT,
        deadline=deadline,
    )
    result = _process_check(
        name,
        process,
        required=not allow_missing_runtime_deps,
        emit_done=False,
    )
    flask_missing = "Flask is not installed" in process.stdout
    if flask_missing:
        result["semantic_reason"] = "flask_missing"
        if allow_missing_runtime_deps:
            result["status"] = "SKIPPED"
            result["required"] = False
        else:
            result["status"] = "FAIL"
            result["required"] = True
    return _done(name, result)


def _git_tracked_files(*, deadline: float | None = None) -> list[Path] | None:
    if shutil.which("git") is None or not (ROOT / ".git").exists():
        return None
    timeout = _remaining_timeout(30.0, deadline)
    if timeout <= 0:
        raise TimeoutError("Overall verifier deadline expired before git source staging.")
    kwargs: dict[str, Any] = {
        "cwd": ROOT,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "shell": False,
    }
    if os.name == "posix":
        kwargs["start_new_session"] = True
    elif os.name == "nt" and hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    process = subprocess.Popen(["git", "ls-files", "-z"], **kwargs)
    try:
        stdout, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _terminate_process_tree(process)
        raise TimeoutError("git ls-files timed out during clean source staging.") from exc
    if process.returncode != 0:
        return None
    output: list[Path] = []
    for raw in stdout.split(b"\x00"):
        if not raw:
            continue
        relative = Path(raw.decode("utf-8", errors="strict"))
        candidate = ROOT / relative
        if candidate.is_file() or candidate.is_symlink():
            output.append(relative)
    return sorted(output, key=lambda item: item.as_posix())


def _fallback_source_files() -> list[Path]:
    output: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() and not path.is_symlink():
            continue
        relative = path.relative_to(ROOT)
        if not is_source_release_file(relative):
            continue
        output.append(relative)
    return sorted(output, key=lambda item: item.as_posix())


def _stage_clean_source(destination: Path, *, deadline: float | None = None) -> tuple[Path, str]:
    source_root = destination / "source"
    source_root.mkdir(parents=True, exist_ok=True)
    tracked = _git_tracked_files(deadline=deadline)
    files = tracked if tracked is not None else _fallback_source_files()
    method = "git-tracked" if tracked is not None else "explicit-source-rules"
    for relative in files:
        source = ROOT / relative
        if source.is_symlink():
            raise RuntimeError(f"source symlink is not permitted in the release candidate: {relative.as_posix()}")
        target = source_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        try:
            target.chmod(stat.S_IMODE(source.stat().st_mode))
        except OSError:
            pass
    return source_root, method


def _zip_info(name: str, mode: int | None = None) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (deterministic_file_mode(name) if mode is None else (stat.S_IFREG | mode)) << 16
    return info


def _build_source_bundle(source_root: Path, output: Path) -> None:
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(item for item in source_root.rglob("*") if item.is_file()):
            relative = path.relative_to(source_root).as_posix()
            archive.writestr(
                _zip_info("crowai/" + relative, 0o755 if relative == "run_linux.sh" else 0o644),
                path.read_bytes(),
            )


def _tamper_core_release(source: Path, output: Path) -> None:
    with zipfile.ZipFile(source) as archive:
        infos = archive.infolist()
        payloads = {info.filename: archive.read(info) for info in infos if not info.is_dir()}
    if "README.md" not in payloads:
        raise RuntimeError("Core release does not contain README.md for the tamper regression.")
    payloads["README.md"] += b"\nTAMPER-REGRESSION\n"
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(payloads):
            archive.writestr(_zip_info(name), payloads[name])


def _tamper_core_metadata(source: Path, output: Path) -> None:
    """Change only README permissions while preserving all covered bytes."""
    with zipfile.ZipFile(source) as archive:
        payloads = {info.filename: archive.read(info) for info in archive.infolist() if not info.is_dir()}
    if "README.md" not in payloads:
        raise RuntimeError("Core release does not contain README.md for the metadata tamper regression.")
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(payloads):
            archive.writestr(_zip_info(name, 0o777 if name == "README.md" else None), payloads[name])


def _core_truth_errors(path: Path) -> list[str]:
    errors: list[str] = []
    with zipfile.ZipFile(path) as archive:
        names = {info.filename.rstrip("/") for info in archive.infolist() if info.filename.rstrip("/")}
        if "PACKAGE_MANIFEST.json" in names:
            errors.append("Core ZIP contains source PACKAGE_MANIFEST.json")
        if any(name.startswith(("models/chat/v1.0/", "models/code/v1.0/", "models/agent/v1.0/")) for name in names):
            errors.append("Core ZIP contains V1.0 mode implementation source")
        try:
            manifest = json.loads(archive.read("RELEASE_MANIFEST.json"))
        except Exception as exc:
            return errors + [f"Core RELEASE_MANIFEST.json could not be parsed: {exc}"]
    expected = {
        "artifact_type": "core-release",
        "model_packages_included": False,
        "model_binaries_included": False,
        "native_runtime_binaries_included": False,
        "runtime_user_data_included": False,
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            errors.append(f"Core manifest claim mismatch: {key}")
    return errors


def _process_problem(
    name: str,
    process: ProcessResult,
    *,
    required: bool,
) -> dict[str, Any] | None:
    """Translate only process-level failure into a semantic check result.

    A successful exit is intentionally represented by ``None`` rather than an
    intermediate PASS.  Semantic checks such as real-model smoke must inspect
    their machine-readable evidence before any PASS/SKIPPED/FAIL status exists
    or is printed.  This makes it structurally impossible to stream a temporary
    ``PASS`` merely because a child exited zero.
    """
    if process.timed_out:
        detail = f"Timed out after {process.timeout_seconds:g}s; process tree terminated."
        if process.stdout:
            detail += "\n" + process.stdout[-3500:]
        return _check(
            name, "TIMEOUT", required=required, detail=detail, command=process.command,
            duration_ms=process.duration_ms, timeout_seconds=process.timeout_seconds,
        )
    if process.unavailable_reason:
        return _check(
            name, "NOT_AVAILABLE", required=required, detail=process.unavailable_reason,
            command=process.command, duration_ms=process.duration_ms,
            timeout_seconds=process.timeout_seconds,
        )
    if process.returncode != 0:
        detail = f"exit={process.returncode}"
        if process.stdout:
            detail += "\n" + process.stdout[-3500:]
        return _check(
            name, "FAIL", required=required, detail=detail, command=process.command,
            duration_ms=process.duration_ms, timeout_seconds=process.timeout_seconds,
        )
    return None


def _model_smoke(required: bool, *, deadline: float | None = None) -> dict[str, Any]:
    process = _run_process(
        "real_model_smoke",
        [sys.executable, "tools/smoke_models.py"],
        timeout_seconds=MODEL_SMOKE_TIMEOUT,
        deadline=deadline,
    )
    process_problem = _process_problem("real_model_smoke", process, required=required)
    if process_problem is not None:
        return _done("real_model_smoke", process_problem)

    output = process.stdout
    try:
        json_start = output.find("{")
        payload = json.loads(output[json_start:]) if json_start >= 0 else {}
        rows = payload.get("real_model_smoke", [])
    except (json.JSONDecodeError, AttributeError):
        return _done("real_model_smoke", _check(
            "real_model_smoke",
            "FAIL",
            required=required,
            detail="Smoke tool output was not machine-readable.",
            duration_ms=process.duration_ms,
            timeout_seconds=process.timeout_seconds,
        ))
    if any(row.get("result") == "FAIL" for row in rows if isinstance(row, dict)):
        return _done("real_model_smoke", _check(
            "real_model_smoke", "FAIL", required=required,
            detail=json.dumps(rows, ensure_ascii=False),
            duration_ms=process.duration_ms, timeout_seconds=process.timeout_seconds,
        ))
    skipped = [row for row in rows if isinstance(row, dict) and row.get("result") == "SKIPPED"]
    if skipped:
        return _done("real_model_smoke", _check(
            "real_model_smoke", "SKIPPED", required=required,
            detail=json.dumps(skipped, ensure_ascii=False),
            duration_ms=process.duration_ms, timeout_seconds=process.timeout_seconds,
        ))
    return _done("real_model_smoke", _check(
        "real_model_smoke", "PASS", required=required,
        detail=json.dumps(rows, ensure_ascii=False),
        duration_ms=process.duration_ms, timeout_seconds=process.timeout_seconds,
    ))


def _docker_isolation(required: bool, *, deadline: float | None = None) -> dict[str, Any]:
    if shutil.which("docker") is None:
        return _done("docker_isolation", _check(
            "docker_isolation",
            "NOT_AVAILABLE",
            required=required,
            detail="Docker is not installed.",
            timeout_seconds=DOCKER_TIMEOUT,
        ))
    process = _run_process(
        "docker_isolation",
        [
            sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
            "tests/unit/test_final_hardening.py", "-k", "real_docker_backend",
        ],
        timeout_seconds=DOCKER_TIMEOUT,
        deadline=deadline,
    )
    process_check = _process_check("docker_isolation", process, required=required, emit_done=False)
    if process_check["status"] != "PASS":
        return _done("docker_isolation", process_check)
    lowered = process.stdout.casefold()
    if "skipped" in lowered and re.search(r"\b\d+\s+skipped\b", lowered):
        return _done("docker_isolation", _check(
            "docker_isolation", "SKIPPED", required=required,
            detail=process.stdout[-3500:], command=process.command,
            duration_ms=process.duration_ms, timeout_seconds=process.timeout_seconds,
        ))
    return _done("docker_isolation", process_check)


def _summarize(
    checks: list[dict[str, Any]],
    *,
    started: float,
    coverage_gate: float,
    overall_timeout_seconds: float,
) -> tuple[dict[str, Any], int]:
    failures = [item for item in checks if item["required"] and item["status"] != "PASS"]
    duration_ms = int((time.monotonic() - started) * 1000)
    if duration_ms > int(overall_timeout_seconds * 1000) and not any(
        item["name"] == "overall_deadline" for item in checks
    ):
        deadline_check = _check(
            "overall_deadline",
            "TIMEOUT",
            required=True,
            detail=f"Verifier exceeded the configured {overall_timeout_seconds:g}s overall deadline.",
            duration_ms=duration_ms,
            timeout_seconds=overall_timeout_seconds,
        )
        checks.append(deadline_check)
        failures.append(deadline_check)
    external_checks = {
        item["name"]: item["status"]
        for item in checks
        if item["name"] in {"docker_isolation", "real_model_smoke"}
    }
    # This local orchestrator deliberately tops out at RELEASE CANDIDATE.
    # A single machine cannot prove the repository's external GitHub Actions
    # matrix, so READY FOR PORTFOLIO V1.0 is a manual release decision made
    # only after the documented GitHub CI, Docker, and real-model evidence is
    # reviewed together. Do not promote the automatic verdict here merely
    # because the local Docker/model checks happen to be required and PASS.
    verdict = "NOT READY" if failures else "RELEASE CANDIDATE"
    summary = {
        "schema_version": 2,
        "product": "CrowAI V1.0",
        "core_version": CORE_VERSION,
        "overall": "PASS" if not failures else "FAIL",
        "verdict": verdict,
        "duration_ms": duration_ms,
        "overall_timeout_seconds": overall_timeout_seconds,
        "coverage_gate": coverage_gate,
        "checks": checks,
        "required_failures": [item["name"] for item in failures],
        "external_evidence": external_checks,
    }
    return summary, 0 if not failures else 1


def _git_commit() -> str:
    if not (ROOT / ".git").exists() or shutil.which("git") is None:
        return ""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    value = result.stdout.strip().casefold()
    return value if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{40,64}", value) else ""


def _pytest_metrics(detail: str) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for label in ("passed", "failed", "skipped"):
        match = re.search(rf"\b(\d+)\s+{label}\b", detail)
        if match:
            metrics[label] = int(match.group(1))
    exact_total = re.search(r"Total coverage:\s*([0-9]+(?:\.[0-9]+)?)%", detail)
    if exact_total:
        metrics["coverage_branch_aware"] = float(exact_total.group(1))
    else:
        rounded_total = re.search(
            r"(?m)^TOTAL\s+\d+\s+\d+\s+\d+\s+\d+\s+([0-9]+(?:\.[0-9]+)?)%",
            detail,
        )
        if rounded_total:
            metrics["coverage_branch_aware"] = float(rounded_total.group(1))
    return metrics


def _safe_model_smoke(detail: str) -> list[dict[str, Any]]:
    try:
        rows = json.loads(detail)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(rows, list):
        return []
    safe: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        record: dict[str, Any] = {
            "model_id": str(row.get("model_id") or ""),
            "result": str(row.get("result") or ""),
        }
        for key in ("reason", "missing_requirements", "invalid_requirements", "alias_verified"):
            if key in row:
                record[key] = row[key]
        safe.append(record)
    return safe


def _public_summary(summary: dict[str, Any]) -> dict[str, Any]:
    """Return machine-readable release evidence without local paths or secrets."""
    checks: dict[str, dict[str, Any]] = {}
    raw_checks = summary.get("checks", [])
    for item in raw_checks:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        record: dict[str, Any] = {
            "status": str(item.get("status") or ""),
            "required": bool(item.get("required")),
            "duration_ms": int(item.get("duration_ms") or 0),
            "timeout_seconds": item.get("timeout_seconds"),
        }
        if name == "pytest_branch_coverage":
            record.update(_pytest_metrics(str(item.get("detail") or "")))
        elif name == "real_model_smoke":
            record["models"] = _safe_model_smoke(str(item.get("detail") or ""))
        elif name == "core_deterministic":
            hashes = re.findall(r"\b[0-9a-f]{64}\b", str(item.get("detail") or "").casefold())
            if hashes:
                record["sha256"] = hashes[0]
            record["deterministic"] = item.get("status") == "PASS"
        checks[name] = record

    return {
        "schema_version": 1,
        "product_version": "V1.0",
        "core_version": CORE_VERSION,
        "git_commit": _git_commit(),
        "timestamp_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "coverage_gate": summary.get("coverage_gate"),
        "checks": checks,
        "overall_status": summary.get("overall"),
        "verdict": summary.get("verdict"),
        "required_failures": list(summary.get("required_failures") or []),
    }


def verify(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    checks: list[dict[str, Any]] = []
    started = time.monotonic()
    overall_timeout = max(1.0, float(getattr(args, "overall_timeout", DEFAULT_OVERALL_TIMEOUT)))
    deadline = started + overall_timeout

    with tempfile.TemporaryDirectory(prefix="crowai-final-verify-") as temporary:
        temp = Path(temporary)

        # Stage and validate source first.  Later tooling is allowed to create
        # local caches, but release-readiness evidence is always based on this
        # clean representation rather than on a checkout dirtied by testing.
        stage_started = time.monotonic()
        print("[START] clean_source_validation", flush=True)
        try:
            clean_source, method = _stage_clean_source(temp, deadline=deadline)
            source_errors = validate_directory(clean_source, policy="source-tree")
            stage_status = "PASS" if not source_errors else "FAIL"
            stage_result = _check(
                "clean_source_validation", stage_status, required=True,
                detail=f"method={method}; errors={source_errors}",
                duration_ms=int((time.monotonic() - stage_started) * 1000),
            )
            checks.append(stage_result)
            _done("clean_source_validation", stage_result)

            source_bundle = temp / "crowai-source.zip"
            bundle_started = time.monotonic()
            print("[START] source_bundle_manifest_integrity", flush=True)
            _build_source_bundle(clean_source, source_bundle)
            bundle_errors = validate_zip(source_bundle, policy="source-bundle")
            bundle_status = "PASS" if not bundle_errors else "FAIL"
            bundle_result = _check(
                "source_bundle_manifest_integrity", bundle_status, required=True,
                detail=str(bundle_errors), duration_ms=int((time.monotonic() - bundle_started) * 1000),
            )
            checks.append(bundle_result)
            _done("source_bundle_manifest_integrity", bundle_result)
        except TimeoutError as exc:
            stage_result = _check(
                "clean_source_validation", "TIMEOUT", required=True, detail=str(exc),
                duration_ms=int((time.monotonic() - stage_started) * 1000), timeout_seconds=30.0,
            )
            checks.append(stage_result)
            _done("clean_source_validation", stage_result)
            bundle_result = _check(
                "source_bundle_manifest_integrity", "SKIPPED", required=True,
                detail="Source staging timed out.",
            )
            checks.append(bundle_result)
            _done("source_bundle_manifest_integrity", bundle_result)
        except Exception as exc:
            stage_result = _check(
                "clean_source_validation", "FAIL", required=True, detail=str(exc),
                duration_ms=int((time.monotonic() - stage_started) * 1000),
            )
            checks.append(stage_result)
            _done("clean_source_validation", stage_result)
            bundle_result = _check(
                "source_bundle_manifest_integrity", "FAIL", required=True,
                detail="Source staging failed.",
            )
            checks.append(bundle_result)
            _done("source_bundle_manifest_integrity", bundle_result)

        checks.append(_run(
            "dependency_metadata", [sys.executable, "tools/check_dependencies.py"],
            timeout_seconds=FAST_CHECK_TIMEOUT, deadline=deadline,
        ))
        checks.append(_run(
            "format", [sys.executable, "tools/check_format.py"],
            timeout_seconds=FAST_CHECK_TIMEOUT, deadline=deadline,
        ))
        checks.append(_tool_check(
            "ruff", ["ruff", "check", "."], allow_missing=args.allow_missing_tooling,
            timeout_seconds=STATIC_TOOL_TIMEOUT, deadline=deadline,
        ))
        checks.append(_tool_check(
            "mypy", ["mypy", "crowai", "tools"], allow_missing=args.allow_missing_tooling,
            timeout_seconds=STATIC_TOOL_TIMEOUT, deadline=deadline,
        ))

        checks.append(_flask_runtime_check(
            allow_missing=args.allow_missing_runtime_deps,
            deadline=deadline,
        ))

        for model_id in ("chat", "code", "agent"):
            checks.append(_run(
                f"model_validator_{model_id}",
                [sys.executable, "-m", "tools.validate_model_package", f"models/{model_id}/v1.0"],
                timeout_seconds=FAST_CHECK_TIMEOUT,
                deadline=deadline,
            ))

        pycache = temp / "pycache"
        checks.append(_run(
            "compileall",
            [sys.executable, "-m", "compileall", "-q", "crowai", "models", "tools", "tests"],
            env={"PYTHONPYCACHEPREFIX": str(pycache)},
            timeout_seconds=COMPILE_TIMEOUT,
            deadline=deadline,
        ))
        checks.append(_tool_check(
            "workspace_js_syntax", ["node", "--check", "static/workspace.js"],
            allow_missing=args.allow_missing_tooling,
            timeout_seconds=FAST_CHECK_TIMEOUT,
            deadline=deadline,
        ))

        coverage_file = temp / ".coverage"
        pytest_result = _pytest_coverage_check(
            allow_missing_runtime_deps=args.allow_missing_runtime_deps,
            coverage_gate=args.coverage_gate,
            coverage_file=coverage_file,
            deadline=deadline,
        )
        checks.append(pytest_result)
        flask_skip = pytest_result.get("semantic_reason") == "flask_missing"
        flask_skip_result = _check(
            "no_flask_missing_skips",
            "FAIL" if flask_skip and not args.allow_missing_runtime_deps else ("SKIPPED" if flask_skip else "PASS"),
            required=not args.allow_missing_runtime_deps,
            detail="Flask-missing skip detected in pytest output." if flask_skip else "No Flask-missing skip detected.",
        )
        checks.append(flask_skip_result)
        _done("no_flask_missing_skips", flask_skip_result)

        CORE_ZIP.unlink(missing_ok=True)
        first_build = _run(
            "core_build_default", [sys.executable, "tools/build_release.py"],
            timeout_seconds=CORE_BUILD_TIMEOUT, deadline=deadline,
        )
        checks.append(first_build)
        first_hash = ""
        first_copy = temp / "core-first.zip"
        if first_build["status"] == "PASS" and CORE_ZIP.is_file():
            first_hash = hashlib.sha256(CORE_ZIP.read_bytes()).hexdigest()
            shutil.copyfile(CORE_ZIP, first_copy)

        second_output = temp / "core-custom.zip"
        second_build = _run(
            "core_build_custom_output",
            [sys.executable, "tools/build_release.py", "--output", str(second_output)],
            timeout_seconds=CORE_BUILD_TIMEOUT,
            deadline=deadline,
        )
        checks.append(second_build)
        second_hash = (
            hashlib.sha256(second_output.read_bytes()).hexdigest()
            if second_build["status"] == "PASS" and second_output.is_file()
            else ""
        )
        deterministic = bool(
            first_hash
            and first_hash == second_hash
            and first_copy.is_file()
            and second_output.is_file()
            and first_copy.read_bytes() == second_output.read_bytes()
        )
        deterministic_result = _check(
            "core_deterministic", "PASS" if deterministic else "FAIL", required=True,
            detail=f"default={first_hash}; custom={second_hash}",
        )
        checks.append(deterministic_result)
        _done("core_deterministic", deterministic_result)

        if CORE_ZIP.is_file():
            print("[START] core_manifest_integrity", flush=True)
            core_errors = validate_zip(CORE_ZIP, policy="core-release")
            core_manifest_result = _check(
                "core_manifest_integrity", "PASS" if not core_errors else "FAIL", required=True,
                detail=str(core_errors),
            )
            checks.append(core_manifest_result)
            _done("core_manifest_integrity", core_manifest_result)
            print("[START] core_artifact_truthfulness", flush=True)
            truth_errors = _core_truth_errors(CORE_ZIP)
            truth_result = _check(
                "core_artifact_truthfulness", "PASS" if not truth_errors else "FAIL", required=True,
                detail=str(truth_errors),
            )
            checks.append(truth_result)
            _done("core_artifact_truthfulness", truth_result)
            print("[START] tampered_core_rejected", flush=True)
            tampered = temp / "core-tampered.zip"
            _tamper_core_release(CORE_ZIP, tampered)
            tamper_errors = validate_zip(tampered, policy="core-release")
            caught = any("manifest sha256 mismatch: README.md" in item for item in tamper_errors)
            tamper_result = _check(
                "tampered_core_rejected", "PASS" if caught else "FAIL", required=True,
                detail=str(tamper_errors),
            )
            checks.append(tamper_result)
            _done("tampered_core_rejected", tamper_result)
            print("[START] tampered_core_metadata_rejected", flush=True)
            metadata_tampered = temp / "core-metadata-tampered.zip"
            _tamper_core_metadata(CORE_ZIP, metadata_tampered)
            metadata_errors = validate_zip(metadata_tampered, policy="core-release")
            metadata_caught = any(
                "non-canonical file mode for Core entry: README.md" in item
                for item in metadata_errors
            )
            metadata_result = _check(
                "tampered_core_metadata_rejected",
                "PASS" if metadata_caught else "FAIL",
                required=True,
                detail=str(metadata_errors),
            )
            checks.append(metadata_result)
            _done("tampered_core_metadata_rejected", metadata_result)
        else:
            for name in (
                "core_manifest_integrity",
                "core_artifact_truthfulness",
                "tampered_core_rejected",
                "tampered_core_metadata_rejected",
            ):
                checks.append(_check(name, "FAIL", required=True, detail="Core ZIP was not produced."))

        checks.append(_model_smoke(args.require_model_smoke, deadline=deadline))
        checks.append(_docker_isolation(args.require_docker, deadline=deadline))

    return _summarize(
        checks,
        started=started,
        coverage_gate=args.coverage_gate,
        overall_timeout_seconds=overall_timeout,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the final CrowAI V1.0 verification sequence without cleaning the working tree."
    )
    parser.add_argument(
        "--coverage-gate", type=float, default=68.0,
        help="Branch-aware coverage.py total gate (default: 68).",
    )
    parser.add_argument(
        "--overall-timeout", type=float, default=DEFAULT_OVERALL_TIMEOUT,
        help="Maximum total verifier duration in seconds (default: 1800).",
    )
    parser.add_argument(
        "--allow-missing-tooling", action="store_true",
        help="Mark missing Ruff/mypy/Node as NOT_AVAILABLE instead of required failures.",
    )
    parser.add_argument(
        "--allow-missing-runtime-deps", action="store_true",
        help="Allow a constrained review environment without Flask/Werkzeug; never use this mode as CI evidence.",
    )
    parser.add_argument(
        "--require-docker", action="store_true",
        help="Require the real Docker isolation test to execute and pass.",
    )
    parser.add_argument(
        "--require-model-smoke", action="store_true",
        help="Require real local GGUF smoke checks instead of allowing source-only SKIPPED results.",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=Path("dist/verification-summary.json"),
        help="Write sanitized machine-readable evidence here (default: dist/verification-summary.json).",
    )
    args = parser.parse_args(argv)
    if args.overall_timeout <= 0:
        parser.error("--overall-timeout must be greater than zero")

    summary, exit_code = verify(args)
    print("\nCrowAI V1.0 final verification")
    print("=" * 34)
    for item in summary["checks"]:
        marker = "*" if item["required"] else "-"
        print(
            f"{marker} {item['status']:<13} {item['name']} "
            f"({item['duration_ms']}ms, timeout={item['timeout_seconds']})"
        )
    print(f"Overall: {summary['overall']}")
    print(f"Verdict: {summary['verdict']}")
    if summary["required_failures"]:
        print("Required failures: " + ", ".join(summary["required_failures"]))

    public_summary = _public_summary(summary)
    machine = json.dumps(public_summary, ensure_ascii=False, sort_keys=True)
    print("FINAL_VERIFY_JSON=" + machine)
    if args.json_out:
        target = args.json_out.expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(public_summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
