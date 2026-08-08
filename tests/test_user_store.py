from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from crowai.db import Database, utcnow
from crowai.user_store import UserJSONStore, validate_username


class UserStoreTests(unittest.TestCase):
    def test_username_validation_and_reserved_names(self) -> None:
        self.assertEqual("kaan_01", validate_username("Kaan_01"))
        for invalid in ("ab", "ad soyad", "../kaan", "api", "static"):
            with self.assertRaises(ValueError):
                validate_username(invalid)

    def test_user_folder_contains_settings_draft_and_full_conversation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = Database(root / "instance" / "workspace.db")
            now = utcnow()
            user_id = database.execute(
                "INSERT INTO users(username,email,password_hash,display_name,settings_json,created_at) VALUES(?,?,?,?,?,?)",
                ("kaan", "kaan@example.com", "not-used", "Kaan", json.dumps({"appearance": "dark"}), now),
            )
            database.execute(
                "INSERT INTO conversations(id,owner_key,title,model_id,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                ("chat-1", f"user:{user_id}", "Persistent chat", "chat/v1", now, now),
            )
            database.execute(
                "INSERT INTO messages(conversation_id,role,content,payload_json,created_at) VALUES(?,?,?,?,?)",
                ("chat-1", "user", "Yarım kalan mesaj", "{}", now),
            )
            user = database.one(
                "SELECT id,username,email,display_name,created_at,last_login FROM users WHERE id=?",
                (user_id,),
            )
            assert user is not None
            store = UserJSONStore(root / "users", database)
            store.save_draft(user, {
                "prompt": "Gönderilmemiş taslak",
                "current_id": "chat-1",
                "panel": "workspace",
                "attachment_ids": [],
            })
            store.sync_all(user)

            directory = root / "users" / "user kaan"
            self.assertTrue((directory / "account.json").is_file())
            self.assertTrue((directory / "settings.json").is_file())
            self.assertTrue((directory / "draft.json").is_file())
            self.assertTrue((directory / "conversations.json").is_file())
            self.assertTrue((directory / "conversations" / "conversation_chat-1.json").is_file())

            draft = json.loads((directory / "draft.json").read_text(encoding="utf-8"))["draft"]
            self.assertEqual("Gönderilmemiş taslak", draft["prompt"])
            conversations = json.loads((directory / "conversations.json").read_text(encoding="utf-8"))["conversations"]
            self.assertEqual("Persistent chat", conversations[0]["title"])
            self.assertEqual("Yarım kalan mesaj", conversations[0]["messages"][0]["content"])
            self.assertTrue(conversations[0]["incomplete"])


if __name__ == "__main__":
    unittest.main()
