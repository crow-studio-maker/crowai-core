VERSION = 5


def apply(connection):
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(request_ledger)").fetchall()}
    if "lease_expires_at" not in columns:
        connection.execute("ALTER TABLE request_ledger ADD COLUMN lease_expires_at TEXT")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_request_ledger_lease ON request_ledger(state, lease_expires_at)")
