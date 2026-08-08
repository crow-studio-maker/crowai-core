from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path

from tools.validate_release import validate_directory, validate_zip


def _source_manifest(root: Path) -> bytes:
    records = []
    model_ids: set[str] = set()
    for path in sorted(item for item in root.rglob("*") if item.is_file() and item.name != "PACKAGE_MANIFEST.json"):
        relative = path.relative_to(root).as_posix()
        raw = path.read_bytes()
        records.append({"path": relative, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
        parts = Path(relative).parts
        if len(parts) >= 3 and parts[0] == "models" and parts[2].startswith("v"):
            model_ids.add("/".join(parts[:3]))
    return json.dumps({
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
    }).encode("utf-8")


def _write_source_manifest(root: Path) -> None:
    (root / "PACKAGE_MANIFEST.json").write_bytes(_source_manifest(root))


def test_source_tree_allows_reviewed_model_source_but_core_release_rejects_it(tmp_path: Path) -> None:
    package = tmp_path / "models" / "chat" / "v1.0"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("def prepare_request(**kwargs): return {}\n", encoding="utf-8")
    _write_source_manifest(tmp_path)
    assert validate_directory(tmp_path, policy="source-tree") == []
    errors = validate_directory(tmp_path, policy="core-release")
    assert any("model package content" in item for item in errors)


def test_source_validation_still_rejects_private_binaries_and_real_local_paths(tmp_path: Path) -> None:
    package = tmp_path / "models" / "chat" / "v1.0"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("def prepare_request(**kwargs): return {}\n", encoding="utf-8")
    _write_source_manifest(tmp_path)
    (tmp_path / "weights.gguf").write_bytes(b"not-real")
    (tmp_path / "leak.txt").write_text("path=" + "/" + "home/person/project", encoding="utf-8")
    errors = validate_directory(tmp_path, policy="source-tree")
    assert any("forbidden private/runtime file" in item for item in errors)
    assert any("absolute local path" in item for item in errors)


def test_source_bundle_and_core_zip_policies_are_distinct(tmp_path: Path) -> None:
    archive = tmp_path / "source.zip"
    staging = tmp_path / "staging"
    package = staging / "models" / "code" / "v1.0"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("def finalize_result(**kwargs): return {}\n", encoding="utf-8")
    (staging / "README.md").write_text("safe", encoding="utf-8")
    _write_source_manifest(staging)
    with zipfile.ZipFile(archive, "w") as bundle:
        for path in sorted(item for item in staging.rglob("*") if item.is_file()):
            bundle.write(path, path.relative_to(staging).as_posix())
    assert validate_zip(archive, policy="source-bundle") == []
    assert any("model package content" in item for item in validate_zip(archive, policy="core-release"))


def test_wrapped_archives_keep_release_policy_enforcement(tmp_path: Path) -> None:
    archive = tmp_path / "wrapped.zip"
    staging = tmp_path / "wrapped-staging"
    package = staging / "models" / "agent" / "v1.0"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("def finalize_result(**kwargs): return {}\n", encoding="utf-8")
    _write_source_manifest(staging)
    (staging / "users").mkdir()
    (staging / "users" / "private.json").write_text("{}", encoding="utf-8")
    with zipfile.ZipFile(archive, "w") as bundle:
        for path in sorted(item for item in staging.rglob("*") if item.is_file()):
            bundle.write(path, "crowai/" + path.relative_to(staging).as_posix())
    source_errors = validate_zip(archive, policy="source-bundle")
    assert any("runtime data included" in item for item in source_errors)
    core_errors = validate_zip(archive, policy="core-release")
    assert any("model package content" in item for item in core_errors)
    assert any("runtime data included" in item for item in core_errors)


def test_source_bundle_allows_empty_runtime_placeholder_directories(tmp_path: Path) -> None:
    archive = tmp_path / "source.zip"
    staging = tmp_path / "placeholder-staging"
    package = staging / "models" / "chat" / "v1.0"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("def finalize_result(**kwargs): return {}\n", encoding="utf-8")
    for directory in ("instance", "uploads", "users"):
        target = staging / directory
        target.mkdir()
        (target / ".gitkeep").write_text("", encoding="utf-8")
    _write_source_manifest(staging)
    with zipfile.ZipFile(archive, "w") as bundle:
        for directory in ("crowai/instance/", "crowai/uploads/", "crowai/users/"):
            bundle.writestr(directory, "")
        for path in sorted(item for item in staging.rglob("*") if item.is_file()):
            bundle.write(path, "crowai/" + path.relative_to(staging).as_posix())
    assert validate_zip(archive, policy="source-bundle") == []


def test_unwrapped_core_archive_does_not_confuse_python_package_names_with_runtime_roots(tmp_path: Path) -> None:
    archive = tmp_path / "core.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("README.md", "safe")
        bundle.writestr("crowai/uploads/service.py", "def save_upload(): return None\n")
        bundle.writestr("crowai/users/service.py", "def get_user(): return None\n")
        bundle.writestr("users/.gitkeep", "")
        bundle.writestr("uploads/.gitkeep", "")
    errors = validate_zip(archive, policy="core-release")
    assert "required manifest missing: RELEASE_MANIFEST.json" in errors
    assert not any("runtime data included: crowai/uploads/service.py" in item for item in errors)
    assert not any("runtime data included: crowai/users/service.py" in item for item in errors)


def test_core_release_manifest_detects_tampering(tmp_path: Path) -> None:
    archive = tmp_path / "core.zip"
    readme = b"safe\n"
    manifest = {
        "schema_version": 1, "artifact_type": "core-release", "product": "CrowAI Core", "version": "4.2.0",
        "model_packages_included": False, "model_binaries_included": False,
        "native_runtime_binaries_included": False, "runtime_user_data_included": False,
        "files": [{"path": "README.md", "size": len(readme), "sha256": hashlib.sha256(readme).hexdigest()}],
    }
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("README.md", readme + b"tampered\n")
        bundle.writestr("RELEASE_MANIFEST.json", json.dumps(manifest))
    errors = validate_zip(archive, policy="core-release")
    assert any("manifest size mismatch: README.md" in item for item in errors)
    assert any("manifest sha256 mismatch: README.md" in item for item in errors)


def test_source_manifest_is_required_and_hashes_are_enforced(tmp_path: Path) -> None:
    package = tmp_path / "models" / "chat" / "v1.0"
    package.mkdir(parents=True)
    source = package / "__init__.py"
    source.write_text("def finalize_result(**kwargs): return {}\n", encoding="utf-8")
    assert "required manifest missing: PACKAGE_MANIFEST.json" in validate_directory(tmp_path, policy="source-tree")
    _write_source_manifest(tmp_path)
    source.write_text(source.read_text(encoding="utf-8") + "# tampered\n", encoding="utf-8")
    errors = validate_directory(tmp_path, policy="source-tree")
    assert any("manifest size mismatch" in item for item in errors)
    assert any("manifest sha256 mismatch" in item for item in errors)


def test_source_bundle_requires_manifest_even_when_otherwise_safe(tmp_path: Path) -> None:
    archive = tmp_path / "source-no-manifest.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("README.md", "safe")
    assert "required manifest missing: PACKAGE_MANIFEST.json" in validate_zip(archive, policy="source-bundle")


def test_core_release_rejects_source_manifest_even_if_core_manifest_covers_it(tmp_path: Path) -> None:
    archive = tmp_path / "core-with-source-manifest.zip"
    readme = b"safe\n"
    source_manifest = b"{}\n"
    records = [
        {"path": "README.md", "size": len(readme), "sha256": hashlib.sha256(readme).hexdigest()},
        {"path": "PACKAGE_MANIFEST.json", "size": len(source_manifest), "sha256": hashlib.sha256(source_manifest).hexdigest()},
    ]
    release_manifest = {
        "schema_version": 1, "artifact_type": "core-release", "product": "CrowAI Core", "version": "4.2.0",
        "model_packages_included": False, "model_binaries_included": False,
        "native_runtime_binaries_included": False, "runtime_user_data_included": False, "files": records,
    }
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("README.md", readme)
        bundle.writestr("PACKAGE_MANIFEST.json", source_manifest)
        bundle.writestr("RELEASE_MANIFEST.json", json.dumps(release_manifest))
    errors = validate_zip(archive, policy="core-release")
    assert any("source manifest included in Core release" in item for item in errors)
    assert any("must not contain the source PACKAGE_MANIFEST" in item for item in errors)


def _run_release_builder(*args: str) -> subprocess.CompletedProcess[str]:
    root = Path(__file__).resolve().parents[2]
    return subprocess.run(
        [sys.executable, "tools/build_release.py", *args],
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )


def test_release_builder_custom_output_and_destination_independent_determinism(tmp_path: Path) -> None:
    first = tmp_path / "first.zip"
    second = tmp_path / "nested" / "second.zip"
    result_one = _run_release_builder("--output", str(first))
    result_two = _run_release_builder("--output", str(second))

    assert result_one.returncode == 0, result_one.stdout
    assert result_two.returncode == 0, result_two.stdout
    assert first.is_file() and second.is_file()
    assert hashlib.sha256(first.read_bytes()).hexdigest() == hashlib.sha256(second.read_bytes()).hexdigest()
    assert validate_zip(first, policy="core-release") == []
    assert validate_zip(second, policy="core-release") == []


def test_release_builder_unknown_argument_fails_with_argparse_exit_2() -> None:
    result = _run_release_builder("--definitely-unknown")
    assert result.returncode == 2
    assert "unrecognized arguments" in result.stdout


def test_release_builder_rejects_directory_destination(tmp_path: Path) -> None:
    result = _run_release_builder("--output", str(tmp_path))
    assert result.returncode == 1
    assert "points to a directory" in result.stdout


def test_release_builder_default_output_still_works() -> None:
    root = Path(__file__).resolve().parents[2]
    output = root / "dist" / "CrowAI-Core-4.2.0.zip"
    output.unlink(missing_ok=True)
    result = _run_release_builder()
    assert result.returncode == 0, result.stdout
    assert output.is_file()
    assert validate_zip(output, policy="core-release") == []


def test_release_builder_rejects_non_directory_parent(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("block", encoding="utf-8")
    result = _run_release_builder("--output", str(blocker / "core.zip"))
    assert result.returncode == 1
    assert "parent is not a directory" in result.stdout


def test_release_builder_rejects_repo_local_output_outside_dist() -> None:
    root = Path(__file__).resolve().parents[2]
    first = root / "custom-one-regression.zip"
    second = root / "custom-two-regression.zip"
    first.unlink(missing_ok=True)
    second.unlink(missing_ok=True)
    try:
        result_one = _run_release_builder("--output", str(first))
        result_two = _run_release_builder("--output", str(second))
        assert result_one.returncode == 1
        assert result_two.returncode == 1
        assert "inside the repository must be under dist/" in result_one.stdout
        assert "inside the repository must be under dist/" in result_two.stdout
        assert not first.exists()
        assert not second.exists()
    finally:
        first.unlink(missing_ok=True)
        second.unlink(missing_ok=True)


def test_release_builder_excludes_stale_repo_zip_artifacts() -> None:
    root = Path(__file__).resolve().parents[2]
    stale = root / "stale-release-regression.zip"
    output = root / "dist" / "stale-artifact-exclusion-test.zip"
    stale.write_bytes(b"not-a-real-release")
    output.unlink(missing_ok=True)
    try:
        result = _run_release_builder("--output", str(output))
        assert result.returncode == 0, result.stdout
        with zipfile.ZipFile(output) as archive:
            assert "stale-release-regression.zip" not in archive.namelist()
        assert validate_zip(output, policy="core-release") == []
    finally:
        stale.unlink(missing_ok=True)
        output.unlink(missing_ok=True)
