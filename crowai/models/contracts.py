from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

JSONValue = None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]


@runtime_checkable
class ModelPackage(Protocol):
    def prepare_request(
        self,
        *,
        question: str,
        language: str,
        interaction_mode: str,
        conversation: list[dict[str, str]],
        attachments: list[dict[str, Any]],
        memory_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def finalize_result(
        self,
        *,
        question: str,
        language: str,
        interaction_mode: str,
        result: dict[str, Any],
    ) -> dict[str, Any]: ...


KNOWN_CAPABILITIES = frozenset({
    "conversation", "attachments", "file_inspection", "web_search", "network", "code", "tools",
    "direct_code_generation", "document_analysis", "follow_up_editing", "language_matching",
    "multi_file", "multimodal", "no_web", "product_comparison", "project_generation",
    "project_memory", "repair_pass", "safe_python_runner", "python_execution",
    "isolated_python_runner", "structured_code_task", "syntax_validation", "vision",
})
