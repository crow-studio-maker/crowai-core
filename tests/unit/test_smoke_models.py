from __future__ import annotations

from typing import Any

from tools import smoke_models


class _FakeService:
    def execute(self, **kwargs: Any) -> dict[str, Any]:
        question = str(kwargs.get("question") or "")
        attachments = kwargs.get("attachments") or []
        if smoke_models.CHAT_TAIL_MARKER in question:
            return {"success": True, "answer": smoke_models.CHAT_TAIL_MARKER}
        if attachments:
            return {"success": True, "answer": smoke_models.AGENT_DOC_MARKER}
        raise AssertionError("unexpected fake-service request")


def test_chat_smoke_checks_exact_response_and_request_tail() -> None:
    ok, evidence = smoke_models._verify_chat(
        _FakeService(),
        "chat/v1.0",
        {"success": True, "answer": smoke_models.CHAT_EXPECTED},
    )
    assert ok is True
    assert evidence["deterministic_short_response"] is True
    assert evidence["tail_marker_preserved"] is True
    assert evidence["tail_request_chars"] > 4000


def test_code_smoke_requires_valid_python_and_disabled_host_execution() -> None:
    response = {
        "success": True,
        "artifacts": [{"path": "main.py", "code": "print('CrowAI code smoke OK')\n"}],
        "meta": {
            "python_execution": {
                "executed": False,
                "requested": False,
                "backend": "disabled",
            }
        },
    }
    ok, evidence = smoke_models._verify_code(response)
    assert ok is True
    assert evidence["syntax_validation"] is True
    assert evidence["host_execution_disabled"] is True

    response["artifacts"][0]["code"] = "if:\n"
    ok, evidence = smoke_models._verify_code(response)
    assert ok is False
    assert evidence["syntax_validation"] is False


def test_agent_smoke_requires_reasoning_and_local_document_marker() -> None:
    ok, evidence = smoke_models._verify_agent(
        _FakeService(),
        "agent/v1.0",
        {"success": True, "answer": "4"},
    )
    assert ok is True
    assert evidence == {
        "local_reasoning": True,
        "local_document_analysis": True,
        "web_required": False,
    }
