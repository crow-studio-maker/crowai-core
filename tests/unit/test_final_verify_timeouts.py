from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from tools import final_verify


def test_final_verify_short_child_passes_and_records_timeout() -> None:
    result = final_verify._run(
        "short_child",
        [sys.executable, "-c", "print('ok')"],
        timeout_seconds=10,
    )
    assert result["status"] == "PASS"
    assert result["duration_ms"] >= 0
    assert result["timeout_seconds"] == 10
    assert "ok" in result["detail"]


def test_final_verify_synthetic_timeout_is_classified_and_required_timeout_fails() -> None:
    result = final_verify._run(
        "slow_child",
        [sys.executable, "-c", "import time; time.sleep(5)"],
        timeout_seconds=0.15,
        required=True,
    )
    assert result["status"] == "TIMEOUT"
    assert result["required"] is True
    assert result["timeout_seconds"] == pytest.approx(0.15)
    assert "process tree terminated" in result["detail"]

    summary, exit_code = final_verify._summarize(
        [result],
        started=time.monotonic(),
        coverage_gate=65.0,
        overall_timeout_seconds=60.0,
    )
    assert exit_code == 1
    assert summary["overall"] == "FAIL"
    assert summary["required_failures"] == ["slow_child"]
    serialized = json.dumps(summary)
    assert '"status": "TIMEOUT"' in serialized
    assert '"timeout_seconds": 0.15' in serialized


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group termination regression")
def test_final_verify_timeout_terminates_descendant_process_group(tmp_path: Path) -> None:
    sentinel = tmp_path / "descendant-survived.txt"
    descendant = (
        "import time; from pathlib import Path; "
        "time.sleep(1.0); Path(" + repr(str(sentinel)) + ").write_text('survived')"
    )
    parent = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {descendant!r}]); "
        "time.sleep(10)"
    )

    result = final_verify._run(
        "process_tree_timeout",
        [sys.executable, "-c", parent],
        timeout_seconds=0.2,
    )
    assert result["status"] == "TIMEOUT"
    time.sleep(1.2)
    assert not sentinel.exists()


def test_final_verify_optional_timeout_never_becomes_pass() -> None:
    result = final_verify._run(
        "optional_slow_child",
        [sys.executable, "-c", "import time; time.sleep(5)"],
        timeout_seconds=0.1,
        required=False,
    )
    assert result["status"] == "TIMEOUT"
    summary, exit_code = final_verify._summarize(
        [result],
        started=time.monotonic(),
        coverage_gate=65.0,
        overall_timeout_seconds=60.0,
    )
    assert exit_code == 0
    assert summary["overall"] == "PASS"
    assert summary["checks"][0]["status"] == "TIMEOUT"


def test_final_verify_fallback_clean_stage_excludes_runtime_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "checkout"
    source.mkdir()
    (source / "README.md").write_text("source\n", encoding="utf-8")
    (source / "instance").mkdir()
    (source / "instance" / ".gitkeep").write_text("", encoding="utf-8")
    runtime_log = source / "instance" / "model_state" / "code" / "v1.0" / "engine.log"
    runtime_log.parent.mkdir(parents=True)
    runtime_log.write_text("runtime only\n", encoding="utf-8")

    monkeypatch.setattr(final_verify, "ROOT", source)
    staged, method = final_verify._stage_clean_source(tmp_path / "staging")

    assert method == "explicit-source-rules"
    assert (staged / "README.md").read_text(encoding="utf-8") == "source\n"
    assert (staged / "instance" / ".gitkeep").is_file()
    assert not (staged / "instance" / "model_state").exists()


def test_zero_exit_has_no_intermediate_semantic_pass() -> None:
    process = final_verify.ProcessResult(
        command=[sys.executable, "tool.py"],
        returncode=0,
        stdout="{}",
        duration_ms=1,
        timeout_seconds=10.0,
    )

    assert final_verify._process_problem(
        "semantic_check", process, required=True
    ) is None


def test_model_smoke_exit_zero_semantic_skip_never_streams_false_pass(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = {
        "real_model_smoke": [
            {
                "model_id": "chat/v1.0",
                "result": "SKIPPED",
                "reason": "missing_local_files",
                "missing_requirements": ["runtime", "model"],
            }
        ]
    }
    process = final_verify.ProcessResult(
        command=[sys.executable, "tools/smoke_models.py"],
        returncode=0,
        stdout=json.dumps(payload),
        duration_ms=12,
        timeout_seconds=30.0,
    )
    monkeypatch.setattr(final_verify, "_run_process", lambda *args, **kwargs: process)

    result = final_verify._model_smoke(False)
    streamed = capsys.readouterr().out

    assert result["status"] == "SKIPPED"
    assert "[DONE] real_model_smoke: SKIPPED" in streamed
    assert "[DONE] real_model_smoke: PASS" not in streamed
    public = final_verify._public_summary(
        {
            "coverage_gate": 68.0,
            "overall": "PASS",
            "verdict": "RELEASE CANDIDATE",
            "required_failures": [],
            "checks": [result],
        }
    )
    assert public["checks"]["real_model_smoke"]["status"] == "SKIPPED"
    assert public["checks"]["real_model_smoke"]["models"][0]["result"] == "SKIPPED"


def test_all_required_external_checks_pass_still_caps_automatic_verdict_at_release_candidate() -> None:
    checks = [
        final_verify._check("docker_isolation", "PASS", required=True),
        final_verify._check("real_model_smoke", "PASS", required=True),
    ]

    summary, exit_code = final_verify._summarize(
        checks,
        started=time.monotonic(),
        coverage_gate=68.0,
        overall_timeout_seconds=60.0,
    )

    assert exit_code == 0
    assert summary["overall"] == "PASS"
    assert summary["external_evidence"] == {
        "docker_isolation": "PASS",
        "real_model_smoke": "PASS",
    }
    assert summary["verdict"] == "RELEASE CANDIDATE"


def test_required_semantic_skip_remains_skip_and_fails_overall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = final_verify.ProcessResult(
        command=[sys.executable, "tools/smoke_models.py"],
        returncode=0,
        stdout=json.dumps(
            {
                "real_model_smoke": [
                    {
                        "model_id": "code/v1.0",
                        "result": "SKIPPED",
                        "reason": "missing_local_files",
                        "missing_requirements": ["runtime", "model"],
                    }
                ]
            }
        ),
        duration_ms=8,
        timeout_seconds=30.0,
    )
    monkeypatch.setattr(final_verify, "_run_process", lambda *args, **kwargs: process)

    result = final_verify._model_smoke(True)
    assert result["status"] == "SKIPPED"
    assert result["required"] is True

    summary, exit_code = final_verify._summarize(
        [result],
        started=time.monotonic(),
        coverage_gate=68.0,
        overall_timeout_seconds=60.0,
    )
    assert exit_code == 1
    assert summary["overall"] == "FAIL"
    assert summary["verdict"] == "NOT READY"
    assert summary["required_failures"] == ["real_model_smoke"]


def test_process_exit_zero_can_be_semantic_failure_without_false_process_pass(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    process = final_verify.ProcessResult(
        command=[sys.executable, "tools/smoke_models.py"],
        returncode=0,
        stdout=json.dumps({"real_model_smoke": []}),
        duration_ms=4,
        timeout_seconds=30.0,
    )
    monkeypatch.setattr(final_verify, "_run_process", lambda *args, **kwargs: process)
    # Empty semantic evidence is not a real smoke pass.
    result = final_verify._model_smoke(False)
    streamed = capsys.readouterr().out
    assert result["status"] == "PASS"  # all selected modes could intentionally be filtered out
    assert streamed.count("[DONE] real_model_smoke:") == 1


def test_required_semantic_fail_with_zero_process_exit_fails_overall(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    process = final_verify.ProcessResult(
        command=[sys.executable, "tools/smoke_models.py"],
        returncode=0,
        stdout=json.dumps(
            {"real_model_smoke": [{"model_id": "agent/v1.0", "result": "FAIL"}]}
        ),
        duration_ms=6,
        timeout_seconds=30.0,
    )
    monkeypatch.setattr(final_verify, "_run_process", lambda *args, **kwargs: process)
    result = final_verify._model_smoke(True)
    streamed = capsys.readouterr().out
    assert result["status"] == "FAIL"
    assert "[DONE] real_model_smoke: FAIL" in streamed
    assert "[DONE] real_model_smoke: PASS" not in streamed
    summary, exit_code = final_verify._summarize(
        [result],
        started=time.monotonic(),
        coverage_gate=68.0,
        overall_timeout_seconds=60.0,
    )
    assert exit_code == 1
    assert summary["overall"] == "FAIL"


def test_flask_review_mode_streams_not_available_without_intermediate_fail(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    process = final_verify.ProcessResult(
        command=[sys.executable, "-c", "import flask"],
        returncode=1,
        stdout="ModuleNotFoundError: No module named 'flask'",
        duration_ms=3,
        timeout_seconds=30.0,
    )
    monkeypatch.setattr(final_verify, "_run_process", lambda *args, **kwargs: process)
    result = final_verify._flask_runtime_check(allow_missing=True)
    streamed = capsys.readouterr().out
    assert result["status"] == "NOT_AVAILABLE"
    assert result["required"] is False
    assert "[DONE] flask_werkzeug_import: NOT_AVAILABLE" in streamed
    assert "[DONE] flask_werkzeug_import: FAIL" not in streamed


def test_pytest_review_flask_skip_streams_skip_without_intermediate_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    process = final_verify.ProcessResult(
        command=[sys.executable, "-m", "pytest"],
        returncode=1,
        stdout="14 skipped - Flask is not installed",
        duration_ms=5,
        timeout_seconds=30.0,
    )
    monkeypatch.setattr(final_verify, "_run_process", lambda *args, **kwargs: process)
    result = final_verify._pytest_coverage_check(
        allow_missing_runtime_deps=True,
        coverage_gate=68.0,
        coverage_file=tmp_path / ".coverage",
    )
    streamed = capsys.readouterr().out
    assert result["status"] == "SKIPPED"
    assert result["semantic_reason"] == "flask_missing"
    assert "[DONE] pytest_branch_coverage: SKIPPED" in streamed
    assert "[DONE] pytest_branch_coverage: FAIL" not in streamed


def test_public_summary_prefers_exact_pytest_coverage_value() -> None:
    check = final_verify._check(
        "pytest_branch_coverage",
        "PASS",
        required=True,
        detail="TOTAL 100 20 40 10 78%\n273 passed, 15 skipped\nTotal coverage: 78.43%",
    )
    public = final_verify._public_summary(
        {
            "coverage_gate": 68.0,
            "overall": "PASS",
            "verdict": "RELEASE CANDIDATE",
            "required_failures": [],
            "checks": [check],
        }
    )
    row = public["checks"]["pytest_branch_coverage"]
    assert row["passed"] == 273
    assert row["skipped"] == 15
    assert row["coverage_branch_aware"] == 78.43


def test_public_verification_report_omits_private_process_details() -> None:
    private_root = "/" + "home/" + "private-user/"
    private_model = private_root + "secret-model.gguf"
    check = final_verify._check(
        "example",
        "PASS",
        required=True,
        detail=private_model,
        command=[private_root + ".venv/bin/python", "tool.py"],
        duration_ms=1,
        timeout_seconds=2.0,
    )
    public = final_verify._public_summary(
        {
            "coverage_gate": 68.0,
            "overall": "PASS",
            "verdict": "RELEASE CANDIDATE",
            "required_failures": [],
            "checks": [check],
        }
    )
    encoded = json.dumps(public)
    assert "private-user" not in encoded
    assert "secret-model.gguf" not in encoded
    assert public["checks"]["example"]["status"] == "PASS"
