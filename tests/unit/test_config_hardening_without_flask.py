from __future__ import annotations

import importlib.util
import os
import stat
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


class _FakeFlaskApp:
    def __init__(self) -> None:
        self.config: dict[str, object] = {}


def _load_config_module():
    previous = sys.modules.get("flask")
    stub = types.ModuleType("flask")
    stub.Flask = _FakeFlaskApp  # type: ignore[attr-defined]
    sys.modules["flask"] = stub
    try:
        spec = importlib.util.spec_from_file_location(
            "_isolated_crowai_config",
            ROOT / "crowai" / "config.py",
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if previous is None:
            sys.modules.pop("flask", None)
        else:
            sys.modules["flask"] = previous


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_private_runtime_root_guard_rejects_project_model_and_overlap_targets(tmp_path: Path) -> None:
    config = _load_config_module()
    project = tmp_path / "project"
    models = project / "models"
    models.mkdir(parents=True)
    config.PROJECT_ROOT = project

    with pytest.raises(RuntimeError, match="project checkout"):
        config._validate_private_runtime_roots(
            instance_dir=project,
            upload_dir=tmp_path / "uploads",
            users_dir=tmp_path / "users",
            models_dir=models,
        )

    with pytest.raises(RuntimeError, match="model package root"):
        config._validate_private_runtime_roots(
            instance_dir=tmp_path / "instance",
            upload_dir=models,
            users_dir=tmp_path / "users",
            models_dir=models,
        )

    private = tmp_path / "private"
    with pytest.raises(RuntimeError, match="non-overlapping"):
        config._validate_private_runtime_roots(
            instance_dir=private,
            upload_dir=private / "uploads",
            users_dir=tmp_path / "users",
            models_dir=models,
        )


def test_production_config_keeps_models_immutable_and_state_under_instance(tmp_path: Path) -> None:
    config = _load_config_module()
    project = tmp_path / "project"
    project.mkdir()
    config.PROJECT_ROOT = project
    models = project / "models"
    models.mkdir()
    if os.name == "posix":
        models.chmod(0o555)

    app = _FakeFlaskApp()
    config.load_configuration(
        app,
        {
            "ENVIRONMENT": "production",
            "INSTANCE_DIR": tmp_path / "runtime" / "instance",
            "UPLOAD_DIR": tmp_path / "runtime" / "uploads",
            "USERS_DIR": tmp_path / "runtime" / "users",
            "MODELS_DIR": models,
            "SECRET_KEY": "x" * 64,
            "MODEL_DEVELOPMENT_RELOAD": False,
        },
    )

    assert app.config["MODELS_DIR"] == models.resolve()
    assert app.config["MODEL_STATE_DIR"] == (tmp_path / "runtime" / "instance" / "model_state").resolve()
    assert Path(app.config["MODEL_STATE_DIR"]).is_dir()
    if os.name == "posix":
        assert _mode(models) == 0o555
        assert _mode(Path(app.config["MODEL_STATE_DIR"])) == 0o700

    missing = project / "missing-models"
    second = _FakeFlaskApp()
    config.load_configuration(
        second,
        {
            "ENVIRONMENT": "production",
            "INSTANCE_DIR": tmp_path / "runtime-2" / "instance",
            "UPLOAD_DIR": tmp_path / "runtime-2" / "uploads",
            "USERS_DIR": tmp_path / "runtime-2" / "users",
            "MODELS_DIR": missing,
            "SECRET_KEY": "y" * 64,
            "MODEL_DEVELOPMENT_RELOAD": False,
        },
    )
    assert second.config["MODELS_DIR"] == missing.resolve()
    assert not missing.exists()


def test_model_state_path_cannot_escape_instance(tmp_path: Path) -> None:
    config = _load_config_module()
    project = tmp_path / "project"
    project.mkdir()
    config.PROJECT_ROOT = project
    app = _FakeFlaskApp()
    with pytest.raises(RuntimeError, match="MODEL_STATE_DIR must stay inside"):
        config.load_configuration(
            app,
            {
                "ENVIRONMENT": "production",
                "INSTANCE_DIR": tmp_path / "runtime" / "instance",
                "UPLOAD_DIR": tmp_path / "runtime" / "uploads",
                "USERS_DIR": tmp_path / "runtime" / "users",
                "MODELS_DIR": project / "models",
                "MODEL_STATE_DIR": tmp_path / "other-state",
                "SECRET_KEY": "z" * 64,
                "MODEL_DEVELOPMENT_RELOAD": False,
            },
        )


def _production_overrides(tmp_path: Path) -> dict[str, object]:
    return {
        "ENVIRONMENT": "production",
        "INSTANCE_DIR": tmp_path / "runtime" / "instance",
        "UPLOAD_DIR": tmp_path / "runtime" / "uploads",
        "USERS_DIR": tmp_path / "runtime" / "users",
        "MODELS_DIR": tmp_path / "project-models",
        "SECRET_KEY": "p" * 64,
    }


def test_programmatic_overrides_cannot_weaken_production_security_or_request_limit(tmp_path: Path) -> None:
    config = _load_config_module()
    config.PROJECT_ROOT = tmp_path / "project"
    config.PROJECT_ROOT.mkdir()
    app = _FakeFlaskApp()
    overrides = _production_overrides(tmp_path)
    overrides.update(
        {
            "PRIVATE_PERMISSIONS_STRICT": False,
            "SESSION_COOKIE_HTTPONLY": False,
            "SESSION_COOKIE_SECURE": False,
            "SESSION_COOKIE_SAMESITE": "None",
            "MAX_REQUEST_BYTES": 4096,
            "MAX_CONTENT_LENGTH": None,
            "MODEL_DEVELOPMENT_RELOAD": True,
            "DEBUG": True,
            "TESTING": True,
        }
    )

    config.load_configuration(app, overrides)

    assert app.config["PRODUCTION"] is True
    assert app.config["PRIVATE_PERMISSIONS_STRICT"] is True
    assert app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert app.config["SESSION_COOKIE_SECURE"] is True
    assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    assert app.config["MODEL_DEVELOPMENT_RELOAD"] is False
    assert app.config["DEBUG"] is False
    assert app.config["TESTING"] is False
    assert app.config["MAX_REQUEST_BYTES"] == 4096
    assert app.config["MAX_CONTENT_LENGTH"] == 4096


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("MAX_UPLOAD_FILES", 1_000_000, "CROWAI_MAX_UPLOAD_FILES"),
        ("MAX_REQUEST_BYTES", 10**15, "CROWAI_MAX_REQUEST_BYTES"),
        ("MAX_UPLOAD_BYTES", 10**15, "CROWAI_MAX_UPLOAD_BYTES"),
        ("MAX_MESSAGE_LENGTH", 10**12, "CROWAI_MAX_MESSAGE_LENGTH"),
        ("SESSION_DAYS", 9999, "CROWAI_SESSION_DAYS"),
    ],
)
def test_programmatic_numeric_overrides_use_same_bounds_as_environment(
    tmp_path: Path,
    key: str,
    value: int,
    message: str,
) -> None:
    config = _load_config_module()
    config.PROJECT_ROOT = tmp_path / "project"
    config.PROJECT_ROOT.mkdir()
    app = _FakeFlaskApp()
    overrides = _production_overrides(tmp_path)
    overrides[key] = value

    with pytest.raises(RuntimeError, match=message):
        config.load_configuration(app, overrides)


def test_programmatic_production_decision_prevents_dotenv_loading(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _load_config_module()
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text("CROWAI_HOST=0.0.0.0\nCROWAI_MAX_UPLOAD_FILES=99\n", encoding="utf-8")
    config.PROJECT_ROOT = project
    monkeypatch.delenv("CROWAI_ENV", raising=False)
    monkeypatch.delenv("CROWAI_HOST", raising=False)
    monkeypatch.delenv("CROWAI_MAX_UPLOAD_FILES", raising=False)

    config.load_project_environment({"ENVIRONMENT": "production"})

    assert os.environ.get("CROWAI_HOST") is None
    assert os.environ.get("CROWAI_MAX_UPLOAD_FILES") is None


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink and chmod semantics required")
def test_private_runtime_symlink_component_is_rejected_before_recursive_hardening(tmp_path: Path) -> None:
    config = _load_config_module()
    project = tmp_path / "project"
    project.mkdir()
    config.PROJECT_ROOT = project

    external = tmp_path / "external"
    nested = external / "instance"
    nested.mkdir(parents=True)
    sentinel = nested / "sentinel.txt"
    sentinel.write_text("leave-me-alone", encoding="utf-8")
    external.chmod(0o755)
    nested.chmod(0o755)
    sentinel.chmod(0o644)

    link = tmp_path / "runtime-link"
    link.symlink_to(external, target_is_directory=True)
    app = _FakeFlaskApp()
    overrides = _production_overrides(tmp_path)
    overrides["INSTANCE_DIR"] = link / "instance"

    with pytest.raises(RuntimeError, match="symlink or junction"):
        config.load_configuration(app, overrides)

    assert _mode(external) == 0o755
    assert _mode(nested) == 0o755
    assert _mode(sentinel) == 0o644


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink and chmod semantics required")
def test_private_runtime_root_symlink_is_rejected_before_target_chmod(tmp_path: Path) -> None:
    config = _load_config_module()
    project = tmp_path / "project"
    project.mkdir()
    config.PROJECT_ROOT = project

    target = tmp_path / "external-instance"
    target.mkdir()
    sentinel = target / "sentinel.txt"
    sentinel.write_text("private", encoding="utf-8")
    target.chmod(0o755)
    sentinel.chmod(0o644)
    link = tmp_path / "instance-link"
    link.symlink_to(target, target_is_directory=True)

    overrides = _production_overrides(tmp_path)
    overrides["INSTANCE_DIR"] = link
    with pytest.raises(RuntimeError, match="symlink or junction"):
        config.initial_instance_path(overrides)

    assert _mode(target) == 0o755
    assert _mode(sentinel) == 0o644


def test_custom_production_runtime_root_requires_empty_tree_or_ownership_marker(tmp_path: Path) -> None:
    config = _load_config_module()
    project = tmp_path / "project"
    project.mkdir()
    config.PROJECT_ROOT = project

    existing = tmp_path / "existing-instance"
    existing.mkdir()
    unrelated = existing / "unrelated.txt"
    unrelated.write_text("not-crowai-owned", encoding="utf-8")
    before = _mode(unrelated) if os.name == "posix" else None

    app = _FakeFlaskApp()
    overrides = _production_overrides(tmp_path)
    overrides["INSTANCE_DIR"] = existing
    with pytest.raises(RuntimeError, match="new/empty or contain"):
        config.load_configuration(app, overrides)
    if os.name == "posix":
        assert _mode(unrelated) == before

    clean_root = tmp_path / "clean-instance"
    clean_root.mkdir()
    clean_app = _FakeFlaskApp()
    clean_overrides = _production_overrides(tmp_path / "clean")
    clean_overrides["INSTANCE_DIR"] = clean_root
    config.load_configuration(clean_app, clean_overrides)
    marker = clean_root / config.PRIVATE_ROOT_MARKER
    assert marker.is_file()
    if os.name == "posix":
        assert _mode(clean_root) == 0o700
        assert _mode(marker) == 0o600

    # A marked CrowAI-owned tree may be reused on the next production start.
    second = _FakeFlaskApp()
    config.load_configuration(second, clean_overrides)
    assert second.config["INSTANCE_DIR"] == clean_root.resolve()


def test_custom_production_runtime_root_rejects_forged_marker_before_chmod(tmp_path: Path) -> None:
    config = _load_config_module()
    project = tmp_path / "project"
    project.mkdir()
    config.PROJECT_ROOT = project

    root = tmp_path / "claimed-instance"
    root.mkdir()
    marker = root / config.PRIVATE_ROOT_MARKER
    marker.write_text("not-crowai\n", encoding="utf-8")
    sentinel = root / "sentinel.txt"
    sentinel.write_text("unchanged", encoding="utf-8")
    if os.name == "posix":
        root.chmod(0o755)
        marker.chmod(0o644)
        sentinel.chmod(0o644)

    overrides = _production_overrides(tmp_path)
    overrides["INSTANCE_DIR"] = root
    with pytest.raises(RuntimeError, match="Invalid CrowAI private-root ownership marker"):
        config.load_configuration(_FakeFlaskApp(), overrides)

    if os.name == "posix":
        assert _mode(root) == 0o755
        assert _mode(sentinel) == 0o644
