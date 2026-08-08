# Troubleshooting

## “No models installed”

This is a supported state. Install a reviewed package under `models/<mode>/<version>/` or set `CROWAI_MODELS_DIR`, then restart or enable development reload.

## Settings or drafts appear missing

Confirm you are signed into the same username and that `CROWAI_USERS_DIR` and the SQLite instance directory are stable/writable. SQLite is authoritative; snapshots are rebuilt at bootstrap.

## Upload rejected

Check configured byte/count limits, executable extension/magic, archive expansion ratio, filename length, and server logs using the response request ID. Local paths are intentionally not returned.

## Production refuses to start

Set `CROWAI_ENV=production`, provide a secret of at least 32 characters, keep debug off, and retain secure cookies/HTTPS.

## Database locked

Use a local filesystem, avoid copying a live SQLite database, keep transactions short, and ensure only supported processes share the file. WAL and busy timeout are enabled automatically.
