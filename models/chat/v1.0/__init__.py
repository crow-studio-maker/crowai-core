"""CrowAI Chat V1.0 entry point."""

from .engine import cancel, health_check, shutdown
from .pipeline import finalize_result, prepare_request


def cancel_conversation(*, conversation_id: str) -> None:
    """Cancel active inference for the conversation-bound single-slot backend."""
    del conversation_id
    cancel()


__all__ = [
    "prepare_request",
    "health_check",
    "finalize_result",
    "cancel_conversation",
    "shutdown",
]
