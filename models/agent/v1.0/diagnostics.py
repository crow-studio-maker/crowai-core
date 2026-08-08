"""Package-local installation diagnostics for Agent V1.0."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent


def _package_path(value: str, *, area: str | None = None) -> Path:
    relative = Path(str(value or "").strip())
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Configured path must remain inside Agent V1.0.")
    candidate = (BASE_DIR / relative).resolve()
    if candidate != BASE_DIR and BASE_DIR not in candidate.parents:
        raise ValueError("Configured path escapes Agent V1.0.")
    if area:
        root = (BASE_DIR / area).resolve()
        if candidate != root and root not in candidate.parents:
            raise ValueError(f"Configured path must remain under {area}/.")
    return candidate


def _dependency_check(module_name: str) -> dict[str, Any]:
    return {"ok": importlib.util.find_spec(module_name) is not None, "module": module_name}


def _run(command: list[str], *, timeout: int) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": exc.__class__.__name__}
    return {
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip()[:4000],
        "stderr": completed.stderr.strip()[:4000],
    }


def run_diagnostics() -> dict[str, Any]:
    """Check only files and dependencies used by this version package."""

    result: dict[str, Any] = {
        "checks": {},
        "environment": {
            "BRAVE_SEARCH_API_KEY": bool(os.environ.get("BRAVE_SEARCH_API_KEY")),
            "SERPER_API_KEY": bool(os.environ.get("SERPER_API_KEY")),
            "BING_SEARCH_API_KEY": bool(os.environ.get("BING_SEARCH_API_KEY")),
        },
    }
    try:
        config = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError("config must be an object")
    except (OSError, json.JSONDecodeError, ValueError):
        result["checks"]["config"] = {"ok": False}
        result["ready"] = False
        return result

    result["checks"]["config"] = {
        "ok": True,
        "context_size": config.get("context_size"),
        "port": config.get("port"),
    }
    fields = {
        "runtime": ("runtime_file", "runtime"),
        "model": ("model_file", "model"),
        "mmproj": ("mmproj_file", "model"),
        "system_prompt": ("system_prompt_file", "prompts"),
        "planner_prompt": ("planner_prompt_file", "prompts"),
        "synthesizer_prompt": ("synthesizer_prompt_file", "prompts"),
        "vision_prompt": ("vision_prompt_file", "prompts"),
        "providers": ("providers_file", None),
        "sites": ("sites_file", None),
    }
    paths: dict[str, Path] = {}
    for label, (field, area) in fields.items():
        try:
            path = _package_path(str(config.get(field) or ""), area=area)
            paths[label] = path
            result["checks"][label] = {"ok": path.is_file()}
        except ValueError:
            result["checks"][label] = {"ok": False}

    for module_name in ("pypdf", "openpyxl", "fitz"):
        result["checks"][f"dependency_{module_name}"] = _dependency_check(module_name)

    runtime = paths.get("runtime")
    if runtime and runtime.is_file():
        result["checks"]["devices"] = _run([str(runtime), "--list-devices"], timeout=25)
        help_result = _run([str(runtime), "--help"], timeout=25)
        combined = str(help_result.get("stdout") or "") + "\n" + str(help_result.get("stderr") or "")
        result["checks"]["runtime_mmproj_support"] = {"ok": "--mmproj" in combined}
        result["checks"]["runtime_jinja_support"] = {"ok": "--jinja" in combined}

    optional = {"dependency_pypdf", "dependency_openpyxl", "dependency_fitz", "devices"}
    result["ready"] = all(
        bool(check.get("ok"))
        for name, check in result["checks"].items()
        if name not in optional
    )
    return result


if __name__ == "__main__":
    print(json.dumps(run_diagnostics(), ensure_ascii=False, indent=2))
