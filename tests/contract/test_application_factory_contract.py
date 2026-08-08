from __future__ import annotations

from pathlib import Path


def test_compatibility_entry_point_is_thin() -> None:
    root = Path(__file__).resolve().parents[2]
    source = (root / "app.py").read_text(encoding="utf-8")
    assert "from crowai.application import create_app" in source
    assert "app = create_app()" in source
    assert len(source.splitlines()) < 20
