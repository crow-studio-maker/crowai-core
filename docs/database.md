# Database

CrowAI uses short-lived SQLite connections with `foreign_keys=ON`, WAL mode, a busy timeout, UTC timestamps, explicit write transactions, and indexes aligned with owner/conversation lookups.

Numbered migrations live in `crowai/storage/migrations/` and are recorded in `schema_migrations`. Migrations run in order and roll back on failure. Existing pre-username databases are upgraded by migration 0002.

Primary tables include users, sessions, conversations, messages, uploads, message-upload links, conversation creation idempotency, and the general request ledger.

`/health` runs `PRAGMA integrity_check` and reports database readiness without exposing SQL or paths.
