"""Passive source/project attachment inspection for CrowAI Code V1.0."""

from __future__ import annotations

import bz2
import gzip
import hashlib
import io
import json
import lzma
import re
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any


_TEXT_EXTENSIONS = {
    ".py", ".pyi", ".pyw", ".html", ".htm", ".css", ".scss", ".less",
    ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".json", ".jsonl",
    ".toml", ".yaml", ".yml", ".xml", ".sql", ".md", ".txt", ".rst",
    ".ini", ".cfg", ".conf", ".env", ".sh", ".bash", ".zsh", ".bat",
    ".cmd", ".ps1", ".java", ".c", ".h", ".cpp", ".hpp", ".cc", ".cs",
    ".go", ".rs", ".php", ".rb", ".kt", ".kts", ".swift", ".dart",
    ".vue", ".svelte", ".scala", ".gradle", ".properties", ".lock",
    ".graphql", ".proto", ".r", ".lua", ".pl", ".cmake", ".mk", ".make",
}
_ARCHIVE_EXTENSIONS = {".zip", ".jar", ".whl", ".apk", ".ipa", ".vsix"}
_MAX_BYTES = 4 * 1024 * 1024
_MAX_TEXT_CHARS = 180_000
_MAX_ARCHIVE_MEMBERS = 500
_MAX_ARCHIVE_UNCOMPRESSED = 64 * 1024 * 1024


def _language_for(path: Path) -> str:
    mapping = {
        ".py": "python", ".pyi": "python", ".html": "html", ".htm": "html",
        ".css": "css", ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
        ".ts": "typescript", ".tsx": "typescript", ".json": "json", ".sql": "sql",
        ".sh": "bash", ".bash": "bash", ".bat": "batch", ".cmd": "batch",
        ".ps1": "powershell", ".java": "java", ".c": "c", ".h": "c",
        ".cpp": "cpp", ".hpp": "cpp", ".cs": "csharp", ".go": "go", ".rs": "rust",
        ".php": "php", ".rb": "ruby", ".kt": "kotlin", ".swift": "swift",
        ".dart": "dart", ".vue": "vue", ".svelte": "svelte", ".md": "markdown",
        ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".xml": "xml",
    }
    return mapping.get(path.suffix.casefold(), "text")


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8", "utf-8-sig", "cp1254", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _safe_member(value: str) -> str | None:
    path = PurePosixPath(str(value).replace("\\", "/"))
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path.as_posix()


def _magic_kind(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            sample = handle.read(8)
    except OSError:
        return ""
    if sample.startswith(b"\x1f\x8b"):
        return "gzip"
    if sample.startswith(b"BZh"):
        return "bzip2"
    if sample.startswith(b"\xfd7zXZ\x00"):
        return "xz"
    return ""


def _compressed_text(path: Path, kind: str) -> str:
    opener = gzip.open if kind == "gzip" else bz2.open if kind == "bzip2" else lzma.open
    with opener(path, "rb") as handle:
        return _decode(handle.read(_MAX_BYTES + 1)[:_MAX_BYTES])[:_MAX_TEXT_CHARS]


def _safe_zip_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    members = archive.infolist()
    if len(members) > _MAX_ARCHIVE_MEMBERS:
        members = members[:_MAX_ARCHIVE_MEMBERS]
    output: list[zipfile.ZipInfo] = []
    total = 0
    for member in members:
        name = _safe_member(member.filename)
        unix_mode = (member.external_attr >> 16) & 0o170000
        if not name or member.flag_bits & 0x1 or unix_mode == 0o120000:
            continue
        if member.file_size > 4_000_000:
            continue
        compressed = max(member.compress_size, 1)
        if member.file_size and member.file_size / compressed > 250:
            continue
        total += member.file_size
        if total > _MAX_ARCHIVE_UNCOMPRESSED:
            break
        output.append(member)
    return output


def _archive_text(path: Path) -> tuple[str, list[str]]:
    blocks: list[str] = []
    names: list[str] = []
    total = 0

    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for member in _safe_zip_members(archive):
                name = _safe_member(member.filename)
                if not name or member.is_dir():
                    continue
                names.append(name)
                if Path(name).suffix.casefold() not in _TEXT_EXTENSIONS or member.file_size > 2_000_000:
                    continue
                raw = archive.read(member)
                blocks.append(f"[PROJECT FILE: {name}]\n{_decode(raw)}")
                if sum(map(len, blocks)) >= _MAX_TEXT_CHARS:
                    break
        return "\n\n".join(blocks)[:_MAX_TEXT_CHARS], names

    if tarfile.is_tarfile(path):
        with tarfile.open(path, "r:*") as archive:
            for member in archive.getmembers()[:_MAX_ARCHIVE_MEMBERS]:
                name = _safe_member(member.name)
                if not name or not member.isfile() or member.issym() or member.islnk():
                    continue
                if member.size > 2_000_000:
                    continue
                total += member.size
                if total > _MAX_ARCHIVE_UNCOMPRESSED:
                    break
                names.append(name)
                if Path(name).suffix.casefold() not in _TEXT_EXTENSIONS:
                    continue
                handle = archive.extractfile(member)
                if handle is None:
                    continue
                blocks.append(f"[PROJECT FILE: {name}]\n{_decode(handle.read(2_000_001))}")
                if sum(map(len, blocks)) >= _MAX_TEXT_CHARS:
                    break
        return "\n\n".join(blocks)[:_MAX_TEXT_CHARS], names

    return "", []


def _binary_strings(raw: bytes) -> str:
    values: list[str] = []
    seen: set[str] = set()
    candidates = [
        match.group().decode("ascii", errors="ignore")
        for match in re.finditer(rb"[\x20-\x7e]{5,}", raw)
    ]
    for match in re.finditer(rb"(?:[\x20-\x7e]\x00){5,}", raw):
        try:
            candidates.append(match.group().decode("utf-16-le"))
        except UnicodeDecodeError:
            continue
    for candidate in candidates:
        text = " ".join(candidate.split())[:400]
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            values.append(text)
        if sum(map(len, values)) >= 24_000:
            break
    return "\n".join(values)


def inspect_file(*, path: Path, original_name: str, media_type: str) -> dict[str, Any]:
    source = Path(path).resolve()
    if not source.is_file():
        return {"status": "error", "text": "", "summary": "The uploaded file could not be found."}

    size = source.stat().st_size
    digest = hashlib.sha256()
    with source.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)

    name = str(original_name or source.name)
    suffix = Path(name).suffix.casefold()
    text = ""
    members: list[str] = []

    try:
        if suffix in _ARCHIVE_EXTENSIONS or zipfile.is_zipfile(source) or tarfile.is_tarfile(source):
            text, members = _archive_text(source)
        elif suffix in _TEXT_EXTENSIONS or str(media_type).startswith("text/"):
            text = _decode(source.read_bytes()[:_MAX_BYTES])[:_MAX_TEXT_CHARS]
            if suffix == ".json":
                try:
                    text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)[:_MAX_TEXT_CHARS]
                except json.JSONDecodeError:
                    pass
        elif _magic_kind(source):
            text = _compressed_text(source, _magic_kind(source))
        else:
            raw = source.read_bytes()[:_MAX_BYTES]
            printable = sum(byte in b"\n\r\t" or 32 <= byte < 127 for byte in raw)
            if raw and printable / len(raw) > 0.82:
                text = _decode(raw)[:_MAX_TEXT_CHARS]
            else:
                text = _binary_strings(raw)
    except (OSError, zipfile.BadZipFile, tarfile.TarError, ValueError):
        text = ""

    result: dict[str, Any] = {
        "status": "inspected" if text else "stored",
        "text": text,
        "summary": (
            f"Project/source attachment inspected: {name}; {len(text)} characters available."
            if text else "File stored safely; no usable source text was extracted."
        ),
        "name": name,
        "media_type": media_type,
        "language": _language_for(Path(name)),
        "size_bytes": size,
        "sha256": digest.hexdigest(),
        "truncated": len(text) >= _MAX_TEXT_CHARS,
    }
    if members:
        result["archive_members"] = members
    return result
