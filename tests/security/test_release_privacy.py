from __future__ import annotations

from pathlib import Path


def test_gitignore_excludes_runtime_private_data() -> None:
    root = Path(__file__).resolve().parents[2]
    content = (root / ".gitignore").read_text(encoding="utf-8")
    for required in ("instance/*", "users/*", "uploads/*", ".env", "secret.key", "*.sqlite3"):
        assert required in content
