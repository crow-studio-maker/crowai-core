from __future__ import annotations

import pytest

from crowai.conversations.schemas import AskRequest, CreateConversationRequest
from crowai.errors import ValidationError


def test_create_request_rejects_non_object() -> None:
    with pytest.raises(ValidationError):
        CreateConversationRequest.parse([])


def test_ask_rejects_duplicate_attachments() -> None:
    identifier = "a" * 32
    with pytest.raises(ValidationError):
        AskRequest.parse(
            {"question": "hello", "model_id": "chat/v1", "attachment_ids": [identifier, identifier]},
            maximum_message_length=100,
        )


def test_attachment_only_request_is_allowed() -> None:
    parsed = AskRequest.parse(
        {"question": "", "model_id": "chat/v1", "attachment_ids": ["a" * 32]},
        maximum_message_length=100,
    )
    assert parsed.question == ""
