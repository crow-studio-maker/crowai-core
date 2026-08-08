"""Visual attachment preparation and conservative local image understanding."""

from __future__ import annotations

import base64
import json
import mimetypes
from pathlib import Path
from typing import Any

from .engine import LocalAgentError, generate_response


SUPPORTED_IMAGE_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
    "image/bmp",
}
MAX_IMAGE_BYTES = 20 * 1024 * 1024


def _data_url_from_path(path: Path, media_type: str | None = None) -> str:
    resolved = path.resolve()
    if not resolved.is_file():
        raise LocalAgentError("Image attachment was not found.")
    if resolved.stat().st_size > MAX_IMAGE_BYTES:
        raise LocalAgentError("Image attachment exceeds the local vision safety limit.")

    guessed = media_type or mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
    if guessed not in SUPPORTED_IMAGE_TYPES:
        raise LocalAgentError(f"Unsupported image type: {guessed}")

    encoded = base64.b64encode(resolved.read_bytes()).decode("ascii")
    return f"data:{guessed};base64,{encoded}"


def attachment_image_url(attachment: dict[str, Any]) -> str | None:
    direct = str(attachment.get("data_url") or "").strip()
    if direct.startswith("data:image/"):
        return direct

    # Only Core's private one-turn path may reference the filesystem. Public
    # attachment metadata must never be able to make Agent read arbitrary files.
    path_value = attachment.get("_internal_path")
    if not path_value:
        return None

    media_type = str(
        attachment.get("media_type") or attachment.get("content_type") or ""
    ).strip() or None
    return _data_url_from_path(Path(str(path_value)), media_type)


def collect_visual_inputs(
    attachments: list[dict[str, Any]],
    *,
    maximum_images: int = 4,
) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []

    for attachment in attachments:
        if not isinstance(attachment, dict) or len(output) >= maximum_images:
            continue

        media_type = str(
            attachment.get("media_type") or attachment.get("content_type") or ""
        ).casefold()
        name = str(
            attachment.get("name") or attachment.get("filename") or "image"
        ).strip()
        suffix = Path(name).suffix.casefold()
        is_direct_image = media_type.startswith("image/") or suffix in {
            ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"
        }

        if is_direct_image:
            try:
                url = attachment_image_url(attachment)
            except (OSError, LocalAgentError):
                url = None
            if url:
                output.append({"name": name, "url": url})

        derived_images = attachment.get("derived_images")
        if not isinstance(derived_images, list):
            continue

        for index, derived in enumerate(derived_images, start=1):
            if len(output) >= maximum_images:
                break
            if not isinstance(derived, dict):
                continue

            derived_name = str(derived.get("name") or f"{name} — page {index}")
            direct = str(derived.get("data_url") or "").strip()
            if direct.startswith("data:image/"):
                output.append({"name": derived_name, "url": direct})
                continue

            # Derived visuals are created by this package as data URLs. Do not
            # accept filesystem paths from attachment metadata.
            continue

    return output


def analyze_images(
    *,
    question: str,
    attachments: list[dict[str, Any]],
    prompt: str,
    maximum_tokens: int,
) -> dict[str, Any]:
    images = collect_visual_inputs(attachments)
    if not images:
        return {}

    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": f"{prompt}\n\nUSER REQUEST:\n{question}\n\nReturn valid JSON only.",
        }
    ]
    for index, image in enumerate(images, start=1):
        content.append({"type": "text", "text": f"IMAGE {index}: {image['name']}"})
        content.append({"type": "image_url", "image_url": {"url": image["url"]}})

    raw = generate_response(
        [{"role": "user", "content": content}],
        maximum_tokens=maximum_tokens,
        temperature=0.1,
        json_mode=True,
    )
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {
            "description": raw,
            "search_queries": [],
            "images": [image["name"] for image in images],
        }
    if not isinstance(value, dict):
        return {}
    value["images"] = [image["name"] for image in images]
    return value
