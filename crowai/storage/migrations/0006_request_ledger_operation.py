VERSION = 6


def apply(connection):
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_request_ledger_operation_state "
        "ON request_ledger(owner_key, operation, state, lease_expires_at)"
    )
