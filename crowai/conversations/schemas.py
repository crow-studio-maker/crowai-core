from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from crowai.errors import ValidationError

_REQUEST_KEY = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}/[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def object_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError("The request body must be a JSON object.")
    return value


def request_key(value: Any) -> str:
    key = str(value or "").strip()
    if key and not _REQUEST_KEY.fullmatch(key):
        raise ValidationError("The request identifier is invalid.")
    return key


def model_id(value: Any) -> str:
    identifier = str(value or "").strip().casefold()
    if not _MODEL_ID.fullmatch(identifier):
        raise ValidationError("Select a valid model.")
    return identifier


def attachment_ids(value: Any, *, maximum: int = 10) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValidationError("attachment_ids must be a list.")
    if len(value) > maximum:
        raise ValidationError(f"A maximum of {maximum} attachments is allowed.")
    output: list[str] = []
    for raw in value:
        identifier = str(raw or "").strip()
        if not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise ValidationError("One or more attachment identifiers are invalid.")
        if identifier in output:
            raise ValidationError("Duplicate attachment identifiers are not allowed.")
        output.append(identifier)
    return tuple(output)


@dataclass(frozen=True)
class CreateConversationRequest:
    model_id: str
    request_key: str

    @classmethod
    def parse(cls, value: Any) -> "CreateConversationRequest":
        data = object_payload(value)
        return cls(model_id(data.get("model_id")), request_key(data.get("request_id")))


@dataclass(frozen=True)
class RenameConversationRequest:
    title: str

    @classmethod
    def parse(cls, value: Any) -> "RenameConversationRequest":
        data = object_payload(value)
        title = " ".join(str(data.get("title") or "").split()).strip()
        if not title or len(title) > 120:
            raise ValidationError("Conversation title must be between 1 and 120 characters.")
        return cls(title)


@dataclass(frozen=True)
class AskRequest:
    question: str
    model_id: str
    language: str
    interaction_mode: str
    attachment_ids: tuple[str, ...]
    request_key: str
    execution: dict[str, Any]

    @classmethod
    def parse(cls, value: Any, *, maximum_message_length: int, maximum_attachments: int = 10) -> "AskRequest":
        data = object_payload(value)
        attachments = attachment_ids(data.get("attachment_ids"), maximum=maximum_attachments)
        question = str(data.get("question") or data.get("message") or "").strip()
        if not question and not attachments:
            raise ValidationError("Enter a message or attach a file.")
        if len(question) > maximum_message_length:
            raise ValidationError(f"Messages may contain at most {maximum_message_length} characters.")
        language = str(data.get("language") or "auto").strip()[:20]
        interaction_mode = str(data.get("interaction_mode") or "conversation").strip()[:40]
        if interaction_mode != "conversation":
            raise ValidationError("The interaction mode is not supported by this Core route.")
        raw_execution = data.get("execution")
        if raw_execution is None:
            execution = {"allow": False, "backend": "isolated"}
        else:
            if not isinstance(raw_execution, dict):
                raise ValidationError("execution must be an object when provided.")
            allow = raw_execution.get("allow", False)
            if not isinstance(allow, bool):
                raise ValidationError("execution.allow must be a boolean.")
            backend = str(raw_execution.get("backend") or "isolated").strip().casefold()
            if backend not in {"isolated", "trusted-local"}:
                raise ValidationError("execution.backend must be isolated or trusted-local.")
            execution = {"allow": allow, "backend": backend}
        return cls(
            question=question,
            model_id=model_id(data.get("model_id")),
            language=language or "auto",
            interaction_mode=interaction_mode,
            attachment_ids=attachments,
            request_key=request_key(data.get("request_id")),
            execution=execution,
        )
