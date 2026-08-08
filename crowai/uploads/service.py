from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any, Iterable

from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from crowai.errors import ModelExecutionError, UploadRejected, ValidationError
from crowai.file_inspection import inspect_file as core_inspect_file
from crowai.models.service import ModelService, sanitize_public_value
from crowai.storage.permissions import (
    harden_private_directory,
    harden_private_file,
    harden_private_tree,
    open_private_binary_exclusive,
)
from crowai.uploads.repository import UploadRepository



class UploadService:
    def __init__(
        self,
        repository: UploadRepository,
        model_service: ModelService,
        *,
        root: Path,
        maximum_bytes: int,
        maximum_files: int = 10,
        strict_permissions: bool = False,
    ) -> None:
        self.repository = repository
        self.model_service = model_service
        self.root = Path(root).resolve()
        self.maximum_bytes = maximum_bytes
        self.maximum_files = maximum_files
        self.strict_permissions = bool(strict_permissions)
        harden_private_tree(self.root, strict=self.strict_permissions)

    @staticmethod
    def _decode_row(row: dict[str, Any], *, include_internal: bool = False) -> dict[str, Any]:
        try:
            metadata = json.loads(row.get("metadata_json") or "{}")
        except (TypeError, ValueError):
            metadata = {}
        value = {
            **(metadata if isinstance(metadata, dict) else {}),
            "id": row["id"],
            "name": row["original_name"],
            "media_type": row["media_type"],
            "size_bytes": row["size_bytes"],
            "sha256": row["sha256"],
        }
        if include_internal:
            value["_internal_path"] = row["stored_path"]
        return value

    def public(self, upload_ids: Iterable[str], owner_key: str) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for upload_id in dict.fromkeys(str(item) for item in upload_ids if str(item)):
            row = self.repository.get_for_owner(upload_id, owner_key)
            if row:
                output.append(sanitize_public_value(self._decode_row(row)))
        return output

    def for_model(self, upload_ids: tuple[str, ...], owner_key: str) -> list[dict[str, Any]]:
        """Return trusted attachment records for the selected local model package.

        The private path is intentionally never returned by ``public()`` or stored in
        conversation JSON. It exists only for the duration of one model invocation so
        multimodal/document packages can inspect the exact uploaded bytes.
        """
        output: list[dict[str, Any]] = []
        for upload_id in upload_ids:
            row = self.repository.get_for_owner(upload_id, owner_key)
            if not row:
                raise ValidationError("One or more attachments are unavailable.")
            value = self._decode_row(row)
            stored = Path(str(row["stored_path"])).resolve()
            try:
                stored.relative_to(self.root)
            except ValueError as exc:
                raise ValidationError("One or more attachments failed storage validation.") from exc
            if not stored.is_file():
                raise ValidationError("One or more attachments are unavailable.")
            value["_internal_path"] = str(stored)
            output.append(value)
        return output

    @staticmethod
    def _sniff(path: Path, fallback: str) -> str:
        with path.open("rb") as handle:
            sample = handle.read(16)
        if sample.startswith(b"%PDF-"):
            return "application/pdf"
        if sample.startswith(b"PK\x03\x04"):
            return "application/zip"
        if sample.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if sample.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if sample.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        if sample.startswith(b"BM"):
            return "image/bmp"
        if sample.startswith(b"RIFF") and sample[8:12] == b"WEBP":
            return "image/webp"
        if sample.startswith(b"MZ"):
            return "application/vnd.microsoft.portable-executable"
        if sample.startswith(b"\x7fELF"):
            return "application/x-elf"
        if sample.startswith(b"\x1f\x8b"):
            return "application/gzip"
        if sample.startswith(b"7z\xbc\xaf\x27\x1c"):
            return "application/x-7z-compressed"
        if sample.startswith((b"Rar!\x1a\x07\x00", b"Rar!\x1a\x07\x01\x00")):
            return "application/vnd.rar"
        if sample.startswith(b"BZh"):
            return "application/x-bzip2"
        if sample.startswith(b"\xfd7zXZ\x00"):
            return "application/x-xz"
        return fallback or "application/octet-stream"

    def save(self, *, files: list[FileStorage], owner_key: str, model_id: str) -> list[dict[str, Any]]:
        if not files or len(files) > self.maximum_files:
            raise ValidationError(f"Choose between 1 and {self.maximum_files} files.")
        owner_dir = self.root / hashlib.sha256(owner_key.encode("utf-8")).hexdigest()[:24]
        harden_private_directory(owner_dir, strict=self.strict_permissions, create=True)
        created: list[tuple[str, Path]] = []
        temporary_paths: set[Path] = set()
        items: list[dict[str, Any]] = []
        try:
            for storage in files:
                original = str(storage.filename or "").strip()
                if not original or len(original) > 255:
                    raise UploadRejected("Filename is missing or too long.")
                safe = secure_filename(original)
                if not safe:
                    raise UploadRejected("Invalid filename.")
                upload_id = uuid.uuid4().hex
                final_path = owner_dir / f"{upload_id}-{safe}"
                temporary = owner_dir / f".{upload_id}.part"
                digest = hashlib.sha256()
                size = 0
                with open_private_binary_exclusive(temporary, strict=self.strict_permissions) as handle:
                    # The exclusive create has succeeded, so this request owns
                    # this path. Never register a path before acquisition: if an
                    # impossible-but-defensive UUID collision occurs, cleanup must
                    # not unlink another request's file.
                    temporary_paths.add(temporary)
                    while True:
                        chunk = storage.stream.read(1024 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > self.maximum_bytes:
                            raise UploadRejected("File is too large.", 413)
                        digest.update(chunk)
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
                if size <= 0:
                    raise UploadRejected("Empty files are not supported.")
                os.replace(temporary, final_path)
                temporary_paths.discard(temporary)
                # Track the final file before any post-replace operation so a
                # strict permission failure also follows normal orphan cleanup.
                created.append((upload_id, final_path))
                harden_private_file(final_path, strict=self.strict_permissions)
                # If passive inspection or the repository write fails, cleanup
                # must not leave orphaned uploads.
                media_type = self._sniff(final_path, storage.mimetype or "application/octet-stream")
                metadata = core_inspect_file(path=final_path, original_name=original, media_type=media_type)
                try:
                    model_metadata = self.model_service.inspect_file(
                        model_id=model_id,
                        path=final_path,
                        original_name=original,
                        media_type=media_type,
                    )
                except ModelExecutionError:
                    model_metadata = {
                        "status": "stored",
                        "summary": "Model-specific inspection was unavailable; Core inspection succeeded.",
                    }
                if model_metadata:
                    metadata["model_inspection"] = model_metadata
                public = sanitize_public_value({
                    "id": upload_id,
                    "name": original,
                    "media_type": media_type,
                    "content_type": media_type,
                    "size_bytes": size,
                    "sha256": digest.hexdigest(),
                    **metadata,
                })
                self.repository.create(
                    upload_id=upload_id,
                    owner_key=owner_key,
                    original_name=original,
                    stored_path=str(final_path),
                    media_type=media_type,
                    size_bytes=size,
                    sha256=digest.hexdigest(),
                    metadata=public,
                )
                items.append(public)
        except Exception:
            for upload_id, path in created:
                self.repository.delete(upload_id, owner_key)
                path.unlink(missing_ok=True)
            for part in temporary_paths:
                part.unlink(missing_ok=True)
            raise
        return items

    def clear_owner(self, owner_key: str) -> None:
        for stored_path in self.repository.delete_all(owner_key):
            Path(stored_path).unlink(missing_ok=True)
        owner_dir = self.root / hashlib.sha256(owner_key.encode("utf-8")).hexdigest()[:24]
        shutil.rmtree(owner_dir, ignore_errors=True)
