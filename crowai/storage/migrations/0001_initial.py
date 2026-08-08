VERSION = 1

SQL = """
CREATE TABLE IF NOT EXISTS users(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 username TEXT UNIQUE COLLATE NOCASE,
 email TEXT UNIQUE COLLATE NOCASE NOT NULL,
 password_hash TEXT NOT NULL,
 display_name TEXT NOT NULL DEFAULT '',
 settings_json TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL,
 last_login TEXT
);
CREATE TABLE IF NOT EXISTS sessions(
 id TEXT PRIMARY KEY,
 data_json TEXT NOT NULL DEFAULT '{}',
 expires_at TEXT NOT NULL,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at);
CREATE TABLE IF NOT EXISTS conversations(
 id TEXT PRIMARY KEY,
 owner_key TEXT NOT NULL,
 title TEXT NOT NULL,
 model_id TEXT NOT NULL,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conversations_owner ON conversations(owner_key, updated_at DESC);
CREATE TABLE IF NOT EXISTS messages(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 conversation_id TEXT NOT NULL,
 role TEXT NOT NULL,
 content TEXT NOT NULL,
 payload_json TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL,
 FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, id);
CREATE TABLE IF NOT EXISTS uploads(
 id TEXT PRIMARY KEY,
 owner_key TEXT NOT NULL,
 original_name TEXT NOT NULL,
 stored_path TEXT NOT NULL,
 media_type TEXT NOT NULL,
 size_bytes INTEGER NOT NULL,
 sha256 TEXT NOT NULL,
 metadata_json TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_uploads_owner ON uploads(owner_key, created_at DESC);
CREATE TABLE IF NOT EXISTS message_uploads(
 message_id INTEGER NOT NULL,
 upload_id TEXT NOT NULL,
 PRIMARY KEY(message_id, upload_id),
 FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE CASCADE,
 FOREIGN KEY(upload_id) REFERENCES uploads(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_message_uploads_upload ON message_uploads(upload_id);
CREATE TABLE IF NOT EXISTS conversation_creations(
 owner_key TEXT NOT NULL,
 request_key TEXT NOT NULL,
 conversation_id TEXT NOT NULL,
 created_at TEXT NOT NULL,
 PRIMARY KEY(owner_key, request_key),
 FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);
"""


def apply(connection):
    for statement in SQL.split(";"):
        statement = statement.strip()
        if statement:
            connection.execute(statement)
