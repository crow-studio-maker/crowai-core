"""Core-owned, passive attachment inspection.

Uploads are treated as untrusted bytes. This module never imports, executes, launches,
renders in a browser, or otherwise activates uploaded content. Unknown formats are still
accepted and summarized through signatures and printable-string extraction.
"""
from __future__ import annotations

import bz2
import csv
import gzip
import io
import json
import lzma
import mimetypes
import re
import struct
import tarfile
import zipfile
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree

MAX_EXTRACTED_CHARS = 160_000
MAX_READ_BYTES = 4 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 1000
MAX_ARCHIVE_ENTRY_BYTES = 8 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_COMPRESSION_RATIO = 250
MAX_BINARY_STRINGS = 600
MAX_BINARY_STRING_CHARS = 36_000

TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".rst", ".csv", ".tsv", ".json", ".jsonl", ".xml", ".html", ".htm",
    ".css", ".scss", ".less", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".py", ".pyw", ".pyi",
    ".java", ".c", ".h", ".cpp", ".hpp", ".cc", ".cxx", ".cs", ".go", ".rs", ".rb", ".php", ".swift",
    ".kt", ".kts", ".scala", ".sh", ".bash", ".zsh", ".fish", ".bat", ".cmd", ".ps1", ".sql", ".yaml",
    ".yml", ".toml", ".ini", ".cfg", ".conf", ".env", ".log", ".tex", ".vue", ".svelte", ".dart",
    ".gradle", ".properties", ".lock", ".dockerfile", ".graphql", ".proto", ".r", ".lua", ".pl", ".asm",
    ".s", ".cmake", ".make", ".mk",
}
ARCHIVE_EXTENSIONS = {".zip", ".jar", ".whl", ".epub", ".apk", ".ipa", ".vsix"}


class _VisibleHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() in {"script", "style", "noscript", "svg", "canvas"}:
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style", "noscript", "svg", "canvas"} and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            value = " ".join(data.split())
            if value:
                self.parts.append(value)


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8", "utf-8-sig", "utf-16", "utf-16-le", "cp1254", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _clean_text(value: str) -> str:
    value = value.replace("\x00", " ")
    value = re.sub(r"\r\n?", "\n", value)
    value = re.sub(r"[ \t]+\n", "\n", value)
    value = re.sub(r"\n{4,}", "\n\n\n", value)
    return value.strip()[:MAX_EXTRACTED_CHARS]


def _safe_member_name(value: str) -> str | None:
    path = PurePosixPath(str(value).replace("\\", "/"))
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path.as_posix()


def _safe_archive_infos(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    infos = archive.infolist()
    if len(infos) > MAX_ARCHIVE_MEMBERS:
        raise ValueError("Archive contains too many entries.")
    total = 0
    safe: list[zipfile.ZipInfo] = []
    for info in infos:
        name = _safe_member_name(info.filename)
        unix_mode = (info.external_attr >> 16) & 0o170000
        if not name or info.flag_bits & 0x1 or unix_mode == 0o120000:
            raise ValueError("Archive contains an unsafe entry.")
        if info.file_size > MAX_ARCHIVE_ENTRY_BYTES:
            raise ValueError("Archive contains an oversized entry.")
        if info.file_size and info.compress_size == 0:
            raise ValueError("Archive contains an invalid compressed entry.")
        if info.compress_size and info.file_size / info.compress_size > MAX_ARCHIVE_COMPRESSION_RATIO:
            raise ValueError("Archive compression ratio exceeds the inspection limit.")
        total += info.file_size
        if total > MAX_ARCHIVE_TOTAL_BYTES:
            raise ValueError("Archive expands beyond the inspection limit.")
        safe.append(info)
    return safe


def _xml_text(raw: bytes) -> str:
    root = ElementTree.fromstring(raw)
    chunks: list[str] = []
    for node in root.iter():
        local = node.tag.rsplit("}", 1)[-1]
        if local in {"t", "v"} and node.text:
            chunks.append(node.text)
        if local in {"p", "tr", "br"}:
            chunks.append("\n")
    return " ".join(chunks).replace(" \n ", "\n")


def _docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        safe = {info.filename: info for info in _safe_archive_infos(archive)}
        names = [
            name for name in safe
            if name.startswith("word/") and name.endswith(".xml")
            and (name == "word/document.xml" or "/header" in name or "/footer" in name or "footnotes" in name or "endnotes" in name)
        ]
        if "word/document.xml" not in safe:
            raise ValueError("DOCX document.xml is missing.")
        return "\n\n".join(_xml_text(archive.read(name)) for name in names)


def _pptx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        safe = _safe_archive_infos(archive)
        names = sorted(info.filename for info in safe if info.filename.startswith("ppt/slides/slide") and info.filename.endswith(".xml"))
        return "\n\n".join(f"[Slide {index}]\n{_xml_text(archive.read(name))}" for index, name in enumerate(names, start=1))


def _xlsx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        safe_names = [info.filename for info in _safe_archive_infos(archive)]
    try:
        import openpyxl  # optional dependency declared by CrowAI
    except Exception:
        names = [name for name in safe_names if name.startswith("xl/worksheets/")][:50]
        return "Spreadsheet stored. Worksheets: " + ", ".join(Path(name).name for name in names)
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    lines: list[str] = []
    characters = 0
    try:
        for sheet in workbook.worksheets[:50]:
            lines.append(f"# Sheet: {sheet.title}")
            for row_index, row in enumerate(sheet.iter_rows(values_only=True), start=1):
                if row_index > 5000:
                    lines.append("[Sheet truncated after 5000 rows]")
                    break
                line = "\t".join("" if value is None else str(value) for value in row[:100]).rstrip()
                if line:
                    lines.append(line)
                    characters += len(line)
                if characters >= MAX_EXTRACTED_CHARS:
                    return "\n".join(lines)
    finally:
        workbook.close()
    return "\n".join(lines)


def _opendocument_text(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        safe = {info.filename: info for info in _safe_archive_infos(archive)}
        if "content.xml" not in safe:
            raise ValueError("OpenDocument content.xml is missing.")
        raw = archive.read("content.xml")
    root = ElementTree.fromstring(raw)
    parts = [" ".join((element.text or "").split()) for element in root.iter() if element.text]
    return "\n".join(part for part in parts if part)


def _zip_container_kind(path: Path) -> tuple[str, str]:
    """Recognize common ZIP-based documents even when their extension was changed."""
    with zipfile.ZipFile(path) as archive:
        infos = _safe_archive_infos(archive)
        names = {info.filename for info in infos}
        if "word/document.xml" in names:
            return "docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        if "ppt/presentation.xml" in names:
            return "pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        if "xl/workbook.xml" in names:
            return "xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if "content.xml" in names and "META-INF/manifest.xml" in names:
            media = "application/vnd.oasis.opendocument.text"
            try:
                value = archive.read("mimetype")[:160].decode("ascii", errors="ignore").strip()
                if value.startswith("application/vnd.oasis.opendocument."):
                    media = value
            except KeyError:
                pass
            return "opendocument", media
    return "zip", "application/zip"


def _pdf_text(path: Path) -> tuple[str, int]:
    try:
        from pypdf import PdfReader
    except Exception:
        return "", 0
    reader = PdfReader(str(path), strict=False)
    parts: list[str] = []
    for index, page in enumerate(reader.pages[:250], start=1):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        parts.append(f"[Page {index}]\n{text}")
        if sum(len(part) for part in parts) >= MAX_EXTRACTED_CHARS:
            break
    return "\n\n".join(parts), len(reader.pages)


def _text_from_bytes(raw: bytes, suffix: str = "") -> str:
    value = _decode(raw)
    if suffix in {".html", ".htm"}:
        parser = _VisibleHtmlParser()
        parser.feed(value)
        value = "\n".join(parser.parts)
    elif suffix == ".json":
        try:
            value = json.dumps(json.loads(value), ensure_ascii=False, indent=2)
        except (json.JSONDecodeError, TypeError):
            pass
    elif suffix in {".csv", ".tsv"}:
        delimiter = "\t" if suffix == ".tsv" else ","
        rows = csv.reader(io.StringIO(value), delimiter=delimiter)
        value = "\n".join("\t".join(row[:100]) for _, row in zip(range(5000), rows))
    return value


def _zip_text(path: Path) -> tuple[str, list[str]]:
    names: list[str] = []
    blocks: list[str] = []
    with zipfile.ZipFile(path) as archive:
        for info in _safe_archive_infos(archive):
            name = _safe_member_name(info.filename)
            if not name or info.is_dir():
                continue
            names.append(name)
            suffix = Path(name).suffix.casefold()
            if suffix not in TEXT_EXTENSIONS or info.file_size > 1_000_000:
                continue
            raw = archive.read(info)
            blocks.append(f"[Archive member: {name}]\n{_text_from_bytes(raw, suffix)}")
            if sum(len(item) for item in blocks) >= MAX_EXTRACTED_CHARS:
                break
    return "\n\n".join(blocks), names


def _tar_text(path: Path) -> tuple[str, list[str]]:
    names: list[str] = []
    blocks: list[str] = []
    total = 0
    with tarfile.open(path, mode="r:*") as archive:
        members = archive.getmembers()
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise ValueError("Archive contains too many entries.")
        for member in members:
            name = _safe_member_name(member.name)
            if not name or not member.isfile() or member.issym() or member.islnk():
                continue
            if member.size > MAX_ARCHIVE_ENTRY_BYTES:
                raise ValueError("Archive contains an oversized entry.")
            total += member.size
            if total > MAX_ARCHIVE_TOTAL_BYTES:
                raise ValueError("Archive expands beyond the inspection limit.")
            names.append(name)
            suffix = Path(name).suffix.casefold()
            if suffix not in TEXT_EXTENSIONS or member.size > 1_000_000:
                continue
            handle = archive.extractfile(member)
            if handle is None:
                continue
            blocks.append(f"[Archive member: {name}]\n{_text_from_bytes(handle.read(1_000_001), suffix)}")
            if sum(len(item) for item in blocks) >= MAX_EXTRACTED_CHARS:
                break
    return "\n\n".join(blocks), names


def _decompressed_preview(path: Path, suffix: str) -> str:
    opener = gzip.open if suffix == ".gz" else bz2.open if suffix == ".bz2" else lzma.open
    with opener(path, "rb") as handle:
        raw = handle.read(MAX_READ_BYTES + 1)
    if len(raw) > MAX_READ_BYTES:
        raw = raw[:MAX_READ_BYTES]
    return _decode(raw)


def _binary_strings(raw: bytes) -> str:
    """Extract a bounded set of ASCII/UTF-16LE strings from unknown binary data."""
    ascii_values = [match.group().decode("ascii", errors="ignore") for match in re.finditer(rb"[\x20-\x7e]{5,}", raw)]
    utf16_values: list[str] = []
    for match in re.finditer(rb"(?:[\x20-\x7e]\x00){5,}", raw):
        try:
            utf16_values.append(match.group().decode("utf-16-le"))
        except UnicodeDecodeError:
            continue
    unique: list[str] = []
    seen: set[str] = set()
    characters = 0
    for value in [*ascii_values, *utf16_values]:
        cleaned = " ".join(value.split())[:500]
        key = cleaned.casefold()
        if not cleaned or key in seen:
            continue
        seen.add(key)
        unique.append(cleaned)
        characters += len(cleaned)
        if len(unique) >= MAX_BINARY_STRINGS or characters >= MAX_BINARY_STRING_CHARS:
            break
    return "\n".join(unique)


def _signature(sample: bytes) -> tuple[str, str]:
    if sample.startswith(b"%PDF-"):
        return "pdf", "application/pdf"
    if sample.startswith(b"PK\x03\x04"):
        return "zip", "application/zip"
    if sample.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image/png"
    if sample.startswith(b"\xff\xd8\xff"):
        return "jpeg", "image/jpeg"
    if sample.startswith((b"GIF87a", b"GIF89a")):
        return "gif", "image/gif"
    if sample.startswith(b"BM"):
        return "bmp", "image/bmp"
    if sample.startswith(b"RIFF") and sample[8:12] == b"WEBP":
        return "webp", "image/webp"
    if sample.startswith(b"MZ"):
        return "pe_executable", "application/vnd.microsoft.portable-executable"
    if sample.startswith(b"\x7fELF"):
        return "elf_binary", "application/x-elf"
    if sample.startswith(b"\x1f\x8b"):
        return "gzip", "application/gzip"
    if sample.startswith(b"BZh"):
        return "bzip2", "application/x-bzip2"
    if sample.startswith(b"\xfd7zXZ\x00"):
        return "xz", "application/x-xz"
    if sample.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "7z", "application/x-7z-compressed"
    if sample.startswith(b"Rar!\x1a\x07"):
        return "rar", "application/vnd.rar"
    return "unknown", ""


def _image_dimensions(sample: bytes, kind: str) -> dict[str, int]:
    try:
        if kind == "png" and len(sample) >= 24:
            width, height = struct.unpack(">II", sample[16:24])
            return {"width": width, "height": height}
        if kind == "gif" and len(sample) >= 10:
            width, height = struct.unpack("<HH", sample[6:10])
            return {"width": width, "height": height}
        if kind == "bmp" and len(sample) >= 26:
            width, height = struct.unpack("<ii", sample[18:26])
            return {"width": abs(width), "height": abs(height)}
    except (struct.error, ValueError):
        return {}
    return {}


def inspect_file(*, path: Path, original_name: str, media_type: str) -> dict[str, Any]:
    """Passively inspect a stored upload regardless of filename extension."""
    source = Path(path).resolve()
    if not source.is_file():
        return {"status": "error", "text": "", "summary": "The uploaded file could not be found."}

    extension = Path(original_name).suffix.casefold()
    size = source.stat().st_size
    with source.open("rb") as handle:
        sample = handle.read(min(MAX_READ_BYTES, max(4096, min(size, MAX_READ_BYTES))))
    kind, signature_media = _signature(sample[:64])
    guessed = signature_media or media_type or mimetypes.guess_type(original_name)[0] or "application/octet-stream"
    zip_kind = ""
    if kind == "zip":
        try:
            zip_kind, zip_media = _zip_container_kind(source)
        except (OSError, ValueError, zipfile.BadZipFile, ElementTree.ParseError):
            zip_kind, zip_media = "zip", "application/zip"
        if zip_kind != "zip":
            kind, guessed = zip_kind, zip_media

    result: dict[str, Any] = {
        "status": "stored",
        "text": "",
        "summary": "",
        "media_type": guessed,
        "size_bytes": size,
        "detected_format": kind,
    }
    dimensions = _image_dimensions(sample, kind)
    if dimensions:
        result["image_dimensions"] = dimensions

    text = ""
    try:
        if extension in TEXT_EXTENSIONS or guessed.startswith("text/"):
            text = _text_from_bytes(sample, extension)
            result["status"] = "read"
        elif extension == ".docx" or kind == "docx":
            text = _docx_text(source)
            result["status"] = "read"
        elif extension == ".pptx" or kind == "pptx":
            text = _pptx_text(source)
            result["status"] = "read"
        elif extension in {".xlsx", ".xlsm", ".xltx", ".xltm"} or kind == "xlsx":
            text = _xlsx_text(source)
            result["status"] = "read"
        elif extension in {".odt", ".ods", ".odp"} or kind == "opendocument":
            text = _opendocument_text(source)
            result["status"] = "read"
        elif extension == ".pdf" or kind == "pdf":
            text, pages = _pdf_text(source)
            result["page_count"] = pages
            result["status"] = "read" if text.strip() else "stored"
        elif extension in ARCHIVE_EXTENSIONS or kind == "zip":
            text, names = _zip_text(source)
            result["archive_members"] = names[:500]
            result["archive_member_count"] = len(names)
            result["status"] = "read" if text else "indexed"
        elif tarfile.is_tarfile(source):
            text, names = _tar_text(source)
            result["archive_members"] = names[:500]
            result["archive_member_count"] = len(names)
            result["status"] = "read" if text else "indexed"
        elif (extension in {".gz", ".bz2", ".xz", ".lzma"} or kind in {"gzip", "bzip2", "xz"}) and size <= MAX_ARCHIVE_ENTRY_BYTES:
            compression_suffix = extension if extension in {".gz", ".bz2", ".xz", ".lzma"} else {
                "gzip": ".gz", "bzip2": ".bz2", "xz": ".xz",
            }[kind]
            text = _decompressed_preview(source, compression_suffix)
            result["status"] = "read"
        else:
            printable = sum(byte in b"\n\r\t" or 32 <= byte < 127 for byte in sample)
            if sample and printable / len(sample) > 0.80:
                text = _decode(sample)
                result["status"] = "read"
            else:
                strings = _binary_strings(sample)
                if strings:
                    text = "Printable strings extracted from binary data:\n" + strings
                    result["status"] = "indexed"
    except (OSError, ValueError, zipfile.BadZipFile, tarfile.TarError, ElementTree.ParseError, EOFError) as exc:
        result["inspection_error"] = exc.__class__.__name__
        text = ""

    text = _clean_text(text)
    result["text"] = text
    if text:
        result["summary"] = f"{original_name} · {guessed} · {size} bytes · {len(text)} extracted characters"
        result["truncated"] = len(text) >= MAX_EXTRACTED_CHARS
    else:
        detail = kind.replace("_", " ") if kind != "unknown" else "binary metadata"
        result["summary"] = f"{original_name} · {guessed} · {size} bytes · {detail} only"
    return result


__all__ = ["inspect_file"]
