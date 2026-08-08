VERSION = 4

SQL = """
CREATE TABLE IF NOT EXISTS conversation_memory(
    conversation_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL DEFAULT 1,
    summary TEXT NOT NULL DEFAULT '',
    facts_json TEXT NOT NULL DEFAULT '[]',
    mode_state_json TEXT NOT NULL DEFAULT '{}',
    turn_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_conversation_memory_updated ON conversation_memory(updated_at);
"""


def apply(connection):
    for statement in SQL.split(";"):
        statement = statement.strip()
        if statement:
            connection.execute(statement)
