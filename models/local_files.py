"""Shared package-local model/runtime resolution and lightweight readiness checks.

The registry and every V1.0 engine use this module so platform fallback and file
validation cannot silently drift apart.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

FileState = Literal["ready", "missing", "invalid"]


def package_local_file(root: Path, value: str, *, area: str | None = None) -> Path:
    root = root.resolve()
    raw = str(value or "").strip()
    relative = Path(raw)
    if not raw or relative.is_absolute() or ".." in relative.parts:
        raise ValueError("invalid package-local file reference")
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("package-local file reference escapes package")
    if area:
        area_root = (root / area).resolve()
        if candidate != area_root and area_root not in candidate.parents:
            raise ValueError(f"file must stay inside package {area} directory")
    return candidate


def runtime_candidates(root: Path, value: str) -> tuple[Path, ...]:
    """Return package-local runtime candidates accepted by all V1.0 engines.

    A single package config can name ``llama-server.exe`` while a Linux install
    supplies the extensionless sibling (and vice versa).  No PATH lookup or
    project-root fallback is ever performed.
    """
    requested = package_local_file(root, value, area="runtime")
    candidates = [requested]
    if requested.suffix.casefold() == ".exe":
        candidates.append(requested.with_suffix(""))
    elif not requested.suffix:
        candidates.append(requested.with_suffix(".exe"))

    safe: list[Path] = []
    runtime_root = (root.resolve() / "runtime").resolve()
    for item in candidates:
        resolved = item.resolve()
        if resolved != runtime_root and runtime_root not in resolved.parents:
            continue
        if resolved not in safe:
            safe.append(resolved)
    return tuple(safe)


def local_file_state(path: Path, *, kind: str) -> FileState:
    """Classify a local asset without reading large model files into memory."""
    try:
        if not path.is_file():
            return "missing"
        size = path.stat().st_size
        if size <= 0:
            return "invalid"
        if kind in {"model", "vision_projector"}:
            if size < 4:
                return "invalid"
            with path.open("rb") as handle:
                return "ready" if handle.read(4) == b"GGUF" else "invalid"
        if kind == "runtime":
            if size < 4:
                return "invalid"
            if os.name != "nt" and not os.access(path, os.X_OK):
                return "invalid"
        return "ready"
    except OSError:
        return "invalid"


def plausible_local_file(path: Path, *, kind: str) -> bool:
    """Return whether a package-local asset is usable for static readiness."""
    return local_file_state(path, kind=kind) == "ready"


def resolve_runtime_file(root: Path, value: str) -> Path:
    """Resolve the same usable runtime candidate that registry readiness accepts."""
    candidates = runtime_candidates(root, value)
    for candidate in candidates:
        if local_file_state(candidate, kind="runtime") == "ready":
            return candidate
    # Keep deterministic diagnostics when no usable runtime exists.  Engines will
    # reject the returned candidate during their own shared readiness validation.
    return candidates[0]
