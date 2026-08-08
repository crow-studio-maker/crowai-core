from __future__ import annotations

from crowai.models.service import sanitize_public_value


def test_sanitizer_preserves_safe_relative_artifact_paths() -> None:
    value = sanitize_public_value({
        "artifact": {"path": "src/app/main.py", "filename": "src/app/main.py"},
    })
    assert value["artifact"]["path"] == "src/app/main.py"


def test_sanitizer_removes_local_and_traversal_paths() -> None:
    value = sanitize_public_value({
        "absolute": {"path": "/" + "home/user/secret.txt"},
        "windows": {"path": "C:/secret.txt"},
        "traversal": {"path": "../secret.txt"},
        "private": {"_internal_path": "/tmp/upload"},
    })
    assert "path" not in value["absolute"]
    assert "path" not in value["windows"]
    assert "path" not in value["traversal"]
    assert "_internal_path" not in value["private"]
