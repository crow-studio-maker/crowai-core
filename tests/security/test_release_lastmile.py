from __future__ import annotations

import hashlib
import json
import os
import stat
import zipfile
from pathlib import Path

import pytest

from tools import build_release, update_source_manifest
from tools.source_policy import deterministic_file_mode
from tools.validate_release import _content_errors, validate_directory, validate_zip


def _write_source_manifest(root: Path) -> None:
    records = []
    model_ids: set[str] = set()
    for path in sorted(item for item in root.rglob("*") if item.is_file() and not item.is_symlink() and item.name != "PACKAGE_MANIFEST.json"):
        relative = path.relative_to(root).as_posix()
        raw = path.read_bytes()
        records.append({"path": relative, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
        parts = Path(relative).parts
        if len(parts) >= 3 and parts[0] == "models" and parts[2].startswith("v"):
            model_ids.add("/".join(parts[:3]))
    (root / "PACKAGE_MANIFEST.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "manifest_type": "source",
                "name": "CrowAI",
                "product": "CrowAI V1.0 test source",
                "core_version": "4.2.0",
                "model_package_sources_included": bool(model_ids),
                "model_binaries_included": False,
                "native_runtime_binaries_included": False,
                "runtime_user_data_included": False,
                "model_ids": sorted(model_ids),
                "files": records,
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.skipif(os.name == "nt", reason="symlink creation/semantics differ on Windows")
def test_source_tree_and_manifest_updater_reject_external_file_symlink(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("external sentinel", encoding="utf-8")
    (source / "leak.txt").symlink_to(outside)

    errors = validate_directory(source, policy="source-tree")
    assert any("source tree symlink entry rejected: leak.txt" in item for item in errors)
    with pytest.raises(RuntimeError, match="symlink"):
        update_source_manifest.update_manifest(source)


@pytest.mark.skipif(os.name == "nt", reason="symlink creation/semantics differ on Windows")
def test_release_builder_rejects_source_symlink_before_reading_external_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("safe", encoding="utf-8")
    outside = tmp_path / "sentinel.txt"
    outside.write_text("must never be packaged", encoding="utf-8")
    (source / "external.txt").symlink_to(outside)

    monkeypatch.setattr(build_release, "ROOT", source)
    output = tmp_path / "outside-build" / "core.zip"
    with pytest.raises(RuntimeError, match="symlink"):
        build_release.build_release(output)
    assert not output.exists()


def test_zip_validator_rejects_unix_symlink_entry(tmp_path: Path) -> None:
    archive_path = tmp_path / "symlink.zip"
    info = zipfile.ZipInfo("external-link")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(info, "/tmp/harmless-sentinel")

    errors = validate_zip(archive_path, policy="core-release")
    assert any("symlink archive entry rejected: external-link" in item for item in errors)


@pytest.mark.parametrize("suffix", [".py", ".js", ".sh"])
def test_secret_scanner_catches_literal_assignments_in_source_files(tmp_path: Path, suffix: str) -> None:
    source = tmp_path / "source"
    package = source / "models" / "chat" / "v1.0"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("def finalize_result(**kwargs): return {}\n", encoding="utf-8")
    # Build the dangerous-looking assignment at runtime so this regression test
    # does not itself embed a literal secret assignment in CrowAI's source tree.
    secret_name = "API_" + "KEY"
    secret_value = "live-" + "credential-material-0123456789"
    (source / f"danger{suffix}").write_text(f'{secret_name} = "{secret_value}"\n', encoding="utf-8")
    _write_source_manifest(source)

    errors = validate_directory(source, policy="source-tree")
    assert any(f"possible embedded secret detected: danger{suffix}" in item for item in errors)


def test_release_file_modes_are_policy_driven_not_host_execute_bits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    readme = source / "README.md"
    linux_entry = source / "run_linux.sh"
    readme.write_text("safe\n", encoding="utf-8")
    linux_entry.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    if os.name == "posix":
        # Intentionally invert normal checkout modes. The archive policy must
        # still normalize README to 0644 and the known launcher to 0755.
        readme.chmod(0o777)
        linux_entry.chmod(0o600)

    monkeypatch.setattr(build_release, "ROOT", source)
    output = tmp_path / "build" / "core.zip"
    build_release.build_release(output)

    with zipfile.ZipFile(output) as archive:
        modes = {
            info.filename: stat.S_IMODE((info.external_attr >> 16) & 0xFFFF)
            for info in archive.infolist()
            if not info.is_dir()
        }
    assert modes["README.md"] == 0o644
    assert modes["run_linux.sh"] == 0o755
    assert modes["RELEASE_MANIFEST.json"] == 0o644
    assert stat.S_IMODE(deterministic_file_mode("README.md")) == 0o644
    assert stat.S_IMODE(deterministic_file_mode("run_linux.sh")) == 0o755

@pytest.mark.skipif(os.name == "nt", reason="symlink creation/semantics differ on Windows")
def test_release_tools_reject_external_directory_symlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("safe\n", encoding="utf-8")
    outside = tmp_path / "external-directory"
    outside.mkdir()
    (outside / "sentinel.txt").write_text("external directory bytes", encoding="utf-8")
    (source / "linked-directory").symlink_to(outside, target_is_directory=True)

    errors = validate_directory(source, policy="source-tree")
    assert any("source tree symlink entry rejected: linked-directory" in item for item in errors)
    with pytest.raises(RuntimeError, match="symlink"):
        update_source_manifest.update_manifest(source)

    monkeypatch.setattr(build_release, "ROOT", source)
    output = tmp_path / "outside-build" / "core.zip"
    with pytest.raises(RuntimeError, match="symlink"):
        build_release.build_release(output)
    assert not output.exists()


def test_source_manifest_updater_never_absorbs_mutable_runtime_state(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("safe source\n", encoding="utf-8")
    runtime_log = source / "instance" / "model_state" / "chat" / "v1.0" / "engine.log"
    runtime_log.parent.mkdir(parents=True)
    runtime_log.write_text("private runtime log\n", encoding="utf-8")
    (source / "instance" / ".gitkeep").write_text("", encoding="utf-8")

    manifest = update_source_manifest.update_manifest(source)
    paths = {record["path"] for record in manifest["files"]}

    assert "README.md" in paths
    assert "instance/.gitkeep" in paths
    assert "instance/model_state/chat/v1.0/engine.log" not in paths
    # The strict source-tree validator still catches a dirty runtime file. The
    # updater merely guarantees that it can never be legitimized by rewriting
    # the source manifest.
    errors = validate_directory(source, policy="source-tree")
    assert any("runtime data included: instance/model_state/chat/v1.0/engine.log" in item for item in errors)


@pytest.mark.parametrize(
    ("filename", "line"),
    [
        ("Dockerfile", "ENV {api}=live-container-credential-0123456789\n"),
        ("Dockerfile", "ARG {api}=live-build-arg-credential-0123456789\n"),
        ("Makefile", "{api} := live-build-credential-0123456789\n"),
        ("Procfile", "{secret}=live-proc-credential-0123456789\n"),
    ],
)
def test_secret_scanner_catches_extensionless_build_files(tmp_path: Path, filename: str, line: str) -> None:
    source = tmp_path / "source"
    package = source / "models" / "chat" / "v1.0"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("def finalize_result(**kwargs): return {}\n", encoding="utf-8")
    # Assemble the dangerous assignment at runtime so the CrowAI repository does
    # not itself contain a literal credential fixture that its own scanner rejects.
    line = line.format(api="API_" + "KEY", secret="SECRET_" + "KEY")
    (source / filename).write_text(line, encoding="utf-8")
    _write_source_manifest(source)

    errors = validate_directory(source, policy="source-tree")
    assert any(f"possible embedded secret detected: {filename}" in item for item in errors)


def test_secret_scanner_catches_python_nested_getenv_literal_default(tmp_path: Path) -> None:
    source = tmp_path / "source"
    package = source / "models" / "chat" / "v1.0"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("def finalize_result(**kwargs): return {}\n", encoding="utf-8")
    secret_name = "PASS" + "WORD"
    secret_value = "really-" + "private-credential-0123456789"
    (source / "danger.py").write_text(
        f'import os\n{secret_name} = os.getenv("{secret_name}", "{secret_value}")\n',
        encoding="utf-8",
    )
    _write_source_manifest(source)

    errors = validate_directory(source, policy="source-tree")
    assert any("possible embedded secret detected: danger.py" in item for item in errors)


def _rewrite_core_metadata(source: Path, output: Path, mutate) -> None:
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as rewritten:
        for old in original.infolist():
            if old.is_dir():
                continue
            info = zipfile.ZipInfo(old.filename, date_time=old.date_time)
            info.compress_type = old.compress_type
            info.create_system = old.create_system
            info.external_attr = old.external_attr
            mutate(info)
            rewritten.writestr(info, original.read(old))


def test_core_validator_rejects_mode_tampering_even_when_payload_hashes_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("safe\n", encoding="utf-8")
    monkeypatch.setattr(build_release, "ROOT", source)
    original = tmp_path / "original.zip"
    tampered = tmp_path / "tampered-mode.zip"
    build_release.build_release(original)
    assert validate_zip(original, policy="core-release") == []

    def mutate(info: zipfile.ZipInfo) -> None:
        if info.filename == "README.md":
            info.external_attr = (stat.S_IFREG | 0o777) << 16

    _rewrite_core_metadata(original, tampered, mutate)
    errors = validate_zip(tampered, policy="core-release")
    assert any("non-canonical file mode for Core entry: README.md" in item for item in errors)
    assert not any("manifest sha256 mismatch: README.md" in item for item in errors)


def test_core_validator_rejects_timestamp_and_origin_metadata_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("safe\n", encoding="utf-8")
    monkeypatch.setattr(build_release, "ROOT", source)
    original = tmp_path / "original.zip"
    tampered_time = tmp_path / "tampered-time.zip"
    tampered_origin = tmp_path / "tampered-origin.zip"
    build_release.build_release(original)

    def mutate_time(info: zipfile.ZipInfo) -> None:
        if info.filename == "README.md":
            info.date_time = (2026, 8, 8, 12, 0, 0)

    _rewrite_core_metadata(original, tampered_time, mutate_time)
    assert any(
        "non-canonical timestamp for Core entry: README.md" in item
        for item in validate_zip(tampered_time, policy="core-release")
    )

    def mutate_origin(info: zipfile.ZipInfo) -> None:
        if info.filename == "README.md":
            info.create_system = 0

    _rewrite_core_metadata(original, tampered_origin, mutate_origin)
    assert any(
        "non-canonical create_system for Core entry: README.md" in item
        for item in validate_zip(tampered_origin, policy="core-release")
    )


def test_python_secret_scanner_allows_environment_lookup_without_literal_default() -> None:
    safe = b'import os\nCROWAI_SECRET_KEY = os.getenv("CROWAI_SECRET_KEY")\n'
    assert not any("secret" in item.casefold() for item in _content_errors("config.py", safe))

    dangerous = b'import os\nCROWAI_SECRET_KEY = os.getenv("CROWAI_SECRET_KEY", "real-production-secret-123456")\n'
    assert any("secret" in item.casefold() for item in _content_errors("config.py", dangerous))
