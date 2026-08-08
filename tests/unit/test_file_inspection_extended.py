from __future__ import annotations

import gzip
import io
import tarfile
import zipfile
from pathlib import Path

from crowai.file_inspection import inspect_file


def test_unknown_pe_binary_is_passively_indexed(tmp_path: Path) -> None:
    target = tmp_path / "sample.anything"
    target.write_bytes(b"MZ" + b"\x00" * 128 + b"CrowAI passive binary marker\x00")

    result = inspect_file(
        path=target,
        original_name="sample.anything",
        media_type="application/octet-stream",
    )

    assert result["detected_format"] == "pe_executable"
    assert result["media_type"] == "application/vnd.microsoft.portable-executable"
    assert "CrowAI passive binary marker" in result["text"]
    assert result["status"] == "indexed"


def test_zip_lists_and_reads_safe_source_members(tmp_path: Path) -> None:
    target = tmp_path / "project.weird"
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("src/main.py", "print('hello')\n")
        archive.writestr("README.md", "Project notes")

    result = inspect_file(
        path=target,
        original_name="project.weird",
        media_type="application/octet-stream",
    )

    assert result["detected_format"] == "zip"
    assert result["archive_member_count"] == 2
    assert "src/main.py" in result["archive_members"]
    assert "print('hello')" in result["text"]


def test_tar_member_path_traversal_is_not_extracted(tmp_path: Path) -> None:
    target = tmp_path / "bundle.tar"
    with tarfile.open(target, "w") as archive:
        payload = b"do not escape"
        info = tarfile.TarInfo("../escape.txt")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))

    result = inspect_file(
        path=target,
        original_name="bundle.tar",
        media_type="application/x-tar",
    )

    assert "../escape.txt" not in result.get("archive_members", [])
    assert not (tmp_path.parent / "escape.txt").exists()


def test_gzip_text_preview(tmp_path: Path) -> None:
    target = tmp_path / "notes.gz"
    with gzip.open(target, "wb") as handle:
        handle.write("Merhaba CrowAI gzip".encode())

    result = inspect_file(
        path=target,
        original_name="notes.gz",
        media_type="application/gzip",
    )

    assert result["status"] == "read"
    assert "Merhaba CrowAI gzip" in result["text"]


def test_gzip_magic_is_used_even_when_extension_is_unknown(tmp_path: Path) -> None:
    target = tmp_path / "renamed.payload"
    with gzip.open(target, "wb") as handle:
        handle.write("Compressed by signature".encode())

    result = inspect_file(
        path=target,
        original_name="renamed.payload",
        media_type="application/octet-stream",
    )

    assert result["detected_format"] == "gzip"
    assert result["status"] == "read"
    assert "Compressed by signature" in result["text"]


def test_docx_is_recognized_by_zip_structure_when_extension_is_unknown(tmp_path: Path) -> None:
    target = tmp_path / "renamed.payload"
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "word/document.xml",
            '<?xml version="1.0"?><w:document xmlns:w="urn:test"><w:t>Renamed DOCX text</w:t></w:document>',
        )

    result = inspect_file(
        path=target,
        original_name="renamed.payload",
        media_type="application/octet-stream",
    )

    assert result["detected_format"] == "docx"
    assert "Renamed DOCX text" in result["text"]
