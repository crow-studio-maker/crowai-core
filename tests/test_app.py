from __future__ import annotations

import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def write_model(root: Path) -> None:
    directory = root / "chat" / "v1.0"
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_text(json.dumps({
        "id": "v1.0",
        "name": "CrowAI Test",
        "version": "1.0",
        "description": "A test-only model package.",
        "display_order": 10,
        "capabilities": ["conversation", "attachments"],
        "model_contract_version": 1,
        "minimum_core_version": "4.0.0",
    }), encoding="utf-8")
    (directory / "__init__.py").write_text('''
def prepare_request(**kwargs):
    return {"metadata": {}}

DELETED = []

def finalize_result(**kwargs):
    return {
        "answer": "Test answer", "sources": [], "artifacts": [], "warnings": [],
        "memory_update": {"mode_state": {"last_test": "persisted"}},
    }

def delete_conversation(*, conversation_id):
    DELETED.append(conversation_id)
''', encoding="utf-8")


@unittest.skipUnless(importlib.util.find_spec("flask"), "Flask is not installed in this validation environment.")
class AppFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        base = Path(cls.temporary.name)
        models = base / "models"
        write_model(models)
        os.environ.update({
            "CROWAI_INSTANCE_DIR": str(base / "instance"),
            "CROWAI_UPLOAD_DIR": str(base / "uploads"),
            "CROWAI_MODELS_DIR": str(models),
            "CROWAI_USERS_DIR": str(base / "users"),
            "CROWAI_SECRET_KEY": "test-secret-key-that-is-long-and-stable",
            "CROWAI_ENV": "development",
        })
        spec = importlib.util.spec_from_file_location("crowai_test_application", ROOT / "app.py")
        cls.module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(cls.module)
        cls.module.app.config.update(TESTING=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.module.app.extensions["crowai"].registry.shutdown()
        cls.temporary.cleanup()

    def client_with_csrf(self):
        client = self.module.app.test_client()
        bootstrap = client.get("/api/bootstrap").get_json()
        return client, bootstrap["csrf_token"]

    def test_bootstrap_and_health_expose_model_state(self) -> None:
        client, _ = self.client_with_csrf()
        bootstrap = client.get("/api/bootstrap").get_json()
        self.assertTrue(bootstrap["models_available"])
        self.assertEqual("chat/v1.0", bootstrap["default_model"])
        health = client.get("/health").get_json()
        self.assertEqual("healthy", health["status"])

    def test_csrf_blocks_state_change(self) -> None:
        client = self.module.app.test_client()
        response = client.post("/api/conversations", json={"model_id": "chat/v1.0"})
        self.assertEqual(403, response.status_code)

    def test_draft_creation_is_idempotent(self) -> None:
        client, token = self.client_with_csrf()
        headers = {"X-CSRF-Token": token}
        self.assertEqual([], client.get("/api/conversations").get_json()["conversations"])
        body = {"model_id": "chat/v1.0", "request_id": "draft-123"}
        first = client.post("/api/conversations", json=body, headers=headers)
        second = client.post("/api/conversations", json=body, headers=headers)
        self.assertEqual(first.get_json()["id"], second.get_json()["id"])
        self.assertTrue(second.get_json()["reused"])
        self.assertEqual(1, len(client.get("/api/conversations").get_json()["conversations"]))

    def test_conversation_model_cannot_change_silently(self) -> None:
        client, token = self.client_with_csrf()
        headers = {"X-CSRF-Token": token}
        created = client.post("/api/conversations", json={"model_id": "chat/v1.0"}, headers=headers).get_json()
        rejected = client.post(
            f"/api/conversations/{created['id']}/ask",
            json={"question": "Hello", "model_id": "other/v1"},
            headers=headers,
        )
        self.assertEqual(409, rejected.status_code)
        accepted = client.post(
            f"/api/conversations/{created['id']}/ask",
            json={"question": "Hello", "model_id": "chat/v1.0"},
            headers=headers,
        )
        self.assertEqual(200, accepted.status_code)
        self.assertEqual("chat/v1.0", accepted.get_json()["result"]["model_id"])

    def test_upload_response_hides_host_paths_and_checks_owner(self) -> None:
        client, token = self.client_with_csrf()
        response = client.post(
            "/api/uploads",
            data={"model_id": "chat/v1.0", "files": (io.BytesIO(b"hello"), "note.txt")},
            headers={"X-CSRF-Token": token},
            content_type="multipart/form-data",
        )
        self.assertEqual(201, response.status_code)
        attachment = response.get_json()["attachments"][0]
        serialized = json.dumps(attachment)
        self.assertNotIn("local_path", serialized)
        self.assertNotIn("stored_path", serialized)
        self.assertNotIn(str(Path(self.temporary.name).resolve()), serialized)

        other_client, other_token = self.client_with_csrf()
        conversation = other_client.post(
            "/api/conversations",
            json={"model_id": "chat/v1.0"},
            headers={"X-CSRF-Token": other_token},
        ).get_json()
        denied = other_client.post(
            f"/api/conversations/{conversation['id']}/ask",
            json={"question": "Use file", "model_id": "chat/v1.0", "attachment_ids": [attachment["id"]]},
            headers={"X-CSRF-Token": other_token},
        )
        self.assertEqual(400, denied.status_code)

    def test_username_routes_and_json_draft_persistence(self) -> None:
        client, token = self.client_with_csrf()
        registered = client.post(
            "/api/auth/register",
            json={"username": "kaan", "email": "kaan@example.com", "password": "strong-pass-123"},
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(200, registered.status_code)
        payload = registered.get_json()
        self.assertEqual("/kaan", payload["user"]["routes"]["home"])
        self.assertEqual(200, client.get("/kaan").status_code)
        self.assertEqual(200, client.get("/kaan/settings").status_code)

        state_response = client.put(
            "/api/state",
            json={"prompt": "unfinished", "panel": "settings", "draft_model_id": "chat/v1.0", "attachment_ids": []},
            headers={"X-CSRF-Token": payload["csrf_token"]},
        )
        self.assertEqual(200, state_response.status_code)
        draft_path = Path(os.environ["CROWAI_USERS_DIR"]) / "user kaan" / "draft.json"
        saved = json.loads(draft_path.read_text(encoding="utf-8"))
        self.assertEqual("unfinished", saved["draft"]["prompt"])


    def test_request_limit_is_separate_from_per_file_upload_limit(self) -> None:
        self.assertGreater(self.module.app.config["MAX_REQUEST_BYTES"], self.module.app.config["MAX_UPLOAD_BYTES"])
        self.assertEqual(self.module.app.config["MAX_REQUEST_BYTES"], self.module.app.config["MAX_CONTENT_LENGTH"])

    def test_auth_rotates_server_side_session_and_invalidates_old_sid(self) -> None:
        import uuid

        client, token = self.client_with_csrf()
        cookie_name = self.module.app.config["SESSION_COOKIE_NAME"]
        old_cookie = client.get_cookie(cookie_name)
        self.assertIsNotNone(old_cookie)
        old_sid = old_cookie.value
        suffix = uuid.uuid4().hex[:10]
        response = client.post(
            "/api/auth/register",
            json={"username": f"u{suffix}", "email": f"u{suffix}@example.com", "password": "strong-pass-123"},
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(200, response.status_code)
        new_sid = client.get_cookie(cookie_name).value
        self.assertNotEqual(old_sid, new_sid)
        database = self.module.app.extensions["crowai"].database
        self.assertIsNone(database.one("SELECT id FROM sessions WHERE id=?", (old_sid,)))
        self.assertIsNotNone(database.one("SELECT id FROM sessions WHERE id=?", (new_sid,)))

        replay = self.module.app.test_client()
        replay.set_cookie(cookie_name, old_sid)
        bootstrap = replay.get("/api/bootstrap").get_json()
        self.assertIsNone(bootstrap["user"])

    def test_clear_local_data_cascades_memory_and_calls_model_cleanup(self) -> None:
        client, token = self.client_with_csrf()
        headers = {"X-CSRF-Token": token}
        created = client.post("/api/conversations", json={"model_id": "chat/v1.0"}, headers=headers).get_json()
        conversation_id = created["id"]
        asked = client.post(
            f"/api/conversations/{conversation_id}/ask",
            json={"question": "my framework is Flask", "model_id": "chat/v1.0"},
            headers=headers,
        )
        self.assertEqual(200, asked.status_code)
        database = self.module.app.extensions["crowai"].database
        self.assertIsNotNone(database.one("SELECT conversation_id FROM conversation_memory WHERE conversation_id=?", (conversation_id,)))

        package = self.module.app.extensions["crowai"].registry._load("chat/v1.0")
        self.assertNotIn(conversation_id, package.DELETED)
        cleared = client.delete("/api/me/data", headers=headers)
        self.assertEqual(200, cleared.status_code)
        self.assertIsNone(database.one("SELECT id FROM conversations WHERE id=?", (conversation_id,)))
        self.assertIsNone(database.one("SELECT conversation_id FROM conversation_memory WHERE conversation_id=?", (conversation_id,)))
        self.assertIn(conversation_id, package.DELETED)

    def test_dotenv_does_not_override_process_environment_and_skips_production(self) -> None:
        from unittest.mock import patch
        import crowai.config as config

        base = Path(self.temporary.name) / "dotenv-case"
        base.mkdir(exist_ok=True)
        (base / ".env").write_text("CROWAI_PORT=6123\nCROWAI_HOST=0.0.0.0\n", encoding="utf-8")
        with patch.object(config, "PROJECT_ROOT", base), patch.dict(os.environ, {"CROWAI_PORT": "7000", "CROWAI_ENV": "development"}, clear=False):
            os.environ.pop("CROWAI_HOST", None)
            config.load_project_environment()
            self.assertEqual("7000", os.environ["CROWAI_PORT"])
            self.assertEqual("0.0.0.0", os.environ["CROWAI_HOST"])
            os.environ.pop("CROWAI_HOST", None)

        with patch.object(config, "PROJECT_ROOT", base), patch.dict(os.environ, {"CROWAI_ENV": "production"}, clear=False):
            os.environ.pop("CROWAI_HOST", None)
            config.load_project_environment()
            self.assertIsNone(os.environ.get("CROWAI_HOST"))

    def test_session_cookie_is_http_only_and_same_site(self) -> None:
        client = self.module.app.test_client()
        response = client.get("/api/bootstrap")
        cookie = response.headers.get("Set-Cookie", "")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)


if __name__ == "__main__":
    unittest.main()
