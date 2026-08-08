from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from crowai.models.service import ModelService
from models.registry import ModelRegistry

CHAT_EXPECTED = "CrowAI chat smoke OK"
CHAT_TAIL_MARKER = "CROWAI-CHAT-TAIL-7F3A"
AGENT_DOC_MARKER = "CROWAI-DOC-ALPHA-42"
PROMPTS = {
    "chat": f"Reply with exactly: {CHAT_EXPECTED}",
    "code": "Create one small Python file named main.py that prints exactly: CrowAI code smoke OK",
    "agent": "Without web access, answer this local reasoning check briefly: what is 2 + 2?",
}


def _base_snapshot() -> dict[str, Any]:
    return {
        "summary": "",
        "relevant_facts": [],
        "mode_state": {},
        "recent_messages": [],
        "request_options": {},
    }


def _execute(service: ModelService, model_id: str, *, question: str, attachments: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return service.execute(
        model_id=model_id,
        question=question,
        language="en",
        conversation=[],
        attachments=attachments or [],
        snapshot=_base_snapshot(),
    )


def _normalized(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _verify_chat(service: ModelService, model_id: str, first: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    answer = _normalized(first.get("answer"))
    exact_response = answer.casefold() == CHAT_EXPECTED.casefold()

    # This is intentionally long enough to exercise the current-message budgeting
    # path while remaining well below the configured model context. The tail marker
    # proves that the request tail was not silently sliced before generation.
    filler = "context-budget-smoke " * 220
    tail = _execute(
        service,
        model_id,
        question=(
            "Ignore the filler and reply with exactly the marker at the very end of this request.\n"
            f"{filler}\nMARKER: {CHAT_TAIL_MARKER}"
        ),
    )
    tail_answer = _normalized(tail.get("answer"))
    tail_preserved = bool(tail.get("success")) and CHAT_TAIL_MARKER.casefold() in tail_answer.casefold()
    return exact_response and tail_preserved, {
        "deterministic_short_response": exact_response,
        "tail_marker_preserved": tail_preserved,
        "tail_request_chars": len(filler) + len(CHAT_TAIL_MARKER),
    }


def _verify_code(first: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    artifacts = first.get("artifacts") if isinstance(first.get("artifacts"), list) else []
    python_artifacts = [
        item for item in artifacts
        if isinstance(item, dict) and str(item.get("path") or item.get("filename") or "").casefold().endswith(".py")
    ]
    syntax_ok = False
    syntax_error = ""
    if python_artifacts:
        item = python_artifacts[0]
        source = str(item.get("code") or "")
        filename = str(item.get("path") or item.get("filename") or "main.py")
        try:
            compile(source, filename, "exec")
            syntax_ok = True
        except (SyntaxError, ValueError, TypeError) as exc:
            syntax_error = f"{type(exc).__name__}: {exc}"

    meta = first.get("meta") if isinstance(first.get("meta"), dict) else {}
    execution = meta.get("python_execution") if isinstance(meta.get("python_execution"), dict) else {}
    host_execution_disabled = (
        execution.get("executed") is False
        and execution.get("requested") is False
        and str(execution.get("backend") or "disabled") == "disabled"
    )
    ok = bool(first.get("success")) and bool(python_artifacts) and syntax_ok and host_execution_disabled
    return ok, {
        "python_artifact_generated": bool(python_artifacts),
        "syntax_validation": syntax_ok,
        "syntax_error": syntax_error,
        "host_execution_disabled": host_execution_disabled,
        "execution_backend": str(execution.get("backend") or ""),
    }


def _verify_agent(service: ModelService, model_id: str, first: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    reasoning_answer = _normalized(first.get("answer"))
    reasoning_ok = bool(first.get("success")) and "4" in reasoning_answer
    document = _execute(
        service,
        model_id,
        question="Read the attached local document only. Reply with the exact verification token it contains.",
        attachments=[
            {
                "name": "crowai-smoke.txt",
                "media_type": "text/plain",
                "status": "inspected",
                "summary": "Synthetic local smoke-test document.",
                "text": f"Verification token: {AGENT_DOC_MARKER}",
                "size_bytes": 64,
            }
        ],
    )
    document_answer = _normalized(document.get("answer"))
    document_ok = bool(document.get("success")) and AGENT_DOC_MARKER.casefold() in document_answer.casefold()
    return reasoning_ok and document_ok, {
        "local_reasoning": reasoning_ok,
        "local_document_analysis": document_ok,
        "web_required": False,
    }


def _mode_verification(
    service: ModelService,
    *,
    mode: str,
    model_id: str,
    response: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    if mode == "chat":
        return _verify_chat(service, model_id, response)
    if mode == "code":
        return _verify_code(response)
    if mode == "agent":
        return _verify_agent(service, model_id, response)
    return bool(response.get("success")), {"basic_response": bool(response.get("success"))}


def _smoke_one(registry: ModelRegistry, model: dict[str, Any]) -> dict[str, Any]:
    model_id = str(model["id"])
    mode = str(model["mode"])
    readiness = registry.readiness(model_id)
    if readiness.get("runnable") is not True:
        return {
            "model_id": model_id,
            "result": "SKIPPED",
            "reason": readiness.get("status") or "unavailable",
            "missing_requirements": readiness.get("missing_requirements") or [],
            "invalid_requirements": readiness.get("invalid_requirements") or [],
        }

    service = ModelService(registry, enable_web_search=False)
    started = time.monotonic()
    try:
        before = registry.health_check(model_id)
        response = _execute(service, model_id, question=PROMPTS.get(mode, "Reply briefly with: CrowAI smoke OK"))
        after = registry.health_check(model_id)
        mode_ok, verification = _mode_verification(
            service,
            mode=mode,
            model_id=model_id,
            response=response,
        )
    except Exception as exc:
        return {
            "model_id": model_id,
            "result": "FAIL",
            "latency_ms": int((time.monotonic() - started) * 1000),
            "error": type(exc).__name__,
        }

    # Each engine's is_ready() verifies the package-owned llama-server's
    # advertised alias, so backend_running_after is also the alias proof.
    backend_running = bool(after.get("backend_running"))
    passed = bool(response.get("success")) and backend_running and mode_ok
    return {
        "model_id": model_id,
        "result": "PASS" if passed else "FAIL",
        "latency_ms": int((time.monotonic() - started) * 1000),
        "backend_running_before": bool(before.get("backend_running")),
        "backend_running_after": backend_running,
        "alias_verified": backend_running,
        "response_status": str(response.get("status") or ""),
        "artifact_count": len(response.get("artifacts") or []),
        "verification": verification,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run optional real local-model smoke checks for CrowAI V1.0.")
    parser.add_argument("--model", action="append", default=[], help="Model id to test; may be repeated.")
    args = parser.parse_args(argv)

    registry = ModelRegistry(ROOT / "models", development=False, strict_capabilities=True)
    try:
        selected = {str(value).strip().casefold() for value in args.model if str(value).strip()}
        models = [item for item in registry.list_models() if not selected or item["id"] in selected]
        results = [_smoke_one(registry, item) for item in models]
    finally:
        registry.shutdown()

    print(json.dumps({"real_model_smoke": results}, ensure_ascii=False, indent=2))
    return 1 if any(item["result"] == "FAIL" for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
