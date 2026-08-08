from __future__ import annotations

import re

VERSION = 2
_RESERVED = {"api", "static", "health", "favicon", "favicon.ico"}


def _seed(value: str) -> str:
    seed = re.sub(r"[^a-z0-9_-]+", "_", str(value or "").strip().casefold()).strip("_-")
    if not seed or not seed[0].isalnum():
        seed = f"user_{seed}"
    if len(seed) < 3:
        seed = f"{seed}_user"
    if seed in _RESERVED:
        seed = f"{seed}_user"
    return seed[:32]


def apply(connection):
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(users)").fetchall()}
    if "username" not in columns:
        connection.execute("ALTER TABLE users ADD COLUMN username TEXT")
    rows = connection.execute("SELECT id,username,email,display_name FROM users ORDER BY id").fetchall()
    used: set[str] = set()
    for row in rows:
        current = str(row["username"] or "").strip().casefold()
        base = _seed(current or str(row["email"]).split("@", 1)[0] or row["display_name"])
        candidate = base
        suffix = 2
        while candidate.casefold() in used:
            tail = f"_{suffix}"
            candidate = f"{base[:32-len(tail)]}{tail}"
            suffix += 1
        used.add(candidate.casefold())
        if current != candidate:
            connection.execute("UPDATE users SET username=? WHERE id=?", (candidate, int(row["id"])))
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users(username COLLATE NOCASE)")
