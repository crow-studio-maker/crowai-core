"""Safe attachment inspection for CrowAI Agent V1.0."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .document_tools import inspect_document


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
_PRIVATE_FIELDS = {"path", "image_path", "local_path", "_internal_path"}


def _load_config() -> dict[str, Any]:
    try:
        value = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


CONFIG = _load_config()


def _public_inspection(value: dict[str, Any]) -> dict[str, Any]:
    """Remove local paths and bulky vision payloads from stored metadata."""

    output: dict[str, Any] = {}
    for key, item in value.items():
        if key in _PRIVATE_FIELDS:
            continue
        if key == "derived_images":
            output["derived_image_count"] = len(item) if isinstance(item, list) else 0
            continue
        output[key] = item
    return output


def inspect_file(
    *,
    path: Path,
    original_name: str,
    media_type: str,
) -> dict[str, Any]:
    """Inspect any attachment passively without executing its contents."""

    source = Path(path).resolve()
    result = inspect_document(
        path=source,
        original_name=original_name,
        media_type=media_type,
        maximum_chars=int(CONFIG.get("document_maximum_chars", 240000)),
        maximum_pages=int(CONFIG.get("document_maximum_pages", 250)),
        maximum_archive_members=int(CONFIG.get("archive_maximum_members", 100)),
        maximum_archive_bytes=int(
            CONFIG.get("archive_maximum_uncompressed_bytes", 25000000)
        ),
    )

    if source.is_file():
        digest = hashlib.sha256()
        with source.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        result["sha256"] = digest.hexdigest()

    return _public_inspection(result)
