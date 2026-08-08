"""Shared source/release enumeration policy.

Release inputs are regular files only. Symlinks are rejected before any read so
an in-repository link can never make CrowAI package bytes from outside the
checkout. ZIP permissions are also policy-driven rather than inherited from the
host checkout, keeping builds stable across Windows/macOS/Linux metadata.
"""
from __future__ import annotations

import stat
from pathlib import Path

GENERATED_PARTS = {
    ".git", ".venv", "venv", "dist", "build", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "htmlcov",
}
GENERATED_FILES = {".coverage", "coverage.xml", "coverage.json"}
EXECUTABLE_PATHS = {"run_linux.sh"}
RUNTIME_ROOTS = {"instance", "users", "uploads"}


def is_generated(relative: Path) -> bool:
    return (
        any(part in GENERATED_PARTS for part in relative.parts)
        or relative.name in GENERATED_FILES
        or relative.suffix.casefold() in {".pyc", ".pyo"}
    )


def is_runtime_state(relative: Path) -> bool:
    """Return whether *relative* is mutable runtime/user state, not source.

    The three runtime roots deliberately keep their ``.gitkeep`` placeholders
    in source releases, while every other file below them is treated as
    generated/private state.  Keeping this rule next to the other source
    enumeration rules prevents a test run from making a later source bundle
    accidentally absorb logs, SQLite files, uploads, or user data.
    """
    return bool(
        relative.parts
        and relative.parts[0] in RUNTIME_ROOTS
        and relative.name != ".gitkeep"
    )


def is_source_release_file(relative: Path) -> bool:
    """Return whether a regular file belongs to a clean source artifact."""
    return not is_generated(relative) and not is_runtime_state(relative)


def source_symlinks(root: Path) -> list[Path]:
    """Return non-generated source-tree symlink entries without following them."""
    output: list[Path] = []
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if is_generated(relative):
            continue
        if path.is_symlink():
            output.append(path)
    return sorted(output, key=lambda item: item.relative_to(root).as_posix())


def reject_source_symlinks(root: Path) -> None:
    links = source_symlinks(root)
    if links:
        relative = links[0].relative_to(root).as_posix()
        raise RuntimeError(f"Source tree symlink entries are not allowed in release inputs: {relative}")


def deterministic_file_mode(relative: Path | str) -> int:
    name = relative.as_posix() if isinstance(relative, Path) else str(relative).replace("\\", "/")
    return stat.S_IFREG | (0o755 if name in EXECUTABLE_PATHS else 0o644)
