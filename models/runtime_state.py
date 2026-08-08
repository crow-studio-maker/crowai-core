"""Private mutable runtime state for otherwise immutable model packages.

Model package source, prompts, GGUF files and native runtimes may be mounted
read-only. Logs, caches and debug captures are therefore written beneath a
separate Core-owned state root (normally ``instance/model_state``).
"""
from __future__ import annotations

import os
import stat
import threading
from pathlib import Path
from typing import IO

_PRIVATE_DIR_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600
_STATE_LOCK = threading.RLock()
_STATE_ROOT: Path | None = None


def configure_state_root(path: Path | str | None) -> None:
    """Set the process-local model state root used by subsequently loaded packages."""
    global _STATE_ROOT
    with _STATE_LOCK:
        _STATE_ROOT = Path(path).expanduser().resolve() if path else None


def _default_state_root(package_dir: Path) -> Path:
    package = Path(package_dir).resolve()
    # models/<mode>/v1.0 -> project root is parents[2]
    try:
        project_root = package.parents[2]
    except IndexError:
        project_root = package.parent
    return (project_root / "instance" / "model_state").resolve()


def state_root(package_dir: Path) -> Path:
    with _STATE_LOCK:
        configured = _STATE_ROOT
    return configured if configured is not None else _default_state_root(package_dir)


def _state_anchor_for(target: Path) -> Path | None:
    with _STATE_LOCK:
        configured = _STATE_ROOT
    if configured is not None:
        try:
            target.relative_to(configured)
            return configured
        except ValueError:
            return None
    for candidate in (target, *target.parents):
        if candidate.name == "model_state":
            return candidate
    return None


def _chmod_private_directory(path: Path) -> None:
    if os.name == "posix":
        os.chmod(path, _PRIVATE_DIR_MODE)
        if stat.S_IMODE(path.stat().st_mode) != _PRIVATE_DIR_MODE:
            raise RuntimeError(f"Model runtime state directory is not private: {path}")


def _ensure_private_directory(path: Path) -> Path:
    target = Path(path)
    if target.is_symlink():
        raise RuntimeError(f"Model runtime state directory must not be a symlink: {target}")

    anchor = _state_anchor_for(target)
    if anchor is not None:
        # The conventional default also owns its instance/ parent. Core already
        # hardens configured roots before package loading, while standalone model
        # tests still receive private defaults.
        if anchor.name == "model_state" and anchor.parent.name == "instance":
            anchor.parent.mkdir(parents=True, exist_ok=True, mode=_PRIVATE_DIR_MODE)
            if anchor.parent.is_symlink():
                raise RuntimeError(f"Model runtime state parent must not be a symlink: {anchor.parent}")
            _chmod_private_directory(anchor.parent)
        anchor.mkdir(parents=True, exist_ok=True, mode=_PRIVATE_DIR_MODE)
        if anchor.is_symlink():
            raise RuntimeError(f"Model runtime state root must not be a symlink: {anchor}")
        _chmod_private_directory(anchor)
        current = anchor
        try:
            relative_parts = target.relative_to(anchor).parts
        except ValueError as exc:
            raise RuntimeError("Model runtime state path escaped its configured root.") from exc
        for part in relative_parts:
            current = current / part
            if current.is_symlink():
                raise RuntimeError(f"Model runtime state directory must not be a symlink: {current}")
            current.mkdir(exist_ok=True, mode=_PRIVATE_DIR_MODE)
            if not current.is_dir():
                raise RuntimeError(f"Model runtime state directory is unavailable: {current}")
            _chmod_private_directory(current)
        return target

    target.mkdir(parents=True, exist_ok=True, mode=_PRIVATE_DIR_MODE)
    if not target.is_dir():
        raise RuntimeError(f"Model runtime state directory is unavailable: {target}")
    _chmod_private_directory(target)
    return target

def model_state_dir(
    package_dir: Path,
    mode: str,
    version: str = "v1.0",
    *,
    create: bool = False,
) -> Path:
    """Return a package's mutable state directory without touching source by default.

    Importing a model package must remain safe when the package/app tree is mounted
    read-only. Writers call :func:`private_subdir`, :func:`open_private_log` or pass
    ``create=True`` at the point mutable state is actually needed.
    """
    root = state_root(package_dir)
    target = root / str(mode) / str(version)
    if not create:
        return target
    root = _ensure_private_directory(root)
    mode_dir = _ensure_private_directory(root / str(mode))
    return _ensure_private_directory(mode_dir / str(version))


def private_subdir(parent: Path, name: str) -> Path:
    value = str(name or "").strip()
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise RuntimeError("Invalid model runtime state subdirectory name.")
    return _ensure_private_directory(Path(parent) / value)


def harden_state_file(path: Path) -> Path:
    target = Path(path)
    if target.is_symlink():
        raise RuntimeError(f"Model runtime state file must not be a symlink: {target}")
    _ensure_private_directory(target.parent)
    if target.exists():
        if not target.is_file():
            raise RuntimeError(f"Model runtime state path is not a regular file: {target}")
        if os.name == "posix":
            os.chmod(target, _PRIVATE_FILE_MODE)
            if stat.S_IMODE(target.stat().st_mode) != _PRIVATE_FILE_MODE:
                raise RuntimeError(f"Model runtime state file is not private: {target}")
    return target



def ensure_private_file(path: Path) -> Path:
    """Create a private regular file atomically enough for SQLite/bootstrap use."""
    target = Path(path)
    _ensure_private_directory(target.parent)
    if target.is_symlink():
        raise RuntimeError(f"Model runtime state file must not be a symlink: {target}")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(target, flags, _PRIVATE_FILE_MODE)
    try:
        if hasattr(os, "fchmod") and os.name == "posix":
            os.fchmod(descriptor, _PRIVATE_FILE_MODE)
    finally:
        os.close(descriptor)
    return harden_state_file(target)

def open_private_log(path: Path) -> IO[str]:
    """Open an append-only UTF-8 log without a permissive creation window on POSIX."""
    target = Path(path)
    _ensure_private_directory(target.parent)
    if target.is_symlink():
        raise RuntimeError(f"Model runtime state log must not be a symlink: {target}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(target, flags, _PRIVATE_FILE_MODE)
    try:
        if hasattr(os, "fchmod") and os.name == "posix":
            os.fchmod(descriptor, _PRIVATE_FILE_MODE)
        handle = os.fdopen(descriptor, "a", encoding="utf-8", buffering=1)
        descriptor = -1
        return handle
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def write_private_text(path: Path, content: str) -> None:
    target = Path(path)
    _ensure_private_directory(target.parent)
    if target.is_symlink():
        raise RuntimeError(f"Model runtime state file must not be a symlink: {target}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(target, flags, _PRIVATE_FILE_MODE)
    try:
        if hasattr(os, "fchmod") and os.name == "posix":
            os.fchmod(descriptor, _PRIVATE_FILE_MODE)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(str(content))
    finally:
        if descriptor >= 0:
            os.close(descriptor)
