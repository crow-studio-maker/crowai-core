from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from crowai.version import CORE_VERSION
from tools.validate_release import validate_zip
from tools.source_policy import deterministic_file_mode, reject_source_symlinks

DIST = ROOT / "dist"
OUTPUT = DIST / f"CrowAI-Core-{CORE_VERSION}.zip"
EXCLUDED_PARTS = {
    ".git", ".venv", "venv", "dist", "build", "__pycache__", ".pytest_cache",
    "htmlcov", ".mypy_cache", ".ruff_cache", "tests", "github", ".github",
}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".sqlite", ".sqlite3", ".db", ".log", ".zip"}


def include(path: Path) -> bool:
    relative = path.relative_to(ROOT)
    if any(part in EXCLUDED_PARTS for part in relative.parts):
        return False
    if path.suffix.casefold() in EXCLUDED_SUFFIXES or path.name in {
        ".env", "secret.key", ".coverage", "PACKAGE_MANIFEST.json",
    }:
        return False
    if relative.parts and relative.parts[0] in {"instance", "users", "uploads"}:
        return path.name == ".gitkeep"
    if len(relative.parts) >= 3 and relative.parts[0] == "models":
        return False
    return True


def zip_info(name: str, mode: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (mode & 0xFFFF) << 16
    return info


def _resolve_output(value: Path) -> Path:
    target = value.expanduser().resolve()
    if target.exists() and target.is_dir():
        raise ValueError(f"Release output points to a directory: {target}")
    if target.suffix.casefold() != ".zip":
        raise ValueError("Release output must use a .zip filename.")
    parent = target.parent
    if parent.exists() and not parent.is_dir():
        raise ValueError(f"Release output parent is not a directory: {parent}")

    # A release artifact written back into the source tree can become input to a
    # later build and destroy reproducibility.  Repo-local outputs are therefore
    # restricted to dist/, which is excluded from source enumeration.  Outputs
    # outside the repository remain supported for CI/review workflows.
    try:
        relative = target.relative_to(ROOT)
    except ValueError:
        relative = None
    if relative is not None and (not relative.parts or relative.parts[0] != "dist"):
        raise ValueError("Release output inside the repository must be under dist/.")
    return target


def build_release(output: Path = OUTPUT) -> Path:
    target = _resolve_output(Path(output))
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"Unable to create release output directory: {target.parent}") from exc

    reject_source_symlinks(ROOT)
    files = sorted(
        path
        for path in ROOT.rglob("*")
        if path.is_file() and not path.is_symlink() and include(path) and path.resolve() != target
    )
    manifest_files = []
    for path in files:
        relative = path.relative_to(ROOT).as_posix()
        raw = path.read_bytes()
        manifest_files.append({
            "path": relative,
            "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        })
    release_manifest = {
        "schema_version": 1,
        "artifact_type": "core-release",
        "product": "CrowAI Core",
        "version": CORE_VERSION,
        "model_packages_included": False,
        "model_binaries_included": False,
        "native_runtime_binaries_included": False,
        "runtime_user_data_included": False,
        "files": manifest_files,
    }

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent, delete=False
        ) as handle:
            temporary_path = Path(handle.name)
        with zipfile.ZipFile(temporary_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path in files:
                relative = path.relative_to(ROOT).as_posix()
                archive.writestr(zip_info(relative, deterministic_file_mode(relative)), path.read_bytes())
            raw_manifest = (
                json.dumps(release_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            ).encode("utf-8")
            archive.writestr(zip_info("RELEASE_MANIFEST.json", deterministic_file_mode("RELEASE_MANIFEST.json")), raw_manifest)

        errors = validate_zip(temporary_path, policy="core-release")
        if errors:
            raise RuntimeError("Release build failed validation:\n" + "\n".join(f"- {item}" for item in errors))
        try:
            os.replace(temporary_path, target)
        except OSError as exc:
            raise RuntimeError(f"Unable to write release output: {target}") from exc
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the deterministic CrowAI Core release ZIP.")
    parser.add_argument(
        "--output",
        type=Path,
        default=OUTPUT,
        help=f"Output ZIP path; repo-local outputs must stay under dist/ (default: {OUTPUT.relative_to(ROOT).as_posix()}).",
    )
    args = parser.parse_args(argv)
    try:
        output = build_release(args.output)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Release build failed: {exc}", file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
