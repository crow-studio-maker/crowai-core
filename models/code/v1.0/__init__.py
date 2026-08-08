"""CrowAI Code V1.0 public package contract."""

from .engine import cancel, health_check, shutdown
from .pipeline import finalize_result, prepare_request
from .tools import inspect_file


def cancel_conversation(*, conversation_id: str) -> None:
    """Cancel active Code inference for a deleted conversation."""
    del conversation_id
    cancel()

__all__ = [
    "prepare_request",
    "health_check",
    "finalize_result",
    "inspect_file",
    "shutdown",
    "cancel_conversation",
]
