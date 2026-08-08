"""Recompute PACKAGE_MANIFEST.json file records for a source release.

The manifest intentionally excludes itself to avoid a self-hashing cycle. This
command does not make a dirty/private tree publishable; run ``final_verify.py``
afterwards, which performs the privacy and semantic validation gates.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.source_policy import is_source_release_file, reject_source_symlinks

MANIFEST = ROOT / "PACKAGE_MANIFEST.json"
EXCLUDED_FILES = {"PACKAGE_MANIFEST.json"}


def source_files(root: Path = ROOT) -> list[Path]:
    reject_source_symlinks(root)
    files: list[Path] = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise RuntimeError(f"Source tree symlink entries are not allowed in the source manifest: {path.relative_to(root).as_posix()}")
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if relative.name in EXCLUDED_FILES or not is_source_release_file(relative):
            continue
        files.append(path)
    return sorted(files, key=lambda item: item.relative_to(root).as_posix())


def file_records(root: Path = ROOT) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in source_files(root):
        raw = path.read_bytes()
        records.append({
            "path": path.relative_to(root).as_posix(),
            "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        })
    return records


def model_ids(records: list[dict[str, Any]]) -> list[str]:
    found: set[str] = set()
    for record in records:
        parts = Path(str(record["path"])).parts
        if len(parts) >= 3 and parts[0] == "models" and parts[2].lower().startswith("v"):
            found.add("/".join(parts[:3]))
    return sorted(found)


def update_manifest(root: Path = ROOT) -> dict[str, Any]:
    path = root / "PACKAGE_MANIFEST.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        manifest = {}
    if not isinstance(manifest, dict):
        manifest = {}
    records = file_records(root)
    manifest.update({
        "schema_version": 1,
        "manifest_type": "source",
        "model_package_sources_included": True,
        "model_binaries_included": False,
        "native_runtime_binaries_included": False,
        "runtime_user_data_included": False,
        "model_ids": model_ids(records),
        "files": records,
    })
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    try:
        manifest = update_manifest()
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Source manifest update failed: {exc}")
        return 1
    print(f"Updated {MANIFEST} with {len(manifest['files'])} source file records.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
