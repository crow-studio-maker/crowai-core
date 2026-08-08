from __future__ import annotations

# IMPORTANT: the strict source validator must be clean even when invoked exactly
# as documented, without asking the operator to remember ``python -B``.  Setting
# ``sys.dont_write_bytecode`` after interpreter startup is not a sufficiently
# strong contract for every Python startup environment, so a direct invocation
# transparently re-execs itself with ``-B`` *before importing any project-local
# helper*.  The script file itself is not imported and therefore does not create
# a project-local .pyc on the first hop.
import os
import sys

if __name__ == "__main__" and not sys.flags.dont_write_bytecode:
    clean_env = os.environ.copy()
    clean_env["PYTHONDONTWRITEBYTECODE"] = "1"
    os.execve(
        sys.executable,
        [sys.executable, "-B", os.path.abspath(__file__), *sys.argv[1:]],
        clean_env,
    )

import argparse
import ast
import hashlib
import json
import re
import stat
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

# Imported/module use (for example final_verify) gets the same no-bytecode
# behavior even though it does not go through the direct-command re-exec.
sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.source_policy import deterministic_file_mode

TEXT_SUFFIXES = {
    ".py", ".pyi", ".pyw", ".md", ".txt", ".toml", ".json", ".yml", ".yaml",
    ".css", ".scss", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".html",
    ".xml", ".sql", ".sh", ".bash", ".bat", ".cmd", ".ps1", ".ini", ".cfg",
    ".properties", ".example",
}
TEXT_FILENAMES = {
    "dockerfile", "makefile", "procfile", "justfile", "gemfile", "rakefile",
    "vagrantfile", "jenkinsfile",
}
FORBIDDEN_NAMES = {"secret.key", "workspace.db", "memory.sqlite3", "sessions.db", ".env", ".coverage"}
FORBIDDEN_SUFFIXES = {".pyc", ".pyo", ".sqlite", ".sqlite3", ".db", ".pem", ".key", ".gguf", ".exe", ".dll", ".so", ".dylib"}
FORBIDDEN_PARTS = {"__pycache__", ".git", ".venv", "venv", "htmlcov", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
RUNTIME_ROOTS = {"instance", "users", "uploads"}
POLICIES = {"source-tree", "source-bundle", "core-release"}
SOURCE_MANIFEST = "PACKAGE_MANIFEST.json"
CORE_MANIFEST = "RELEASE_MANIFEST.json"
_UNIX_PRIVATE_ROOTS = ("/" + "home/", "/" + "Users/", "/" + "mnt/" + "data/")
_WINDOWS_USER_ROOT = r"[A-Za-z]:" + r"\\" + "Users" + r"\\"
ABSOLUTE_PATHS = re.compile("(?:" + "|".join(re.escape(item) for item in _UNIX_PRIVATE_ROOTS) + "|" + _WINDOWS_USER_ROOT + ")")
PRIVATE_KEY = re.compile("-----BEGIN " + r"(?:RSA |EC |OPENSSH )?" + "PRIVATE KEY-----")
SECRET_ASSIGNMENT = re.compile(
    r"(?im)^[ \t]*(?:(?:export|const|let|var)\s+)?[\"\']?"
    r"(CROWAI_SECRET_KEY|SECRET_KEY|API_KEY|ACCESS_TOKEN|AUTH_TOKEN|CLIENT_SECRET|GITHUB_TOKEN|OPENAI_API_KEY|AWS_SECRET_ACCESS_KEY|PASSWORD)"
    r"[\"\']?[ \t]*(?::=|\?=|\+=|[:=])[ \t]*[\"\']([^\"\'\r\n]{16,})[\"\']"
)
UNQUOTED_SECRET_ASSIGNMENT = re.compile(
    r"(?im)^[ \t]*(?:(?:export)\s+)?"
    r"(CROWAI_SECRET_KEY|SECRET_KEY|API_KEY|ACCESS_TOKEN|AUTH_TOKEN|CLIENT_SECRET|GITHUB_TOKEN|OPENAI_API_KEY|AWS_SECRET_ACCESS_KEY|PASSWORD)"
    r"[ \t]*(?::=|\?=|\+=|=)[ \t]*([A-Za-z0-9_./:+@-]{16,})[ \t]*(?:#.*)?$"
)
SECRET_TOKEN = re.compile(
    r"(?i)\b(?:gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{20,})\b"
)
DOCKER_ENV_SECRET_ASSIGNMENT = re.compile(
    r"(?im)^[ \t]*(?:ENV|ARG)[ \t]+"
    r"(CROWAI_SECRET_KEY|SECRET_KEY|API_KEY|ACCESS_TOKEN|AUTH_TOKEN|CLIENT_SECRET|GITHUB_TOKEN|OPENAI_API_KEY|AWS_SECRET_ACCESS_KEY|PASSWORD)"
    r"(?:[ \t]*=[ \t]*|[ \t]+)[\"']?([^\s\"'#]{16,})[\"']?"
)
EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SECRET_NAMES = {
    "CROWAI_SECRET_KEY", "SECRET_KEY", "API_KEY", "ACCESS_TOKEN", "AUTH_TOKEN",
    "CLIENT_SECRET", "GITHUB_TOKEN", "OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY", "PASSWORD",
}
PLACEHOLDER_MARKERS = (
    "replace", "example", "placeholder", "test-secret", "your-",
    "not-a-real", "dummy", "fake", "fixture", "sample", "changeme",
)
CANONICAL_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def _core_zip_metadata_errors(
    info: zipfile.ZipInfo,
    *,
    canonical_name: str,
) -> list[str]:
    """Validate canonical Core ZIP metadata, not only payload bytes.

    Core artifacts are deterministic across host filesystems: regular files use
    policy-owned modes, a fixed DOS timestamp, Unix metadata, and deflate. A ZIP
    whose bytes match the manifest but whose execution permissions or timestamp
    were modified is therefore not the artifact produced by the release builder.
    """
    errors: list[str] = []
    if info.is_dir():
        errors.append(f"non-canonical directory entry in Core release: {info.filename}")
        return errors
    if info.create_system != 3:
        errors.append(f"non-canonical create_system for Core entry: {info.filename}")
        return errors
    unix_mode = (info.external_attr >> 16) & 0xFFFF
    if stat.S_IFMT(unix_mode) != stat.S_IFREG:
        errors.append(f"non-regular Core archive entry: {info.filename}")
    expected_mode = deterministic_file_mode(canonical_name)
    if stat.S_IMODE(unix_mode) != stat.S_IMODE(expected_mode):
        errors.append(f"non-canonical file mode for Core entry: {info.filename}")
    if tuple(info.date_time) != CANONICAL_ZIP_TIMESTAMP:
        errors.append(f"non-canonical timestamp for Core entry: {info.filename}")
    if info.compress_type != zipfile.ZIP_DEFLATED:
        errors.append(f"non-canonical compression for Core entry: {info.filename}")
    return errors


def _looks_like_real_secret(value: str) -> bool:
    normalized = value.strip().casefold()
    return len(value.strip()) >= 16 and not any(marker in normalized for marker in PLACEHOLDER_MARKERS)


def _call_path(node: ast.AST) -> tuple[str, ...]:
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, ast.Attribute):
        parent = _call_path(node.value)
        return (*parent, node.attr) if parent else (node.attr,)
    return ()


def _secret_value_literals(node: ast.AST | None) -> list[str]:
    """Return credential-like literals from a value expression without keys.

    Environment lookup names are selectors, not credential values. For calls
    such as ``os.getenv("CROWAI_SECRET_KEY")`` the first argument is therefore
    deliberately ignored; only a real fallback/default literal is inspected.
    """
    if node is None:
        return []
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.Call):
        path = tuple(part.casefold() for part in _call_path(node.func))
        recognized_env_lookup = path in {
            ("os", "getenv"),
            ("getenv",),
            ("os", "environ", "get"),
            ("environ", "get"),
        }
        if recognized_env_lookup:
            output: list[str] = []
            if len(node.args) >= 2:
                output.extend(_secret_value_literals(node.args[1]))
            for keyword in node.keywords:
                if keyword.arg in {"default", "fallback"}:
                    output.extend(_secret_value_literals(keyword.value))
            return output
        # Arbitrary function-call string arguments are not assumed to be secret
        # values; scanning them would reintroduce selector/name false positives.
        return []
    if isinstance(node, ast.BoolOp):
        output: list[str] = []
        for value in node.values:
            output.extend(_secret_value_literals(value))
        return output
    if isinstance(node, ast.IfExp):
        return _secret_value_literals(node.body) + _secret_value_literals(node.orelse)
    if isinstance(node, ast.NamedExpr):
        return _secret_value_literals(node.value)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        output: list[str] = []
        for value in node.elts:
            output.extend(_secret_value_literals(value))
        return output
    if isinstance(node, ast.Dict):
        output: list[str] = []
        for value in node.values:
            output.extend(_secret_value_literals(value))
        return output
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _secret_value_literals(node.left) + _secret_value_literals(node.right)
    return []


def _python_nested_secret(text: str) -> bool:
    """Detect literal/default credentials that regex-only assignment scans miss.

    Examples include ``PASSWORD = os.getenv("PASSWORD", "literal-default")``.
    An environment lookup without a fallback is intentionally allowed because
    the variable name itself is not credential material.
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return False

    def target_names(target: ast.AST) -> set[str]:
        if isinstance(target, ast.Name):
            return {target.id.upper()}
        if isinstance(target, (ast.Tuple, ast.List)):
            names: set[str] = set()
            for child in target.elts:
                names.update(target_names(child))
            return names
        return set()

    for node in ast.walk(tree):
        value_node: ast.AST | None = None
        targets: set[str] = set()
        if isinstance(node, ast.Assign):
            value_node = node.value
            for target in node.targets:
                targets.update(target_names(target))
        elif isinstance(node, ast.AnnAssign):
            value_node = node.value
            targets.update(target_names(node.target))
        if targets & SECRET_NAMES:
            if any(_looks_like_real_secret(value) for value in _secret_value_literals(value_node)):
                return True

        if isinstance(node, ast.Call) and len(node.args) >= 2:
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str) and first.value.upper() in SECRET_NAMES:
                if any(_looks_like_real_secret(value) for value in _secret_value_literals(node.args[1])):
                    return True

        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and isinstance(key.value, str) and key.value.upper() in SECRET_NAMES:
                    if any(_looks_like_real_secret(item) for item in _secret_value_literals(value)):
                        return True
    return False


def _root_index(path: PurePosixPath, name: str, *, allow_wrapper: bool) -> int | None:
    """Return a repository-root position, optionally allowing one ZIP wrapper directory."""
    indexes = (0, 1) if allow_wrapper else (0,)
    for index in indexes:
        if len(path.parts) > index and path.parts[index] == name:
            return index
    return None


def _is_model_package_content(path: PurePosixPath, *, allow_wrapper: bool) -> bool:
    index = _root_index(path, "models", allow_wrapper=allow_wrapper)
    return index is not None and len(path.parts) >= index + 3 and path.parts[index + 2].lower().startswith("v")


def _unsafe_relative_path(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        not name
        or "\\" in name
        or "\x00" in name
        or path.is_absolute()
        or ".." in path.parts
        or any(part in {"", "."} for part in path.parts)
        or path.as_posix() != name
    )


def _path_errors(
    name: str,
    *,
    policy: str,
    allow_wrapper: bool = False,
    is_directory: bool = False,
) -> list[str]:
    path = PurePosixPath(name)
    errors: list[str] = []
    if _unsafe_relative_path(name):
        errors.append(f"unsafe archive path: {name}")
    if any(part in FORBIDDEN_PARTS for part in path.parts):
        errors.append(f"forbidden cache/environment path: {name}")
    if path.name in FORBIDDEN_NAMES or path.suffix.casefold() in FORBIDDEN_SUFFIXES:
        errors.append(f"forbidden private/runtime file: {name}")
    if (
        not is_directory
        and any(_root_index(path, root, allow_wrapper=allow_wrapper) is not None for root in RUNTIME_ROOTS)
        and path.name != ".gitkeep"
    ):
        errors.append(f"runtime data included: {name}")
    if policy == "core-release" and _is_model_package_content(path, allow_wrapper=allow_wrapper):
        errors.append(f"model package content included in Core release: {name}")
    if policy == "core-release" and path.name == SOURCE_MANIFEST:
        errors.append(f"source manifest included in Core release: {name}")
    return errors


def _content_errors(name: str, raw: bytes) -> list[str]:
    errors: list[str] = []
    if raw.startswith(b"SQLite format 3\x00"):
        errors.append(f"SQLite database header detected: {name}")
    path = PurePosixPath(name)
    suffix = path.suffix.casefold()
    basename = path.name.casefold()
    # Extensionless build/automation files are source too. In addition to known
    # names, scan any small extensionless UTF-8 file so secrets cannot hide in
    # a custom launcher merely by omitting a suffix.
    is_text_candidate = suffix in TEXT_SUFFIXES or basename in TEXT_FILENAMES or suffix == ""
    if not is_text_candidate or len(raw) > 2_000_000 or b"\x00" in raw:
        return errors
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return errors
    if PRIVATE_KEY.search(text):
        errors.append(f"private key material detected: {name}")
    if ABSOLUTE_PATHS.search(text):
        errors.append(f"absolute local path detected: {name}")
    # Scan literal secret assignments in every recognized text/source format, not
    # just configuration files. Environment lookups (os.getenv/process.env) are
    # not literals and therefore do not match this rule.
    secret_matches = (
        list(SECRET_ASSIGNMENT.finditer(text))
        + list(UNQUOTED_SECRET_ASSIGNMENT.finditer(text))
        + list(DOCKER_ENV_SECRET_ASSIGNMENT.finditer(text))
    )
    for match in secret_matches:
        value = match.group(2).strip().casefold()
        if not any(marker in value for marker in PLACEHOLDER_MARKERS):
            errors.append(f"possible embedded secret detected: {name}")
            break
    else:
        if suffix in {".py", ".pyi", ".pyw"} and _python_nested_secret(text):
            errors.append(f"possible embedded secret detected: {name}")
        for token in SECRET_TOKEN.findall(text):
            lowered = token.casefold()
            if not any(marker in lowered for marker in PLACEHOLDER_MARKERS):
                errors.append(f"possible embedded secret token detected: {name}")
                break
    for address in EMAIL.findall(text):
        lowered = address.casefold()
        if not lowered.endswith(("@example.com", "@example.org", "@example.net", "@example.invalid")):
            errors.append(f"non-sanitized email address detected: {name}")
            break
    return errors


def _semantic_manifest_errors(manifest: dict[str, Any], entries: dict[str, bytes], *, artifact_type: str) -> list[str]:
    errors: list[str] = []
    if manifest.get("schema_version") != 1:
        errors.append(f"unsupported {artifact_type} manifest schema")

    if artifact_type == "core-release":
        if manifest.get("artifact_type") != "core-release":
            errors.append("Core release manifest has wrong artifact_type")
        if not str(manifest.get("product") or "").strip() or not str(manifest.get("version") or "").strip():
            errors.append("Core release manifest requires product and version")
        for field in ("model_packages_included", "model_binaries_included", "native_runtime_binaries_included", "runtime_user_data_included"):
            if manifest.get(field) is not False:
                errors.append(f"Core release manifest must declare {field}=false")
        if SOURCE_MANIFEST in entries:
            errors.append("Core release must not contain the source PACKAGE_MANIFEST.json")
    else:
        if manifest.get("manifest_type") != "source":
            errors.append("source manifest must declare manifest_type=source")
        if not str(manifest.get("name") or "").strip() or not str(manifest.get("product") or "").strip():
            errors.append("source manifest requires name and product")
        if not str(manifest.get("core_version") or "").strip():
            errors.append("source manifest requires core_version")
        if manifest.get("model_package_sources_included") is not True:
            errors.append("source manifest must declare model_package_sources_included=true")
        for field in ("model_binaries_included", "native_runtime_binaries_included", "runtime_user_data_included"):
            if manifest.get(field) is not False:
                errors.append(f"source manifest must declare {field}=false")
        has_model_source = any(_is_model_package_content(PurePosixPath(name), allow_wrapper=False) for name in entries)
        if manifest.get("model_package_sources_included") is True and not has_model_source:
            errors.append("source manifest claims model package sources but none are present")
        model_ids = manifest.get("model_ids")
        if model_ids is not None:
            if not isinstance(model_ids, list) or not all(isinstance(item, str) and "/v" in item for item in model_ids):
                errors.append("source manifest model_ids must be a list of package ids")
            else:
                actual_ids = {
                    "/".join(PurePosixPath(name).parts[:3])
                    for name in entries
                    if _is_model_package_content(PurePosixPath(name), allow_wrapper=False)
                }
                if set(model_ids) != actual_ids:
                    errors.append("source manifest model_ids do not match included package sources")
    return errors


def _manifest_errors(entries: dict[str, bytes], *, manifest_name: str, artifact_type: str) -> list[str]:
    errors: list[str] = []
    raw = entries.get(manifest_name)
    if raw is None:
        return [f"required manifest missing: {manifest_name}"]
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [f"malformed {manifest_name}: {exc}"]
    if not isinstance(manifest, dict):
        return [f"{manifest_name} must contain an object"]

    errors.extend(_semantic_manifest_errors(manifest, entries, artifact_type=artifact_type))
    records = manifest.get("files")
    if not isinstance(records, list):
        return errors + [f"{manifest_name} files must be a list"]

    covered: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            errors.append(f"invalid file record in {manifest_name}")
            continue
        name = str(record.get("path") or "")
        if _unsafe_relative_path(name) or name in covered or name == manifest_name:
            errors.append(f"unsafe/duplicate manifest path: {name}")
            continue
        covered.add(name)
        data = entries.get(name)
        if data is None:
            errors.append(f"manifest file missing from artifact: {name}")
            continue

        size = record.get("size")
        if type(size) is not int or size < 0:
            errors.append(f"invalid manifest size: {name}")
        elif size != len(data):
            errors.append(f"manifest size mismatch: {name}")

        digest = str(record.get("sha256") or "").casefold()
        if not SHA256.fullmatch(digest):
            errors.append(f"invalid manifest sha256: {name}")
        elif digest != hashlib.sha256(data).hexdigest():
            errors.append(f"manifest sha256 mismatch: {name}")

    actual = set(entries) - {manifest_name}
    for name in sorted(actual - covered):
        errors.append(f"archive file missing from manifest: {name}")
    return errors


def _source_manifest_entries(root: Path) -> dict[str, bytes]:
    entries: dict[str, bytes] = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            continue
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in FORBIDDEN_PARTS | {"dist", "build"} for part in relative.parts):
            continue
        entries[relative.as_posix()] = path.read_bytes()
    return entries


def _source_manifest_errors(root: Path) -> list[str]:
    return _manifest_errors(_source_manifest_entries(root), manifest_name=SOURCE_MANIFEST, artifact_type="source")


def validate_zip(path: Path, *, policy: str = "core-release") -> list[str]:
    if policy not in POLICIES:
        raise ValueError(f"Unknown validation policy: {policy}")
    errors: list[str] = []
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            names = [info.filename.rstrip("/") for info in infos if info.filename.rstrip("/")]
            first_parts = {PurePosixPath(name).parts[0] for name in names if PurePosixPath(name).parts}
            allow_wrapper = len(first_parts) == 1 and any(len(PurePosixPath(name).parts) > 1 for name in names)
            wrapper_prefix = (next(iter(first_parts)) + "/") if allow_wrapper else ""
            seen: set[str] = set()
            regular_entries: dict[str, bytes] = {}
            for info in infos:
                name = info.filename.rstrip("/")
                if not name:
                    continue
                if name in seen:
                    errors.append(f"duplicate archive entry: {name}")
                    continue
                seen.add(name)
                unix_mode = (info.external_attr >> 16) & 0xFFFF
                if info.create_system == 3 and stat.S_IFMT(unix_mode) == stat.S_IFLNK:
                    errors.append(f"symlink archive entry rejected: {name}")
                    continue
                if policy == "core-release":
                    canonical_name = name[len(wrapper_prefix):] if wrapper_prefix and name.startswith(wrapper_prefix) else name
                    errors.extend(_core_zip_metadata_errors(info, canonical_name=canonical_name))
                errors.extend(
                    _path_errors(
                        name,
                        policy=policy,
                        allow_wrapper=allow_wrapper,
                        is_directory=info.is_dir(),
                    )
                )
                if not info.is_dir():
                    raw = archive.read(info)
                    regular_entries[name] = raw
                    errors.extend(_content_errors(name, raw))

            if policy == "core-release":
                errors.extend(_manifest_errors(regular_entries, manifest_name=CORE_MANIFEST, artifact_type="core-release"))
            elif policy == "source-bundle":
                prefix = wrapper_prefix
                normalized = {
                    name[len(prefix):] if prefix and name.startswith(prefix) else name: raw
                    for name, raw in regular_entries.items()
                }
                errors.extend(_manifest_errors(normalized, manifest_name=SOURCE_MANIFEST, artifact_type="source"))
    except (OSError, zipfile.BadZipFile) as exc:
        errors.append(f"archive cannot be read: {exc}")
    return errors


def validate_directory(root: Path, *, policy: str = "source-tree") -> list[str]:
    if policy not in POLICIES:
        raise ValueError(f"Unknown validation policy: {policy}")
    errors: list[str] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        # Repository metadata is never part of a source artifact. Other local
        # caches remain visible so a dirty tree still fails the strict validator.
        if ".git" in relative.parts:
            continue
        name = relative.as_posix()
        if path.is_symlink():
            errors.append(f"source tree symlink entry rejected: {name}")
            continue
        if not path.is_file():
            continue
        errors.extend(_path_errors(name, policy=policy, allow_wrapper=False))
        errors.extend(_content_errors(name, path.read_bytes()))
    if policy == "source-tree":
        errors.extend(_source_manifest_errors(root))
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate CrowAI source trees and release bundles for privacy and manifest integrity.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--source-tree", action="store_true", help="Allow reviewed model package source, reject private/runtime binaries and data.")
    group.add_argument("--source-bundle", action="store_true", help="Validate a source ZIP and its PACKAGE_MANIFEST.json integrity.")
    group.add_argument("--core-release", action="store_true", help="Validate a Core-only distribution and RELEASE_MANIFEST.json integrity.")
    parser.add_argument("target", nargs="?", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    target = args.target.expanduser().resolve()
    if args.source_tree:
        policy = "source-tree"
    elif args.source_bundle:
        policy = "source-bundle"
    elif args.core_release:
        policy = "core-release"
    else:
        policy = "core-release" if target.suffix.casefold() == ".zip" else "source-tree"
    errors = validate_zip(target, policy=policy) if target.suffix.casefold() == ".zip" else validate_directory(target, policy=policy)
    if errors:
        print(f"Release validation failed ({policy}):")
        for item in sorted(set(errors)):
            print(f"- {item}")
        return 1
    print(f"Release validation passed ({policy}): {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
