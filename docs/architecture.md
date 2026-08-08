# Architecture

## Request path

```text
Browser
  ↓
Flask application factory
  ├─ request context / request ID
  ├─ auth + server-side sessions + CSRF
  ├─ workspace pages and draft state
  ├─ conversation API
  ├─ upload API
  ├─ frozen Settings API
  └─ health/bootstrap API
       ↓
Services (business rules and ownership)
       ↓
Repositories (SQL only) / model registry / snapshot cache
       ↓
SQLite + private filesystem roots
```

`crowai.application.create_app()` loads and validates configuration, initializes isolated runtime objects, registers Blueprints, error handlers, request hooks, logging, and security headers. `app.py` is only a compatibility WSGI entry point.

## Modules

- `crowai/auth/` — password validation, rate limits, session/CSRF rotation, auth repository/service/routes.
- `crowai/conversations/` — typed requests, SQL repository, ownership and model orchestration service, API routes.
- `crowai/uploads/` — atomic storage, inspection, ownership, SQL repository, API routes.
- `crowai/models/` — stable callback contracts, result sanitization, search orchestration.
- `crowai/settings/` — frozen Settings service and routes.
- `crowai/storage/` — SQLite wrapper, numbered migrations, sessions, idempotency ledger.
- `crowai/users/` and `crowai/user_store.py` — rebuildable private JSON snapshots.
- `models/registry.py` — manifest discovery and trusted plugin loading.

## Source of truth

SQLite is authoritative. Per-user JSON files are atomic, human-readable snapshots for persistence/inspection and can be rebuilt from SQLite. They must not be edited concurrently as a second database.

## Compatibility

Public API paths and username-based page routes are preserved. Existing imports from `crowai.db` and `crowai.session_store` remain compatibility shims. The Settings markup/API contract is frozen by tests.
