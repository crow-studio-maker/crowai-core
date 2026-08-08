# Configuration

For development, `crowai.application.create_app(config)` passes the app-factory overrides into `load_project_environment(config)` before constructing Flask/Core configuration. It loads the project `.env` with `override=False`, so explicit process environment variables always win. If either the programmatic factory override or the process environment selects `production`/`prod`, project `.env` loading is skipped before any dotenv value can affect the application.

All bundled local entry points default to loopback. Set `CROWAI_HOST=0.0.0.0` only when you intentionally want LAN exposure and have considered firewall, TLS/reverse-proxy, authentication and host-network risks. `CROWAI_TRUSTED_PROXIES` is not supported and is intentionally absent rather than acting as a security-looking no-op.

| Variable | Purpose | Default |
|---|---|---|
| `CROWAI_ENV` | `development` or `production` | `development` |
| `CROWAI_HOST` | Bind address for `app.py`/`serve.py` | `127.0.0.1` |
| `CROWAI_PORT` | HTTP port | `5000` |
| `CROWAI_SECRET_KEY` | Stable signing secret; at least 32 chars in production | generated private dev file |
| `CROWAI_INSTANCE_DIR` | SQLite and development secret root | `instance` |
| `CROWAI_UPLOAD_DIR` | Private stored uploads | `uploads` |
| `CROWAI_MODELS_DIR` | Immutable model package root; read-only/absent is supported in production | `models` |
| `CROWAI_MODEL_STATE_DIR` | Private mutable model logs/cache/debug state; must stay inside `CROWAI_INSTANCE_DIR` | `instance/model_state` |
| `CROWAI_USERS_DIR` | Private user JSON snapshots | `users` |
| `CROWAI_MAX_MESSAGE_LENGTH` | Maximum message/draft characters | `12000` |
| `CROWAI_MAX_UPLOAD_BYTES` | **Per-file** upload limit | `33554432` (32 MiB) |
| `CROWAI_MAX_REQUEST_BYTES` | Whole HTTP request limit, allowing bounded multi-file requests | `134217728` (128 MiB) |
| `CROWAI_MAX_UPLOAD_FILES` | Files per request | `10` |
| `CROWAI_SESSION_DAYS` | Signed-in session lifetime | `7` |
| `CROWAI_LOG_LEVEL` | Python log level | `INFO` |
| `CROWAI_ENABLE_WEB_SEARCH` | Permit Core search orchestration | `true` |
| `CROWAI_STRICT_MODEL_CAPABILITIES` | Reject unknown package capabilities | `false` |
| `CROWAI_MODEL_DEVELOPMENT_RELOAD` | Deterministic content-hash reload in development | environment-dependent |

Programmatic overrides and environment variables go through the same type/range normalization for CrowAI-managed settings. Core-derived security values are applied last and cannot be weakened through the factory override mapping: production always forces strict private permissions, disables debug/testing/model hot reload, uses HttpOnly+Secure session cookies, and derives Flask `MAX_CONTENT_LENGTH` from the validated bounded `MAX_REQUEST_BYTES`. Production fails closed if the secret is weak/missing. On POSIX, configured private runtime directories are hardened to `0700` and private database/upload/user files to `0600`, including existing broader files discovered at startup; nested database directories below `CROWAI_INSTANCE_DIR` are also forced to `0700`, and a strict production database path outside that private root is rejected. A production permission-hardening failure is fatal. Model packages are read-only application inputs: production neither requires `CROWAI_MODELS_DIR` to be writable nor creates it when absent. All package-owned mutable state is redirected to `CROWAI_MODEL_STATE_DIR`; on POSIX that state tree is private (`0700` directories and `0600` files). On Windows, CrowAI keeps best-effort file modes but the real confidentiality boundary is the account/service ACL configured by the operator. The request limit remains finite even when multiple files are allowed; a request exceeding it is rejected by Flask before normal upload processing.
