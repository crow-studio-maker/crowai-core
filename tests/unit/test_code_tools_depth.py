from __future__ import annotations

import gzip
import importlib
import io
import json
import tarfile
import zipfile
from pathlib import Path

from models.registry import ModelRegistry

ROOT = Path(__file__).resolve().parents[2]


def _tools():
    registry = ModelRegistry(ROOT / "models", development=True, strict_capabilities=True)
    package = registry._load("code/v1.0")
    return importlib.import_module(f"{package.__name__}.tools")


def test_code_inspector_text_json_and_missing_file(tmp_path: Path) -> None:
    tools = _tools()
    missing = tools.inspect_file(path=tmp_path / "missing.py", original_name="missing.py", media_type="text/plain")
    assert missing["status"] == "error"

    source = tmp_path / "main.py"
    source.write_text("print('hello')\n", encoding="utf-8")
    result = tools.inspect_file(path=source, original_name="main.py", media_type="text/x-python")
    assert result["status"] == "inspected"
    assert result["language"] == "python"
    assert "hello" in result["text"]
    assert len(result["sha256"]) == 64

    data = tmp_path / "data.json"
    data.write_text('{"b":2,"a":1}', encoding="utf-8")
    parsed = tools.inspect_file(path=data, original_name="data.json", media_type="application/json")
    assert parsed["status"] == "inspected"
    assert json.loads(parsed["text"]) == {"a": 1, "b": 2}


def test_code_inspector_compressed_text_and_binary_strings(tmp_path: Path) -> None:
    tools = _tools()
    gz = tmp_path / "source.bin"
    with gzip.open(gz, "wb") as handle:
        handle.write(b"compressed source text\n")
    result = tools.inspect_file(path=gz, original_name="source.bin", media_type="application/octet-stream")
    assert "compressed source text" in result["text"]

    binary = tmp_path / "program.bin"
    binary.write_bytes(b"\x00\x01\x02HELLO_BINARY_WORLD\x00\xff" + "UNICODETEXT".encode("utf-16-le"))
    inspected = tools.inspect_file(path=binary, original_name="program.bin", media_type="application/octet-stream")
    assert "HELLO_BINARY_WORLD" in inspected["text"] or "UNICODETEXT" in inspected["text"]


def test_code_zip_inspection_rejects_unsafe_members_but_reads_safe_source(tmp_path: Path) -> None:
    tools = _tools()
    archive_path = tmp_path / "project.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("src/app.py", "value = 7\n")
        archive.writestr("README.md", "hello\n")
        archive.writestr("../escape.py", "bad = True\n")
        archive.writestr("assets/blob.bin", b"\x00\x01")
    result = tools.inspect_file(path=archive_path, original_name="project.zip", media_type="application/zip")
    assert result["status"] == "inspected"
    assert "[PROJECT FILE: src/app.py]" in result["text"]
    assert "value = 7" in result["text"]
    assert "../escape.py" not in result.get("archive_members", [])
    assert "src/app.py" in result["archive_members"]
    assert "assets/blob.bin" in result["archive_members"]


def test_code_tar_inspection_reads_regular_text_and_skips_links(tmp_path: Path) -> None:
    tools = _tools()
    archive_path = tmp_path / "project.tar"
    with tarfile.open(archive_path, "w") as archive:
        payload = b"def f():\n    return 3\n"
        info = tarfile.TarInfo("pkg/mod.py")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))

        link = tarfile.TarInfo("pkg/link.py")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        archive.addfile(link)
    result = tools.inspect_file(path=archive_path, original_name="project.tar", media_type="application/x-tar")
    assert "pkg/mod.py" in result["archive_members"]
    assert "pkg/link.py" not in result["archive_members"]
    assert "return 3" in result["text"]


def test_code_inspector_safe_member_and_language_helpers() -> None:
    tools = _tools()
    assert tools._safe_member("src/x.py") == "src/x.py"
    assert tools._safe_member("../x.py") is None
    assert tools._safe_member("/x.py") is None
    assert tools._language_for(Path("a.vue")) == "vue"
    assert tools._language_for(Path("a.unknown")) == "text"
