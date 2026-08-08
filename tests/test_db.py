from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from crowai.db import Database, utcnow


class DatabaseTests(unittest.TestCase):
    def test_conversation_delete_cascades_messages_and_creation_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "test.db")
            now = utcnow()
            database.execute(
                "INSERT INTO conversations(id,owner_key,title,model_id,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                ("conversation", "guest:test", "Title", "chat/v1", now, now),
            )
            message_id = database.execute(
                "INSERT INTO messages(conversation_id,role,content,payload_json,created_at) VALUES(?,?,?,?,?)",
                ("conversation", "user", "Hello", "{}", now),
            )
            database.execute(
                "INSERT INTO uploads(id,owner_key,original_name,stored_path,media_type,size_bytes,sha256,metadata_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                ("upload", "guest:test", "a.txt", "/internal/a.txt", "text/plain", 1, "x", "{}", now),
            )
            database.execute("INSERT INTO message_uploads(message_id,upload_id) VALUES(?,?)", (message_id, "upload"))
            database.execute(
                "INSERT INTO conversation_creations(owner_key,request_key,conversation_id,created_at) VALUES(?,?,?,?)",
                ("guest:test", "request", "conversation", now),
            )
            database.execute("DELETE FROM conversations WHERE id=?", ("conversation",))
            self.assertIsNone(database.one("SELECT id FROM messages WHERE conversation_id=?", ("conversation",)))
            self.assertIsNone(database.one("SELECT conversation_id FROM conversation_creations WHERE conversation_id=?", ("conversation",)))
            self.assertIsNone(database.one("SELECT message_id FROM message_uploads WHERE upload_id=?", ("upload",)))
            self.assertIsNotNone(database.one("SELECT id FROM uploads WHERE id=?", ("upload",)))

    def test_existing_user_table_receives_unique_username(self) -> None:
        import sqlite3

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "legacy.db"
            connection = sqlite3.connect(path)
            connection.execute(
                "CREATE TABLE users(id INTEGER PRIMARY KEY AUTOINCREMENT,email TEXT UNIQUE,password_hash TEXT,display_name TEXT,settings_json TEXT DEFAULT '{}',created_at TEXT,last_login TEXT)"
            )
            connection.execute(
                "INSERT INTO users(email,password_hash,display_name,created_at) VALUES(?,?,?,?)",
                ("kaan@example.com", "x", "Kaan", utcnow()),
            )
            connection.commit()
            connection.close()
            database = Database(path)
            row = database.one("SELECT username FROM users WHERE email=?", ("kaan@example.com",))
            self.assertEqual("kaan", row["username"])

    def test_transaction_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "test.db")
            now = utcnow()
            with self.assertRaises(RuntimeError):
                with database.transaction() as connection:
                    connection.execute(
                        "INSERT INTO conversations(id,owner_key,title,model_id,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                        ("conversation", "guest:test", "Title", "chat/v1", now, now),
                    )
                    raise RuntimeError("rollback")
            self.assertIsNone(database.one("SELECT id FROM conversations WHERE id=?", ("conversation",)))


if __name__ == "__main__":
    unittest.main()
