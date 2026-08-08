from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tools import final_verify
from tools.validate_release import validate_directory


ROOT = Path(__file__).resolve().parents[2]


def _write_manifest(root: Path) -> None:
    records = []
    model_ids: set[str] = set()
    for path in sorted(item for item in root.rglob("*") if item.is_file() and item.name != "PACKAGE_MANIFEST.json"):
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
                "product": "CrowAI V1.0 validator regression",
                "core_version": "4.2.0",
                "model_package_sources_included": True,
                "model_binaries_included": False,
                "native_runtime_binaries_included": False,
                "runtime_user_data_included": False,
                "model_ids": sorted(model_ids),
                "files": records,
            },
            sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )


def _validator_checkout(tmp_path: Path) -> Path:
    checkout = tmp_path / "checkout"
    tools = checkout / "tools"
    tools.mkdir(parents=True)
    (tools / "__init__.py").write_text("", encoding="utf-8")
    shutil.copyfile(ROOT / "tools" / "validate_release.py", tools / "validate_release.py")
    shutil.copyfile(ROOT / "tools" / "source_policy.py", tools / "source_policy.py")
    model_package = checkout / "models" / "chat" / "v1.0"
    model_package.mkdir(parents=True)
    (model_package / "__init__.py").write_text("def finalize_result(**kwargs): return {}\n", encoding="utf-8")
    (checkout / "README.md").write_text("clean source\n", encoding="utf-8")
    _write_manifest(checkout)
    return checkout


def _run_direct(checkout: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    # Prove that the tool is safe even when the caller did not remember -B or
    # PYTHONDONTWRITEBYTECODE.
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    return subprocess.run(
        [sys.executable, "tools/validate_release.py", "--source-tree", "."],
        cwd=checkout,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=False,
    )


def test_documented_source_validator_passes_without_self_polluting(tmp_path: Path) -> None:
    checkout = _validator_checkout(tmp_path)
    result = _run_direct(checkout)
    assert result.returncode == 0, result.stdout
    assert "Release validation passed (source-tree)" in result.stdout
    assert not list(checkout.rglob("__pycache__"))
    assert not list(checkout.rglob("*.pyc"))


def test_documented_source_validator_bootstraps_with_startup_no_bytecode_flag(
    tmp_path: Path,
) -> None:
    checkout = _validator_checkout(tmp_path)
    helper = checkout / "tools" / "source_policy.py"
    source = helper.read_text(encoding="utf-8")
    source = source.replace(
        "from __future__ import annotations\n",
        "from __future__ import annotations\n\n"
        "import sys\n"
        "if not sys.flags.dont_write_bytecode:\n"
        "    raise RuntimeError('validator helper imported without interpreter -B')\n",
        1,
    )
    helper.write_text(source, encoding="utf-8")
    _write_manifest(checkout)

    result = _run_direct(checkout)

    assert result.returncode == 0, result.stdout
    assert not list(checkout.rglob("__pycache__"))
    assert not list(checkout.rglob("*.pyc"))


def test_preexisting_pycache_and_pyc_still_fail_strict_source_validation(tmp_path: Path) -> None:
    checkout = _validator_checkout(tmp_path)
    cache = checkout / "pkg" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "module.cpython-313.pyc").write_bytes(b"preexisting")
    result = _run_direct(checkout)
    assert result.returncode == 1
    assert "forbidden" in result.stdout.casefold() or "__pycache__" in result.stdout


def test_compileall_dirtied_tree_still_fails_direct_validation(tmp_path: Path) -> None:
    checkout = _validator_checkout(tmp_path)
    package = checkout / "pkg"
    package.mkdir()
    (package / "module.py").write_text("value = 1\n", encoding="utf-8")
    _write_manifest(checkout)
    compiled = subprocess.run(
        [sys.executable, "-m", "compileall", "-q", "pkg"],
        cwd=checkout,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=20,
        check=False,
    )
    assert compiled.returncode == 0
    assert list(checkout.rglob("*.pyc"))
    result = _run_direct(checkout)
    assert result.returncode == 1


def test_final_verify_clean_staging_excludes_untracked_bytecode_without_deleting_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("safe\n", encoding="utf-8")
    cache = source / "pkg" / "__pycache__"
    cache.mkdir(parents=True)
    bytecode = cache / "module.pyc"
    bytecode.write_bytes(b"local development cache")

    monkeypatch.setattr(final_verify, "ROOT", source)
    staged, method = final_verify._stage_clean_source(tmp_path / "stage")

    assert method == "explicit-source-rules"
    assert (staged / "README.md").is_file()
    assert not (staged / "pkg" / "__pycache__").exists()
    assert bytecode.is_file()  # final_verify never cleans/deletes the working tree


def test_final_verify_staged_source_validates_even_when_checkout_has_local_bytecode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _validator_checkout(tmp_path)
    cache = source / "scratch" / "__pycache__"
    cache.mkdir(parents=True)
    bytecode = cache / "scratch.pyc"
    bytecode.write_bytes(b"untracked local cache")

    monkeypatch.setattr(final_verify, "ROOT", source)
    staged, method = final_verify._stage_clean_source(tmp_path / "staged-release")

    assert method == "explicit-source-rules"
    assert validate_directory(staged, policy="source-tree") == []
    assert bytecode.is_file()
