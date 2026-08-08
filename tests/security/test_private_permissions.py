from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path

import pytest

from crowai.storage.database import Database
from crowai.storage import permissions
from crowai.user_store import UserJSONStore


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode enforcement is not a Windows ACL guarantee")
def test_database_and_instance_are_private_and_existing_modes_are_hardened(tmp_path: Path) -> None:
    instance = tmp_path / "instance"
    instance.mkdir(mode=0o755)
    database_path = instance / "workspace.db"
    sqlite3.connect(database_path).close()
    instance.chmod(0o755)
    database_path.chmod(0o644)

    database = Database(database_path)

    assert _mode(instance) == 0o700
    assert _mode(database_path) == 0o600
    assert database.health()["ok"] is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode enforcement is not a Windows ACL guarantee")
def test_sqlite_sidecars_are_hardened_when_present(tmp_path: Path) -> None:
    database_path = tmp_path / "instance" / "workspace.db"
    Database(database_path)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(database_path) + suffix)
        sidecar.write_bytes(b"test-sidecar")
        sidecar.chmod(0o644)

    permissions.harden_sqlite_sidecars(database_path, strict=True)

    assert _mode(database_path) == 0o600
    assert _mode(Path(str(database_path) + "-wal")) == 0o600
    assert _mode(Path(str(database_path) + "-shm")) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode enforcement is not a Windows ACL guarantee")
def test_user_root_existing_tree_and_json_snapshots_are_private(tmp_path: Path) -> None:
    database = Database(tmp_path / "instance" / "workspace.db")
    users = tmp_path / "users"
    existing = users / "user old"
    existing.mkdir(parents=True)
    existing_file = existing / "draft.json"
    existing_file.write_text("{}", encoding="utf-8")
    users.chmod(0o755)
    existing.chmod(0o755)
    existing_file.chmod(0o644)

    store = UserJSONStore(users, database)

    assert _mode(users) == 0o700
    assert _mode(existing) == 0o700
    assert _mode(existing_file) == 0o600

    now = "2026-01-01T00:00:00+00:00"
    user_id = database.execute(
        "INSERT INTO users(username,email,password_hash,display_name,settings_json,created_at) VALUES(?,?,?,?,?,?)",
        ("kaan", "kaan@example.com", "hash", "Kaan", "{}", now),
    )
    user = database.one(
        "SELECT id,username,email,display_name,created_at,last_login FROM users WHERE id=?",
        (user_id,),
    )
    assert user is not None
    directory = store.ensure_user(user)
    assert _mode(directory) == 0o700
    assert _mode(directory / "conversations") == 0o700
    assert _mode(directory / "account.json") == 0o600
    assert _mode(directory / "settings.json") == 0o600


@pytest.mark.skipif(os.name != "posix", reason="production fail-closed mode is a POSIX permission policy")
def test_strict_posix_permission_failure_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "private.txt"
    target.write_text("private", encoding="utf-8")

    def fail_chmod(path: object, mode: int) -> None:
        raise OSError("chmod unavailable")

    monkeypatch.setattr(permissions.os, "chmod", fail_chmod)
    with pytest.raises(permissions.PrivatePermissionError, match="Unable to harden"):
        permissions.harden_private_file(target, strict=True)


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode enforcement is not a Windows ACL guarantee")
def test_nested_database_directories_are_private_under_instance_root(tmp_path: Path) -> None:
    instance = tmp_path / "instance"
    database_path = instance / "data" / "database" / "workspace.db"

    Database(
        database_path,
        private_root=instance,
        strict_permissions=True,
    )

    assert _mode(instance) == 0o700
    assert _mode(instance / "data") == 0o700
    assert _mode(instance / "data" / "database") == 0o700
    assert _mode(database_path) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="strict private-root containment is a POSIX production policy")
def test_strict_database_rejects_path_outside_private_root(tmp_path: Path) -> None:
    instance = tmp_path / "instance"
    outside = tmp_path / "outside" / "workspace.db"

    with pytest.raises(permissions.PrivatePermissionError, match="must remain under"):
        Database(outside, private_root=instance, strict_permissions=True)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink/mode semantics differ on Windows")
def test_private_tree_rejects_symlink_root_without_chmodding_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o755)
    link = tmp_path / "private-link"
    link.symlink_to(target, target_is_directory=True)

    with pytest.raises(permissions.PrivatePermissionError, match="must not be a symlink"):
        permissions.harden_private_tree(link, strict=True)

    assert _mode(target) == 0o755


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink/mode semantics differ on Windows")
def test_private_file_rejects_symlink_root_without_chmodding_target(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("private", encoding="utf-8")
    target.chmod(0o644)
    link = tmp_path / "private-file-link"
    link.symlink_to(target)

    with pytest.raises(permissions.PrivatePermissionError, match="must not be a symlink"):
        permissions.harden_private_file(link, strict=True)

    assert _mode(target) == 0o644


def test_production_configuration_allows_read_only_or_absent_model_root(tmp_path: Path) -> None:
    flask = pytest.importorskip("flask")
    from crowai.config import load_configuration

    models = tmp_path / "models"
    models.mkdir()
    if os.name == "posix":
        models.chmod(0o555)

    app = flask.Flask("crowai-permission-test")
    load_configuration(
        app,
        {
            "ENVIRONMENT": "production",
            "INSTANCE_DIR": tmp_path / "instance",
            "UPLOAD_DIR": tmp_path / "uploads",
            "USERS_DIR": tmp_path / "users",
            "MODELS_DIR": models,
            "SECRET_KEY": "x" * 64,
            "MODEL_DEVELOPMENT_RELOAD": False,
        },
    )
    assert app.config["MODELS_DIR"] == models.resolve()
    if os.name == "posix":
        assert _mode(models) == 0o555

    missing = tmp_path / "models-not-installed"
    second = flask.Flask("crowai-missing-model-test")
    load_configuration(
        second,
        {
            "ENVIRONMENT": "production",
            "INSTANCE_DIR": tmp_path / "instance-2",
            "UPLOAD_DIR": tmp_path / "uploads-2",
            "USERS_DIR": tmp_path / "users-2",
            "MODELS_DIR": missing,
            "SECRET_KEY": "y" * 64,
            "MODEL_DEVELOPMENT_RELOAD": False,
        },
    )
    assert second.config["MODELS_DIR"] == missing.resolve()
    assert not missing.exists()
