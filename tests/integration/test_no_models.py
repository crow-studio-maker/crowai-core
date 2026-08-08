from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(importlib.util.find_spec("flask") is None, reason="Flask is not installed")


def test_no_model_bootstrap_and_health(tmp_path: Path) -> None:
    from crowai.application import create_app

    app = create_app({
        "TESTING": True,
        "INSTANCE_DIR": tmp_path / "instance",
        "DATABASE_PATH": tmp_path / "instance" / "db.sqlite3",
        "UPLOAD_DIR": tmp_path / "uploads",
        "USERS_DIR": tmp_path / "users",
        "MODELS_DIR": tmp_path / "models",
        "SECRET_KEY": "test-secret-key-that-is-long-enough",
        "MODEL_DEVELOPMENT_RELOAD": False,
    })
    client = app.test_client()
    bootstrap = client.get("/api/bootstrap").get_json()
    assert bootstrap["models_available"] is False
    assert bootstrap["default_model"] == ""
    health = client.get("/health").get_json()
    assert health["status"] == "degraded"
    assert health["core_ready"] is True
    assert health["installed_model_count"] == 0
    assert health["runnable_model_count"] == 0
    assert health["installed_models"] == []
    assert health["runnable_models"] == []
