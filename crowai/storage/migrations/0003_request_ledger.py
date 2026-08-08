VERSION = 3


SQL = """
CREATE TABLE IF NOT EXISTS request_ledger(
    owner_key TEXT NOT NULL,
    request_key TEXT NOT NULL,
    operation TEXT NOT NULL,
    state TEXT NOT NULL,
    response_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(owner_key, request_key, operation)
);
CREATE INDEX IF NOT EXISTS idx_request_ledger_updated ON request_ledger(updated_at);
"""


def apply(connection):
    for statement in SQL.split(";"):
        statement = statement.strip()
        if statement:
            connection.execute(statement)
