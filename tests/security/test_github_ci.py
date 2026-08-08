from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_github_actions_workflow_is_in_discoverable_directory() -> None:
    workflow = ROOT / ".github" / "workflows" / "ci.yml"
    assert workflow.is_file()
    assert not (ROOT / "github" / "workflows" / "ci.yml").exists()
    assert not (ROOT / "github").exists()


def test_ci_keeps_required_release_and_test_checks() -> None:
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    for required in (
        "push:",
        "pull_request:",
        'python-version: ["3.11", "3.12", "3.13"]',
        "python tools/check_dependencies.py",
        "python tools/check_format.py",
        "ruff check .",
        "mypy crowai tools",
        "validate_model_package models/chat/v1.0",
        "validate_model_package models/code/v1.0",
        "validate_model_package models/agent/v1.0",
        "python -m compileall",
        "node --check static/workspace.js",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
        "-p pytest_cov",
        "--cov-branch",
        "--cov-fail-under=68",
        "Flask is not installed",
        "tools/final_verify.py --coverage-gate 68",
        "validate_release.py --core-release",
    ):
        assert required in text


def test_ci_supply_chain_references_are_immutable() -> None:
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "actions/checkout@v" not in text
    assert "actions/setup-python@v" not in text
    assert "actions/checkout@11d5960a326750d5838078e36cf38b85af677262" in text
    assert "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065" in text
    assert "python:3.13-slim@sha256:9662417aace5ae7b8e2609cce472b72a8958e134ba372808abe9cc1a0c0125e6" in text
    assert "pip==26.2.1" in text


def test_direct_python_dependencies_are_exactly_pinned() -> None:
    for filename in ("requirements.txt", "requirements-dev.txt"):
        for raw in (ROOT / filename).read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith("-r "):
                continue
            assert "==" in line, f"unbounded direct dependency in {filename}: {line}"
            assert not any(marker in line for marker in (">=", "<=", "~=", "<", ">"))


def test_build_backend_is_exactly_pinned() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'requires = ["setuptools==83.0.0"]' in pyproject
    assert "setuptools>=" not in pyproject
