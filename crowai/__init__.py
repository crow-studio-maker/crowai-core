from __future__ import annotations

from typing import Any, Mapping


def create_app(config: Mapping[str, Any] | None = None):
    """Create a configured CrowAI Flask application without import-time side effects."""
    from crowai.application import create_app as factory

    return factory(config)


__all__ = ["create_app"]
