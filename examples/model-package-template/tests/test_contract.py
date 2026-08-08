from pathlib import Path


def test_template_is_explicitly_nonfunctional() -> None:
    source = (Path(__file__).resolve().parents[1] / "__init__.py").read_text(encoding="utf-8")
    assert "template" in source.casefold()
    assert "raise RuntimeError" in source
