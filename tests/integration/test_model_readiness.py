from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(importlib.util.find_spec("flask") is None, reason="Flask is not installed")


def _write_unrunnable_package(root: Path) -> None:
    package = root / "chat" / "v1.0"
    package.mkdir(parents=True)
    (package / "manifest.json").write_text(json.dumps({
        "id": "v1.0",
        "name": "Chat V1.0",
        "version": "1.0",
        "description": "Installed source-only package.",
        "display_order": 1,
        "capabilities": ["conversation"],
        "model_contract_version": 1,
        "minimum_core_version": "4.0.0",
    }), encoding="utf-8")
    (package / "__init__.py").write_text(
        "def prepare_request(**kwargs): return {'metadata': {}}\n"
        "def finalize_result(**kwargs): return {'answer': 'should not run'}\n",
        encoding="utf-8",
    )
    (package / "config.json").write_text(json.dumps({
        "runtime_file": "runtime/llama-server.exe",
        "model_file": "model/model.gguf",
    }), encoding="utf-8")


def test_installed_but_unrunnable_package_degrades_health_and_cannot_submit(tmp_path: Path) -> None:
    from crowai.application import create_app

    models = tmp_path / "models"
    _write_unrunnable_package(models)
    app = create_app({
        "TESTING": True,
        "INSTANCE_DIR": tmp_path / "instance",
        "DATABASE_PATH": tmp_path / "instance" / "db.sqlite3",
        "UPLOAD_DIR": tmp_path / "uploads",
        "USERS_DIR": tmp_path / "users",
        "MODELS_DIR": models,
        "SECRET_KEY": "test-secret-key-that-is-long-enough",
        "MODEL_DEVELOPMENT_RELOAD": False,
    })
    client = app.test_client()
    bootstrap = client.get("/api/bootstrap").get_json()
    assert bootstrap["models_available"] is False
    assert bootstrap["default_model"] == ""
    assert len(bootstrap["installed_models"]) == 1
    assert bootstrap["runnable_models"] == []
    assert bootstrap["models"][0]["runnable"] is False
    assert bootstrap["models"][0]["availability_message"] == "Local files missing"
    assert str(tmp_path.resolve()) not in json.dumps(bootstrap)

    health = client.get("/health").get_json()
    assert health["status"] == "degraded"
    assert health["installed_model_count"] == 1
    assert health["runnable_model_count"] == 0
    assert any(item["code"] == "NO_RUNNABLE_MODELS" for item in health["issues"])
    assert str(tmp_path.resolve()) not in json.dumps(health)

    csrf = bootstrap["csrf_token"]
    response = client.post(
        "/api/conversations",
        json={"model_id": "chat/v1.0", "request_id": "source-only"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "MODEL_UNAVAILABLE"
