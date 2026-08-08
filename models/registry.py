from __future__ import annotations

import ast
import hashlib
import importlib
import importlib.util
import json
import logging
import os
import re
import sys
import threading
import shutil
from dataclasses import dataclass
from pathlib import Path

from models.local_files import local_file_state, package_local_file, runtime_candidates
from types import ModuleType
from typing import Any

from crowai.version import CORE_VERSION
SUPPORTED_CONTRACT_VERSION = 1
MODEL_SHUTDOWN_TIMEOUT_SECONDS = 20.0
_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_KNOWN_CAPABILITIES = {
    "attachments",
    "code",
    "conversation",
    "direct_code_generation",
    "document_analysis",
    "file_inspection",
    "follow_up_editing",
    "language_matching",
    "multi_file",
    "multimodal",
    "network",
    "no_web",
    "product_comparison",
    "project_generation",
    "project_memory",
    "repair_pass",
    "safe_python_runner",
    "python_execution",
    "isolated_python_runner",
    "structured_code_task",
    "syntax_validation",
    "tools",
    "vision",
    "web_search",
}
_LOG = logging.getLogger(__name__)


class ModelError(RuntimeError):
    """A safe model error suitable for returning to the interface."""


class ModelInputError(ModelError):
    """A package-declared input validation error safe to show to the user."""


@dataclass(frozen=True)
class ModelIssue:
    package: str
    code: str
    message: str
    severity: str = "error"

    def public(self) -> dict[str, str]:
        return {
            "package": self.package,
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
        }


@dataclass(frozen=True)
class Descriptor:
    id: str
    mode: str
    local_id: str
    name: str
    version: str
    description: str
    capabilities: tuple[str, ...]
    directory: Path
    order: int
    contract_version: int
    minimum_core_version: str

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "mode": self.mode,
            "local_id": self.local_id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "capabilities": list(self.capabilities),
            "order": self.order,
            "model_contract_version": self.contract_version,
            "minimum_core_version": self.minimum_core_version,
        }


@dataclass(frozen=True)
class ModeDescriptor:
    id: str
    name: str
    description: str
    order: int


def _version_tuple(value: str) -> tuple[int, ...]:
    match = re.fullmatch(r"\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-+].*)?\s*", value)
    if not match:
        raise ValueError("Version must use semantic numeric form.")
    return tuple(int(part or 0) for part in match.groups())




def _validate_package_locality(version_dir: Path) -> None:
    """Reject model package configuration that references files outside v1.0.

    Model packages may use the network when their mode allows it, but all runtime,
    GGUF, projector, prompt, provider/site configuration and package state files
    referenced by config.json must remain inside the version directory.
    """
    config_path = version_dir / "config.json"
    if not config_path.is_file():
        return
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("config.json is invalid.") from exc
    if not isinstance(config, dict):
        raise ValueError("config.json must contain an object.")

    root = version_dir.resolve()
    for raw_key, raw_value in config.items():
        key = str(raw_key)
        if not key.endswith("_file") or not isinstance(raw_value, str):
            continue
        value = raw_value.strip()
        if not value:
            continue
        relative = Path(value)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"Configuration field '{key}' must use a package-local relative path.")
        candidate = (root / relative).resolve()
        if candidate != root and root not in candidate.parents:
            raise ValueError(f"Configuration field '{key}' escapes the model package.")
        if key == "runtime_file":
            runtime_root = (root / "runtime").resolve()
            if candidate.parent != runtime_root and runtime_root not in candidate.parents:
                raise ValueError("The runtime must be stored under the package runtime directory.")
        if key in {"model_file", "mmproj_file"}:
            model_root = (root / "model").resolve()
            if candidate.parent != model_root and model_root not in candidate.parents:
                raise ValueError("Model weights/projectors must be stored under the package model directory.")

def _validate_callbacks_static(version_dir: Path) -> None:
    """Validate that the package entry point exposes the required names without importing it.

    The callbacks may be defined directly or re-exported from a package-local module.
    Runtime callability is checked later, when the selected trusted package is imported.
    """
    source_path = version_dir / "__init__.py"
    try:
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    except (OSError, SyntaxError) as exc:
        raise ValueError("Package __init__.py is invalid.") from exc

    exposed: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            exposed.add(node.name)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    exposed.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                exposed.add(alias.asname or alias.name.split(".", 1)[0])
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    exposed.add(target.id)

    missing = {"prepare_request", "finalize_result"} - exposed
    if missing:
        raise ValueError("Required package callbacks are missing.")


def _sanitize_public(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        return None
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for raw_key, raw_value in list(value.items())[:200]:
            key = str(raw_key)[:120]
            lowered = key.casefold()
            if lowered in {"local_path", "stored_path", "filesystem_path", "absolute_path"} or lowered.endswith("_path"):
                continue
            if lowered == "path":
                raw_path = str(raw_value or "").replace("\\", "/")
                parts = [part for part in raw_path.split("/") if part]
                if raw_path.startswith("/") or re.match(r"^[A-Za-z]:/", raw_path) or ".." in parts:
                    continue
            output[key] = _sanitize_public(raw_value, depth=depth + 1)
        return output
    if isinstance(value, (list, tuple)):
        return [_sanitize_public(item, depth=depth + 1) for item in list(value)[:500]]
    if isinstance(value, str):
        return value[:500_000]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:1000]


def _bounded_string(manifest: dict[str, Any], key: str, *, maximum: int, required: bool = True) -> str:
    value = str(manifest.get(key) or "").strip()
    if required and not value:
        raise ValueError(f"Manifest field '{key}' is required.")
    if len(value) > maximum:
        raise ValueError(f"Manifest field '{key}' exceeds {maximum} characters.")
    return value


class ModelRegistry:
    """Discover and validate packages under models/<mode>/<version>."""

    def __init__(
        self,
        root: Path,
        *,
        development: bool | None = None,
        strict_capabilities: bool | None = None,
    ) -> None:
        self.root = root.resolve()
        self.development = (
            development
            if development is not None
            else os.getenv("CROWAI_DEVELOPMENT", "").strip().lower() in {"1", "true", "yes"}
        )
        self.strict_capabilities = (
            strict_capabilities
            if strict_capabilities is not None
            else os.getenv("CROWAI_STRICT_MODEL_CAPABILITIES", os.getenv("CROWAI_STRICT_CAPABILITIES", "")).strip().lower() in {"1", "true", "yes"}
        )
        self._descriptors: dict[str, Descriptor] = {}
        self._modes: dict[str, ModeDescriptor] = {}
        self._modules: dict[str, tuple[str, ModuleType]] = {}
        self._issues: list[ModelIssue] = []
        self._lock = threading.RLock()
        self.refresh()

    def _package_label(self, mode: str, version: str | None = None) -> str:
        return f"{mode}/{version}" if version else mode

    def _mode_metadata(self, mode_dir: Path, mode: str) -> ModeDescriptor:
        fallback = ModeDescriptor(mode, mode.replace("_", " ").replace("-", " ").title(), "", 100)
        path = mode_dir / "mode.json"
        if not path.is_file():
            return fallback
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("mode.json must contain an object.")
            identifier = str(data.get("id") or mode).strip().lower()
            if identifier != mode:
                raise ValueError("Mode id must match its directory.")
            name = _bounded_string(data, "name", maximum=80)
            description = _bounded_string(data, "description", maximum=500, required=False)
            order = int(data.get("display_order", 100))
            return ModeDescriptor(mode, name, description, order)
        except Exception:
            _LOG.exception("Invalid mode metadata in %s", path)
            self._issues.append(ModelIssue(mode, "invalid_mode_metadata", "Mode metadata is invalid; safe defaults are being used.", "warning"))
            return fallback

    def _parse_descriptor(self, mode: str, version_dir: Path) -> Descriptor:
        manifest_path = version_dir / "manifest.json"
        module_path = version_dir / "__init__.py"
        if not manifest_path.is_file() or not module_path.is_file():
            raise ValueError("Both manifest.json and __init__.py are required.")
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("manifest.json must contain an object.")
        _validate_package_locality(version_dir)
        _validate_callbacks_static(version_dir)

        local_id = _bounded_string(data, "id", maximum=64).lower()
        if local_id != version_dir.name.lower() or not _PART.fullmatch(local_id):
            raise ValueError("Manifest id must match the version directory.")

        name = _bounded_string(data, "name", maximum=100)
        version = _bounded_string(data, "version", maximum=40)
        description = _bounded_string(data, "description", maximum=500)
        contract_version = int(data.get("model_contract_version", 0))
        if contract_version != SUPPORTED_CONTRACT_VERSION:
            raise ValueError(f"Unsupported model contract version {contract_version}.")
        minimum_core = _bounded_string(data, "minimum_core_version", maximum=40)
        if _version_tuple(minimum_core) > _version_tuple(CORE_VERSION):
            raise ValueError(f"This package requires CrowAI Core {minimum_core} or newer.")

        raw_checksums = data.get("files_sha256", {})
        if raw_checksums is not None and not isinstance(raw_checksums, dict):
            raise ValueError("Manifest files_sha256 must be an object when provided.")
        for raw_name, raw_digest in (raw_checksums or {}).items():
            relative = Path(str(raw_name))
            digest = str(raw_digest).strip().lower()
            if relative.is_absolute() or ".." in relative.parts or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("Manifest contains invalid integrity metadata.")
            candidate = (version_dir / relative).resolve()
            if version_dir.resolve() not in candidate.parents or not candidate.is_file():
                raise ValueError("Manifest integrity file is unavailable.")
            actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
            if actual != digest:
                raise ValueError("Manifest integrity check failed.")

        raw_capabilities = data.get("capabilities")
        if not isinstance(raw_capabilities, list):
            raise ValueError("Manifest capabilities must be a list.")
        capabilities: list[str] = []
        unknown: list[str] = []
        for raw in raw_capabilities:
            value = str(raw).strip().lower()
            if not value or len(value) > 80 or not _PART.fullmatch(value):
                raise ValueError("Manifest contains an invalid capability.")
            if value not in _KNOWN_CAPABILITIES:
                unknown.append(value)
                continue
            if value not in capabilities:
                capabilities.append(value)
        if unknown and self.strict_capabilities:
            raise ValueError("Manifest contains unsupported capabilities.")
        if unknown:
            self._issues.append(
                ModelIssue(
                    self._package_label(mode, local_id),
                    "unknown_capability",
                    "Unknown capabilities were ignored in development-compatible mode.",
                    "warning",
                )
            )

        order = int(data.get("display_order", 100))
        if order < -100_000 or order > 100_000:
            raise ValueError("Manifest display_order is outside the supported range.")

        return Descriptor(
            id=f"{mode}/{local_id}",
            mode=mode,
            local_id=local_id,
            name=name,
            version=version,
            description=description,
            capabilities=tuple(capabilities),
            directory=version_dir.resolve(),
            order=order,
            contract_version=contract_version,
            minimum_core_version=minimum_core,
        )

    def _fingerprint(self, descriptor: Descriptor) -> str:
        digest = hashlib.sha256()
        candidates = [
            path for path in descriptor.directory.rglob("*")
            if path.is_file() and path.suffix.casefold() in {".py", ".json", ".txt", ".md"}
        ]
        for path in sorted(candidates):
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            digest.update(path.relative_to(descriptor.directory).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(hashlib.sha256(raw).digest())
        return digest.hexdigest()

    @staticmethod
    def _package_name(descriptor: Descriptor) -> str:
        digest = hashlib.sha256(str(descriptor.directory).encode()).hexdigest()[:16]
        return f"crowai_model_{digest}"

    @staticmethod
    def _purge_module_namespace(package_name: str) -> None:
        for name in [key for key in sys.modules if key == package_name or key.startswith(f"{package_name}.")]:
            sys.modules.pop(name, None)
        importlib.invalidate_caches()

    def _purge_import_cache(self, descriptor: Descriptor) -> None:
        package_name = self._package_name(descriptor)
        self._purge_module_namespace(package_name)
        for cache_dir in descriptor.directory.rglob("__pycache__"):
            shutil.rmtree(cache_dir, ignore_errors=True)

    def _shutdown_module_bounded(
        self,
        model_id: str,
        module: ModuleType,
        *,
        timeout_seconds: float = MODEL_SHUTDOWN_TIMEOUT_SECONDS,
    ) -> bool:
        """Run an optional package shutdown callback without allowing reload to hang forever.

        A failed/timed-out shutdown deliberately blocks cache purge.  Keeping the
        old module reachable is safer than orphaning a package-owned llama-server
        and importing a second backend on the same resources/port.
        """
        callback = getattr(module, "shutdown", None)
        if not callable(callback):
            return True

        finished = threading.Event()
        failure: list[BaseException] = []

        def invoke() -> None:
            try:
                callback()
            except BaseException as exc:  # package boundary: never let callback tear down the registry
                failure.append(exc)
            finally:
                finished.set()

        worker = threading.Thread(
            target=invoke,
            name=f"crowai-model-shutdown-{model_id.replace('/', '-')}",
            daemon=True,
        )
        worker.start()
        worker.join(timeout=max(0.05, float(timeout_seconds)))
        if not finished.is_set():
            _LOG.error(
                "Model shutdown timed out after %.1fs; reload is blocked to avoid orphaning the old backend: %s",
                timeout_seconds,
                model_id,
            )
            return False
        if failure:
            _LOG.error(
                "Model shutdown callback failed; reload is blocked to keep the old module reachable: %s",
                model_id,
                exc_info=(type(failure[0]), failure[0], failure[0].__traceback__),
            )
            return False
        return True

    def _retire_cached_module(self, model_id: str, descriptor: Descriptor | None) -> bool:
        cached = self._modules.get(model_id)
        if cached is None:
            return True
        if not self._shutdown_module_bounded(model_id, cached[1]):
            return False
        purge_descriptor = descriptor or self._descriptors.get(model_id)
        if purge_descriptor is not None:
            self._purge_import_cache(purge_descriptor)
        self._modules.pop(model_id, None)
        return True

    def _import_descriptor(self, descriptor: Descriptor, *, force: bool = False) -> ModuleType:
        fingerprint = self._fingerprint(descriptor)
        cached = self._modules.get(descriptor.id)
        if cached and not force and (not self.development or cached[0] == fingerprint):
            return cached[1]

        package_name = self._package_name(descriptor)
        if cached:
            if not self._retire_cached_module(descriptor.id, descriptor):
                raise RuntimeError("The previous model backend did not shut down; development reload was blocked.")
        elif force or (self.development and package_name in sys.modules):
            self._purge_import_cache(descriptor)
        spec = importlib.util.spec_from_file_location(
            package_name,
            descriptor.directory / "__init__.py",
            submodule_search_locations=[str(descriptor.directory)],
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("Python could not create a package loader.")
        module = importlib.util.module_from_spec(spec)
        sys.modules[package_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(package_name, None)
            raise
        if not callable(getattr(module, "prepare_request", None)):
            raise ValueError("Required callback prepare_request() is missing.")
        if not callable(getattr(module, "finalize_result", None)):
            raise ValueError("Required callback finalize_result() is missing.")
        self._modules[descriptor.id] = (fingerprint, module)
        return module

    def refresh(self) -> None:
        descriptors: dict[str, Descriptor] = {}
        modes: dict[str, ModeDescriptor] = {}
        issues: list[ModelIssue] = []
        with self._lock:
            self._issues = issues
            if self.root.is_dir():
                for mode_dir in sorted(path for path in self.root.iterdir() if path.is_dir() and path.name != "__pycache__"):
                    mode = mode_dir.name.strip().lower()
                    if not _PART.fullmatch(mode):
                        issues.append(ModelIssue(mode_dir.name, "invalid_mode_path", "The mode directory name is invalid."))
                        continue
                    modes[mode] = self._mode_metadata(mode_dir, mode)
                    for version_dir in sorted(path for path in mode_dir.iterdir() if path.is_dir() and path.name != "__pycache__"):
                        label = self._package_label(mode, version_dir.name)
                        try:
                            resolved = version_dir.resolve()
                            if self.root != resolved and self.root not in resolved.parents:
                                raise ValueError("Model package path escapes the configured model root.")
                            descriptor = self._parse_descriptor(mode, version_dir)
                            if descriptor.id in descriptors:
                                raise ValueError("Duplicate public model id.")
                            descriptors[descriptor.id] = descriptor
                        except Exception:
                            _LOG.exception("Model package validation failed: %s", label)
                            issues.append(ModelIssue(label, "invalid_model_package", "The model package failed contract validation."))
            for model_id, cached in list(self._modules.items()):
                descriptor = descriptors.get(model_id)
                removed = descriptor is None
                changed = bool(
                    self.development
                    and descriptor is not None
                    and cached[0] != self._fingerprint(descriptor)
                )
                if not (removed or changed):
                    continue
                old_descriptor = self._descriptors.get(model_id)
                if not self._retire_cached_module(model_id, descriptor or old_descriptor):
                    issues.append(
                        ModelIssue(
                            model_id,
                            "model_shutdown_blocked_reload",
                            "The previous model backend did not shut down; reload/removal was deferred.",
                            "warning",
                        )
                    )
            self._descriptors = descriptors
            self._modes = {key: value for key, value in modes.items() if any(item.mode == key for item in descriptors.values())}

    @staticmethod
    def _requirement_label(key: str) -> str:
        labels = {
            "runtime_file": "runtime",
            "model_file": "model",
            "mmproj_file": "vision_projector",
        }
        return labels.get(key, "package_file")

    def readiness(self, model_id: str) -> dict[str, Any]:
        """Return lightweight package readiness without importing or starting a backend."""
        descriptor = self.descriptor(model_id)
        config_path = descriptor.directory / "config.json"
        if not config_path.is_file():
            return {"runnable": True, "status": "runnable", "missing_requirements": []}
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"runnable": False, "status": "invalid_local_config", "missing_requirements": ["package_config"]}
        if not isinstance(config, dict):
            return {"runnable": False, "status": "invalid_local_config", "missing_requirements": ["package_config"]}

        missing: list[str] = []
        invalid: list[str] = []
        root = descriptor.directory.resolve()
        for raw_key, raw_value in config.items():
            key = str(raw_key)
            if not key.endswith("_file") or key in {"database_file", "cache_file", "state_file"}:
                continue
            value = str(raw_value or "").strip() if isinstance(raw_value, str) else ""
            if not value:
                continue
            label = self._requirement_label(key)
            try:
                if key == "runtime_file":
                    states = [local_file_state(candidate, kind="runtime") for candidate in runtime_candidates(root, value)]
                    if "ready" in states:
                        continue
                    (invalid if "invalid" in states else missing).append(label)
                else:
                    area = "model" if key in {"model_file", "mmproj_file"} else None
                    candidate = package_local_file(root, value, area=area)
                    state = local_file_state(candidate, kind=label)
                    if state == "missing":
                        missing.append(label)
                    elif state == "invalid":
                        invalid.append(label)
            except ValueError:
                invalid.append(label)
        missing = list(dict.fromkeys(missing))
        invalid = list(dict.fromkeys(invalid))
        unavailable = missing + [item for item in invalid if item not in missing]
        if missing:
            status = "missing_local_files"
        elif invalid:
            status = "invalid_local_files"
        else:
            status = "runnable"
        return {
            "runnable": not unavailable,
            "status": status,
            "missing_requirements": unavailable,
            "invalid_requirements": invalid,
        }

    def _public_model(self, descriptor: Descriptor) -> dict[str, Any]:
        value = descriptor.public()
        readiness = self.readiness(descriptor.id)
        value.update(readiness)
        if not readiness["runnable"]:
            messages = {
                "missing_local_files": "Local files missing",
                "invalid_local_files": "Local files invalid",
            }
            value["availability_message"] = messages.get(readiness["status"], "Local model unavailable")
        return value

    def list_models(self) -> list[dict[str, Any]]:
        with self._lock:
            values = sorted(self._descriptors.values(), key=lambda item: (item.order, item.mode, item.name.casefold(), item.version.casefold()))
        return [self._public_model(item) for item in values]

    def list_runnable_models(self) -> list[dict[str, Any]]:
        return [item for item in self.list_models() if item.get("runnable") is True]

    def list_modes(self) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in self.list_models():
            grouped.setdefault(item["mode"], []).append(item)
        with self._lock:
            modes = dict(self._modes)
        output = []
        for mode_id, models in grouped.items():
            metadata = modes.get(mode_id) or ModeDescriptor(mode_id, mode_id.replace("_", " ").title(), "", 100)
            output.append({
                "id": mode_id,
                "name": metadata.name,
                "description": metadata.description,
                "order": metadata.order,
                "models": models,
            })
        return sorted(output, key=lambda item: (item["order"], item["name"].casefold(), item["id"]))

    def issues(self) -> list[dict[str, str]]:
        with self._lock:
            return [issue.public() for issue in self._issues]

    def status(self) -> dict[str, Any]:
        models = self.list_models()
        runnable = [item for item in models if item.get("runnable") is True]
        available = bool(runnable)
        if available:
            error = None
        elif models:
            error = "No installed CrowAI model package is currently runnable."
        else:
            error = "No valid CrowAI model packages are installed."
        return {
            "models_available": available,
            "models": models,
            "installed_models": models,
            "runnable_models": runnable,
            "modes": self.list_modes(),
            "model_error": error,
            "model_issues": self.issues(),
        }

    def default_id(self) -> str:
        models = self.list_runnable_models()
        return str(models[0]["id"]) if models else ""

    def descriptor(self, requested: str | None) -> Descriptor:
        candidate = str(requested or "").strip().lower()
        if not candidate:
            candidate = self.default_id()
        if not candidate:
            raise ModelError("No valid CrowAI model packages are installed.")
        with self._lock:
            descriptor = self._descriptors.get(candidate)
        if descriptor is None:
            raise ModelError("The selected model is unavailable.")
        return descriptor

    def runnable_descriptor(self, requested: str | None) -> Descriptor:
        descriptor = self.descriptor(requested)
        readiness = self.readiness(descriptor.id)
        if readiness.get("runnable") is not True:
            raise ModelError("The selected local model is installed but is not runnable.")
        return descriptor

    def _load(self, model_id: str) -> ModuleType:
        descriptor = self.descriptor(model_id)
        with self._lock:
            try:
                return self._import_descriptor(descriptor)
            except Exception as exc:
                _LOG.exception("Model load failed for %s", descriptor.id)
                raise ModelError("The selected model could not be loaded.") from exc

    def prepare(
        self,
        *,
        model_id: str,
        question: str,
        language: str,
        conversation: list[dict[str, str]],
        attachments: list[dict[str, Any]],
        snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        module = self._load(model_id)
        try:
            value = module.prepare_request(
                question=question,
                language=language,
                interaction_mode="conversation",
                conversation=conversation,
                attachments=attachments,
                memory_snapshot=snapshot,
            )
        except ValueError as exc:
            _LOG.info("Model prepare_request rejected input for %s: %s", model_id, exc)
            raise ModelInputError(str(exc)[:500]) from exc
        except Exception as exc:
            _LOG.exception("Model prepare_request failed for %s", model_id)
            raise ModelError("The selected model could not prepare the request.") from exc
        if not isinstance(value, dict):
            raise ModelError("The selected model returned an invalid request plan.")
        value.setdefault("request_question", question)
        value.setdefault("query_variations", [{"query": question, "purpose": "input", "priority": 100}])
        value.setdefault("metadata", {})
        return value

    def finalize(self, *, model_id: str, question: str, language: str, result: dict[str, Any]) -> dict[str, Any]:
        descriptor = self.descriptor(model_id)
        module = self._load(descriptor.id)
        try:
            value = module.finalize_result(
                question=question,
                language=language,
                interaction_mode="conversation",
                result=result,
            )
        except ValueError as exc:
            _LOG.info("Model finalize_result rejected input for %s: %s", model_id, exc)
            raise ModelInputError(str(exc)[:500]) from exc
        except Exception as exc:
            _LOG.exception("Model finalize_result failed for %s", model_id)
            raise ModelError("The selected model could not finalize the response.") from exc
        if not isinstance(value, dict):
            raise ModelError("The selected model returned an invalid response.")
        value.setdefault("model_id", descriptor.id)
        value.setdefault("mode_id", descriptor.mode)
        return value

    def inspect_file(self, *, model_id: str, path: Path, original_name: str, media_type: str) -> dict[str, Any]:
        module = self._load(model_id)
        inspector = getattr(module, "inspect_file", None)
        if not callable(inspector):
            return {}
        try:
            value = inspector(path=path, original_name=original_name, media_type=media_type)
        except Exception as exc:
            _LOG.exception("Model inspect_file failed for %s", model_id)
            raise ModelError("The selected model could not inspect the file.") from exc
        if not isinstance(value, dict):
            raise ModelError("The selected model returned invalid file metadata.")
        return value

    def health_check(self, model_id: str) -> dict[str, Any]:
        descriptor = self.descriptor(model_id)
        module = self._load(descriptor.id)
        callback = getattr(module, "health_check", None)
        if not callable(callback):
            return {"model_id": descriptor.id, "ok": True, "status": "loaded"}
        try:
            value = callback()
        except Exception:
            _LOG.exception("Model health check failed for %s", descriptor.id)
            return {"model_id": descriptor.id, "ok": False, "status": "error"}
        if isinstance(value, dict):
            safe = _sanitize_public(value)
            if not isinstance(safe, dict):
                safe = {}
            safe.setdefault("model_id", descriptor.id)
            safe.setdefault("ok", True)
            return safe
        return {"model_id": descriptor.id, "ok": bool(value), "status": "reported"}

    def cancel_conversation(self, conversation_id: str, *, model_id: str | None = None) -> None:
        """Best-effort immediate cancellation for an active conversation turn.

        Cancellation deliberately targets already imported packages only. If a
        package is not loaded, it cannot own a live backend process in this Core
        process, so importing it during deletion would only add work and state.
        """
        modules: list[ModuleType] = []
        with self._lock:
            if model_id:
                cached = self._modules.get(model_id)
                if cached is not None:
                    modules.append(cached[1])
            else:
                modules = [value[1] for value in self._modules.values()]

        for module in modules:
            callback = getattr(module, "cancel_conversation", None)
            if not callable(callback):
                callback = getattr(module, "cancel", None)
            if not callable(callback):
                continue
            try:
                if getattr(module, "cancel_conversation", None) is callback:
                    callback(conversation_id=conversation_id)
                else:
                    callback()
            except Exception:
                _LOG.exception("Model conversation cancellation failed")

    def cleanup_conversation(self, conversation_id: str, *, model_id: str | None = None) -> None:
        modules: list[ModuleType] = []
        if model_id:
            try:
                modules.append(self._load(model_id))
            except ModelError:
                _LOG.warning("Model conversation cleanup package is unavailable: %s", model_id)
        else:
            with self._lock:
                modules = [value[1] for value in self._modules.values()]

        for module in modules:
            callback = getattr(module, "delete_conversation", None)
            if callable(callback):
                try:
                    callback(conversation_id=conversation_id)
                except Exception:
                    _LOG.exception("Model conversation cleanup failed")

    def shutdown(self) -> None:
        with self._lock:
            modules = list(self._modules.items())
            for model_id, (_, module) in modules:
                if self._shutdown_module_bounded(model_id, module):
                    descriptor = self._descriptors.get(model_id)
                    if descriptor is not None:
                        self._purge_import_cache(descriptor)
                    else:
                        self._purge_module_namespace(module.__name__)
                    self._modules.pop(model_id, None)
