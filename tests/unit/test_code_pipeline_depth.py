from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest

from models.registry import ModelRegistry

ROOT = Path(__file__).resolve().parents[2]


def _pipeline():
    registry = ModelRegistry(ROOT / "models", development=True, strict_capabilities=True)
    package = registry._load("code/v1.0")
    return importlib.import_module(f"{package.__name__}.pipeline")


def test_code_pipeline_helpers_cover_paths_formats_and_task_routing() -> None:
    p = _pipeline()
    assert p._safe_path("src/app.py") == "src/app.py"
    for bad in ("", "../x.py", "/x.py", "C:/x.py", "a/../x.py"):
        assert p._safe_path(bad) is None
    assert p._language_from_path("a.py") == "python"
    assert p._language_from_path("a.ts") == "typescript"
    assert p._language_from_path("README.unknown") == "text"
    assert p._strip_markdown_fence("```python\nprint(1)\n```") == "print(1)"
    assert p._extract_json_object("prefix {\"files\": []} suffix") == {"files": []}
    with pytest.raises(p.LocalModelError, match="valid JSON"):
        p._extract_json_object("no object")

    assert p._task_kind("fix this", has_attachments=True) == "edit"
    assert p._task_kind("fix this", has_attachments=False) == "generate"
    assert p._task_kind("write tests", has_attachments=False) == "tests"
    assert p._task_kind("security audit", has_attachments=True) == "review"
    assert p._task_kind("explain this", has_attachments=True) == "explain"
    assert p._task_kind("debug root cause", has_attachments=True) == "debug"
    assert p._task_kind("what is here", has_attachments=True) == "analysis"
    assert p._task_kind("create app", has_attachments=False) == "generate"
    assert p._is_simple_single_file_request("write main.py", "generate") is True
    assert p._is_simple_single_file_request("build full project with database", "generate") is False
    assert p._is_simple_single_file_request("review main.py", "review") is False


def test_code_filename_and_validation_helpers() -> None:
    p = _pipeline()
    assert p._test_filename_for("pkg/app.py") == "pkg/test_app.py"
    assert p._test_filename_for("app.ts") == "app.test.ts"
    assert p._test_filename_for("main.go") == "main_test.go"
    assert p._test_filename_for("Thing.java") == "ThingTest.java"
    assert p._guess_single_filename("write src/tool.py", [], task_kind="generate") == "src/tool.py"
    assert p._guess_single_filename("write tests", [{"path": "src/tool.py"}], task_kind="tests") == "src/test_tool.py"
    assert p._guess_single_filename("typescript unit test", [], task_kind="tests") == "main.test.ts"
    assert p._guess_single_filename("make rust script", [], task_kind="generate") == "main.rs"
    assert p._guess_single_filename("do something", [], task_kind="generate") == "main.py"
    assert p._validate_source("x.py", "value = 1\n") is None
    assert "Python syntax error" in p._validate_source("x.py", "if:\n")
    assert p._validate_source("x.json", '{"ok": true}') is None
    assert "JSON syntax error" in p._validate_source("x.json", "{")
    assert p._validate_source("x.xml", "<a/>") is None
    assert "XML syntax error" in p._validate_source("x.xml", "<a>")
    artifact = p._artifact(path="x.py", content="print(1)\n", operation="bad")
    assert artifact["operation"] == "create" and artifact["runnable"] is True


def test_code_prepare_request_carries_memory_attachment_and_explicit_execution_policy() -> None:
    p = _pipeline()
    prepared = p.prepare_request(
        question="fix src/app.py",
        language="en",
        interaction_mode="conversation",
        conversation=[{"role": "user", "content": "old"}],
        attachments=[
            {"name": "src/app.py", "content": "print('old')", "summary": "python file"},
            "bad",
        ],
        memory_snapshot={
            "recent_messages": [{"role": "assistant", "content": "memory turn"}],
            "summary": "keep API stable",
            "relevant_facts": [{"key": "framework", "value": "Flask"}],
            "mode_state": {"project": "CrowAI"},
            "request_options": {"execution": {"allow": True, "backend": "isolated"}},
        },
    )
    meta = prepared["metadata"]
    assert meta["task_kind"] == "edit"
    assert meta["execution_policy"] == {"allow": True, "backend": "isolated"}
    assert meta["memory_summary"] == "keep API stable"
    assert meta["conversation_messages"] == [{"role": "assistant", "content": "memory turn"}]
    assert meta["existing_files"][0]["path"] == "src/app.py"
    assert "FILE START: src/app.py" in meta["attachment_context"]

    fallback = p.prepare_request(
        question=" ", language="tr", interaction_mode="conversation", conversation=[],
        attachments=[{"name": "x.py", "content": "x=1"}], memory_snapshot=None,
    )
    assert fallback["metadata"]["task_kind"] == "review"
    with pytest.raises(ValueError, match="too short"):
        p.prepare_request(question=" ", language="en", interaction_mode="conversation", conversation=[], attachments=[])


def test_code_single_file_generation_repairs_invalid_python(monkeypatch: pytest.MonkeyPatch) -> None:
    p = _pipeline()
    answers = iter(["```python\nif:\n```", "```python\nprint('fixed')\n```"])
    monkeypatch.setattr(p, "generate_response", lambda *args, **kwargs: next(answers))
    artifacts, summary, warnings = p._generate_single_file(
        question="write main.py",
        language="en",
        metadata={"task_kind": "generate", "existing_files": []},
    )
    assert artifacts[0]["filename"] == "main.py"
    assert artifacts[0]["code"] == "print('fixed')"
    assert warnings == []
    assert "main.py" in summary


def test_code_project_plan_and_file_generation_filter_unsafe_and_duplicate_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    p = _pipeline()
    monkeypatch.setattr(
        p,
        "generate_response",
        lambda *args, **kwargs: json.dumps(
            {"files": [
                {"path": "src/app.py", "purpose": "app"},
                {"path": "SRC/APP.PY", "purpose": "dup"},
                {"path": "../escape.py", "purpose": "bad"},
                {"path": "tests/test_app.py", "purpose": "tests"},
            ]}
        ),
    )
    plan = p._generate_plan(question="build project", metadata={})
    assert [item["path"] for item in plan] == ["src/app.py", "tests/test_app.py"]

    monkeypatch.setattr(p, "generate_response", lambda *args, **kwargs: "print('ok')\n")
    artifact, warning = p._generate_project_file(
        question="build project", planned_files=plan, target=plan[0], generated=[], metadata={}, index=1,
    )
    assert artifact["path"] == "src/app.py"
    assert warning is None


def test_code_finalize_analysis_and_simple_file_execution_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    p = _pipeline()
    monkeypatch.setattr(p, "_generate_analysis_answer", lambda **kwargs: "review complete")
    analysis_result = p.finalize_result(
        question="review", language="en", interaction_mode="conversation",
        result={"metadata": {"task_kind": "review"}},
    )
    assert analysis_result["success"] is True
    assert analysis_result["artifacts"] == []
    assert analysis_result["meta"]["local_analysis_used"] is True

    monkeypatch.setattr(
        p,
        "_generate_single_file",
        lambda **kwargs: ([p._artifact(path="main.py", content="print('ok')\n")], "generated", []),
    )
    monkeypatch.setattr(
        p,
        "execute_python_artifacts",
        lambda *args, **kwargs: {
            "executed": True, "requested": True, "passed": True, "backend": "docker",
            "isolation": {"isolated_backend": True, "host_filesystem_isolated": True, "network_isolated": True, "trusted_local": False},
        },
    )
    generated = p.finalize_result(
        question="write main.py", language="en", interaction_mode="conversation",
        result={"metadata": {"task_kind": "generate", "simple_single_file": True, "execution_policy": {"allow": True, "backend": "isolated"}}},
    )
    assert generated["success"] is True
    assert generated["tests"]["executed"] is True
    assert generated["tests"]["passed"] is True
    assert generated["meta"]["python_execution"]["backend"] == "docker"
    assert "executed successfully" in generated["answer"]


def test_code_finalize_reports_runner_unavailable_and_rejection_without_fabricating_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    p = _pipeline()
    monkeypatch.setattr(
        p,
        "_generate_single_file",
        lambda **kwargs: ([p._artifact(path="main.py", content="print('ok')\n")], "generated", []),
    )
    monkeypatch.setattr(
        p,
        "execute_python_artifacts",
        lambda *args, **kwargs: {
            "executed": False, "requested": True, "reason": "isolated_backend_unavailable", "backend": "docker",
            "isolation": {"isolated_backend": False, "host_filesystem_isolated": False, "network_isolated": False, "trusted_local": False},
        },
    )
    unavailable = p.finalize_result(
        question="write main.py", language="en", interaction_mode="conversation",
        result={"metadata": {"task_kind": "generate", "simple_single_file": True, "execution_policy": {"allow": True, "backend": "isolated"}}},
    )
    assert unavailable["tests"]["executed"] is False
    assert unavailable["tests"]["evidence"] == {}
    assert any("no permitted execution backend" in item for item in unavailable["warnings"])

    def reject(*args, **kwargs):
        raise p.RunnerError("runner refused unsafe workspace")

    monkeypatch.setattr(p, "execute_python_artifacts", reject)
    rejected = p.finalize_result(
        question="write main.py", language="en", interaction_mode="conversation",
        result={"metadata": {"task_kind": "generate", "simple_single_file": True, "execution_policy": {"allow": True, "backend": "isolated"}}},
    )
    evidence = rejected["meta"]["python_execution"]
    assert evidence["executed"] is False and evidence["reason"] == "runner_rejected"
    assert evidence["isolation"]["host_filesystem_isolated"] is False


def test_code_finalize_project_generation_and_path_collision_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    p = _pipeline()
    plan = [{"path": "a.py", "purpose": "a"}, {"path": "b.py", "purpose": "b"}]
    monkeypatch.setattr(p, "_generate_plan", lambda **kwargs: plan)
    monkeypatch.setattr(
        p,
        "_generate_project_file",
        lambda **kwargs: (p._artifact(path=kwargs["target"]["path"], content="value = 1\n"), None),
    )
    monkeypatch.setattr(p, "execute_python_artifacts", lambda *args, **kwargs: {"executed": False, "requested": False, "backend": "disabled", "reason": "execution_disabled", "isolation": {}})
    result = p.finalize_result(
        question="build complete project", language="tr", interaction_mode="conversation",
        result={"metadata": {"task_kind": "generate", "simple_single_file": False}},
    )
    assert result["success"] is True and len(result["artifacts"]) == 2
    assert "2 proje dosyası" in result["answer"]

    with pytest.raises(p.LocalModelError, match="duplicate|colliding"):
        p._annotate_operations(
            [p._artifact(path="A.py", content="x=1"), p._artifact(path="a.py", content="x=2")],
            {},
        )
    with pytest.raises(p.LocalModelError, match="unsafe"):
        p._annotate_operations([{"path": "../x.py", "code": ""}], {})
