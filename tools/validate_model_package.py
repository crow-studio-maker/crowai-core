from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from crowai.models.contracts import KNOWN_CAPABILITIES
from crowai.version import CORE_VERSION

PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
FORBIDDEN_NAMES = {"workspace.db", "secret.key", ".env", "memory.sqlite3", "sessions.db"}
FORBIDDEN_PARTS = {"users", "uploads", "instance", ".git", ".venv"}
GENERATED_CACHE_PARTS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
REQUIRED_CALLBACKS = {"prepare_request", "finalize_result"}


def version_tuple(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-+].*)?\s*", value)
    if not match:
        raise ValueError("Version must use numeric semantic form.")
    return tuple(int(part or 0) for part in match.groups())  # type: ignore[return-value]


def validate(root: Path) -> list[str]:
    errors: list[str] = []
    root = root.expanduser().resolve()
    if not root.is_dir():
        return ["Package path is not a directory."]
    if not PART.fullmatch(root.name):
        errors.append("Package directory name is unsafe.")
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        # Developer validation ignores interpreter/test caches so compileall can be
        # run before this command. Release validation remains strict and rejects
        # these paths from source/release artifacts.
        if any(part in GENERATED_CACHE_PARTS for part in relative.parts) or path.suffix.casefold() in {".pyc", ".pyo"}:
            continue
        if any(part in FORBIDDEN_PARTS for part in relative.parts) or path.name in FORBIDDEN_NAMES:
            errors.append(f"Forbidden runtime/private path: {relative}")
        if path.is_symlink():
            errors.append(f"Symbolic links are not allowed: {relative}")
    manifest_path = root / "manifest.json"
    module_path = root / "__init__.py"
    if not manifest_path.is_file():
        errors.append("manifest.json is missing.")
    if not module_path.is_file():
        errors.append("__init__.py is missing.")
    if errors and (not manifest_path.is_file() or not module_path.is_file()):
        return errors
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        errors.append(f"manifest.json is invalid: {exc.__class__.__name__}")
        return errors
    if not isinstance(manifest, dict):
        return errors + ["manifest.json must contain an object."]
    identifier = str(manifest.get("id") or "").casefold()
    if identifier != root.name.casefold() or not PART.fullmatch(identifier):
        errors.append("Manifest id must match the package directory.")
    for key in ("name", "version", "description", "minimum_core_version"):
        if not isinstance(manifest.get(key), str) or not str(manifest[key]).strip():
            errors.append(f"Manifest field {key!r} is required.")
    if manifest.get("model_contract_version") != 1:
        errors.append("Only model_contract_version 1 is supported.")
    try:
        if version_tuple(str(manifest.get("minimum_core_version") or "")) > version_tuple(CORE_VERSION):
            errors.append(f"Package requires a newer Core than {CORE_VERSION}.")
    except ValueError as exc:
        errors.append(str(exc))
    capabilities = manifest.get("capabilities")
    if not isinstance(capabilities, list):
        errors.append("Manifest capabilities must be a list.")
    else:
        unknown = {str(item) for item in capabilities} - KNOWN_CAPABILITIES
        if unknown:
            errors.append("Unknown capabilities: " + ", ".join(sorted(unknown)))
    try:
        tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
        callbacks = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for node in tree.body:
            if not isinstance(node, ast.ImportFrom):
                continue
            for alias in node.names:
                callbacks.add(alias.asname or alias.name)
        missing = REQUIRED_CALLBACKS - callbacks
        if missing:
            errors.append("Missing required callbacks: " + ", ".join(sorted(missing)))
    except (OSError, SyntaxError) as exc:
        errors.append(f"__init__.py cannot be parsed: {exc}")
    config_path = root / "config.json"
    if config_path.is_file():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
            if not isinstance(config, dict):
                raise ValueError("config.json must contain an object.")
            for raw_key, raw_value in config.items():
                key = str(raw_key)
                if not key.endswith("_file") or not isinstance(raw_value, str) or not raw_value.strip():
                    continue
                relative = Path(raw_value.strip())
                if relative.is_absolute() or ".." in relative.parts:
                    errors.append(f"Configuration field {key!r} must use a package-local relative path.")
                    continue
                candidate = (root / relative).resolve()
                if candidate != root and root not in candidate.parents:
                    errors.append(f"Configuration field {key!r} escapes the package.")
                    continue
                if key == "runtime_file" and relative.parts[:1] != ("runtime",):
                    errors.append("runtime_file must be stored under runtime/.")
                if key in {"model_file", "mmproj_file"} and relative.parts[:1] != ("model",):
                    errors.append(f"{key} must be stored under model/.")
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            errors.append(f"config.json is invalid: {exc}")

    checksums = manifest.get("files_sha256", {})
    if checksums is not None and not isinstance(checksums, dict):
        errors.append("files_sha256 must be an object.")
    elif isinstance(checksums, dict):
        for name, expected in checksums.items():
            relative = Path(str(name))
            if relative.is_absolute() or ".." in relative.parts or not SHA256.fullmatch(str(expected).casefold()):
                errors.append(f"Invalid checksum entry: {name}")
                continue
            candidate = (root / relative).resolve()
            if root not in candidate.parents or not candidate.is_file():
                errors.append(f"Checksum file missing: {name}")
                continue
            if hashlib.sha256(candidate.read_bytes()).hexdigest() != str(expected).casefold():
                errors.append(f"Checksum mismatch: {name}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a CrowAI Contract v1 model package without executing it.")
    parser.add_argument("package", type=Path)
    args = parser.parse_args(argv)
    errors = validate(args.package)
    if errors:
        print("Model package validation failed:")
        for item in errors:
            print(f"- {item}")
        return 1
    print("Model package validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
