"""Best-effort private local filesystem permissions for CrowAI runtime data.

CrowAI stores account/session/conversation metadata and uploads locally.  On
POSIX filesystems those private runtime paths are explicitly restricted instead
of relying on the process umask.  Windows ``chmod`` is only a best-effort
compatibility measure; deployment ACLs remain the authoritative boundary.
"""
from __future__ import annotations

import logging
import os
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Iterator

LOGGER = logging.getLogger("crowai.storage.permissions")
PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


class PrivatePermissionError(RuntimeError):
    """Raised when required POSIX private permissions cannot be enforced."""


def _handle_failure(message: str, *, strict: bool, error: OSError | None = None) -> None:
    if os.name == "posix" and strict:
        raise PrivatePermissionError(message) from error
    LOGGER.warning("%s%s", message, f" ({error})" if error else "")


def _verify_mode(path: Path, expected: int, *, strict: bool, label: str) -> None:
    if os.name != "posix":
        return
    try:
        actual = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        _handle_failure(f"Unable to verify private {label} permissions: {path}", strict=strict, error=exc)
        return
    if actual != expected:
        _handle_failure(
            f"Private {label} permissions are {oct(actual)}, expected {oct(expected)}: {path}",
            strict=strict,
        )


def harden_private_directory(path: Path, *, strict: bool = False, create: bool = True) -> Path:
    """Create/harden one private runtime directory without following symlinks."""
    target = Path(path)
    if target.is_symlink():
        raise PrivatePermissionError(f"Private runtime directory must not be a symlink: {target}")
    if create:
        try:
            target.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIRECTORY_MODE)
        except OSError as exc:
            raise PrivatePermissionError(f"Unable to create private runtime directory: {target}") from exc
    if not target.is_dir():
        raise PrivatePermissionError(f"Private runtime directory is unavailable: {target}")
    try:
        os.chmod(target, PRIVATE_DIRECTORY_MODE)
    except OSError as exc:
        _handle_failure(f"Unable to harden private runtime directory: {target}", strict=strict, error=exc)
    else:
        _verify_mode(target, PRIVATE_DIRECTORY_MODE, strict=strict, label="directory")
    return target


def harden_private_file(path: Path, *, strict: bool = False, create: bool = False) -> Path:
    """Create/harden one private regular file as 0600 on POSIX without following symlinks."""
    target = Path(path)
    if target.is_symlink():
        raise PrivatePermissionError(f"Private runtime file must not be a symlink: {target}")
    if create and not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            descriptor = os.open(target, flags, PRIVATE_FILE_MODE)
        except FileExistsError:
            pass
        except OSError as exc:
            raise PrivatePermissionError(f"Unable to create private runtime file: {target}") from exc
        else:
            os.close(descriptor)
    if not target.exists():
        return target
    if not target.is_file():
        raise PrivatePermissionError(f"Private runtime file is not a regular file: {target}")
    try:
        os.chmod(target, PRIVATE_FILE_MODE)
    except OSError as exc:
        _handle_failure(f"Unable to harden private runtime file: {target}", strict=strict, error=exc)
    else:
        _verify_mode(target, PRIVATE_FILE_MODE, strict=strict, label="file")
    return target



def harden_private_directory_chain(
    root: Path,
    target: Path,
    *,
    strict: bool = False,
) -> Path:
    """Create/harden every directory from ``root`` through ``target`` as 0700.

    Both paths are treated as private runtime locations.  The target must remain
    below the root after symlink resolution, and any symlink component in the
    configured path is rejected rather than followed.
    """
    root_path = Path(root).expanduser().absolute()
    target_path = Path(target).expanduser().absolute()
    if root_path.is_symlink():
        raise PrivatePermissionError(f"Private runtime root must not be a symlink: {root_path}")
    try:
        relative = target_path.relative_to(root_path)
    except ValueError as exc:
        raise PrivatePermissionError(
            f"Private runtime directory escapes its configured root: {target_path}"
        ) from exc

    try:
        resolved_root = root_path.resolve()
        resolved_target = target_path.resolve()
    except OSError as exc:
        raise PrivatePermissionError("Unable to resolve private runtime directory chain.") from exc
    if resolved_target != resolved_root and resolved_root not in resolved_target.parents:
        raise PrivatePermissionError(
            f"Private runtime directory escapes its configured root: {target_path}"
        )

    current = harden_private_directory(root_path, strict=strict, create=True)
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise PrivatePermissionError(f"Private runtime directory must not be a symlink: {current}")
        harden_private_directory(current, strict=strict, create=True)
    return target_path

def harden_private_tree(root: Path, *, strict: bool = False) -> Path:
    """Harden an explicitly configured CrowAI runtime tree only.

    Symlinks are never followed.  This helper must only be called for configured
    private roots such as ``instance/``, ``users/`` and ``uploads/``.
    """
    target = harden_private_directory(root, strict=strict, create=True)
    try:
        iterator = os.walk(target, topdown=True, followlinks=False)
        for directory, names, files in iterator:
            base = Path(directory)
            retained: list[str] = []
            for name in names:
                child = base / name
                if child.is_symlink():
                    _handle_failure(f"Skipping symlink inside private runtime tree: {child}", strict=strict)
                    continue
                harden_private_directory(child, strict=strict, create=False)
                retained.append(name)
            names[:] = retained
            for name in files:
                child = base / name
                if child.name == ".gitkeep":
                    continue
                if child.is_symlink():
                    _handle_failure(f"Skipping symlink inside private runtime tree: {child}", strict=strict)
                    continue
                harden_private_file(child, strict=strict)
    except OSError as exc:
        _handle_failure(f"Unable to traverse private runtime tree: {target}", strict=strict, error=exc)
    return target


def harden_sqlite_sidecars(database_path: Path, *, strict: bool = False) -> None:
    """Harden the SQLite database plus any currently present WAL/SHM sidecars."""
    database = Path(database_path)
    for candidate in (database, Path(str(database) + "-wal"), Path(str(database) + "-shm")):
        if candidate.exists():
            harden_private_file(candidate, strict=strict)


@contextmanager
def open_private_binary_exclusive(path: Path, *, strict: bool = False) -> Iterator[IO[bytes]]:
    """Open a newly created private binary file without a permissive-mode window."""
    target = Path(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(target, flags, PRIVATE_FILE_MODE)
    except OSError as exc:
        raise PrivatePermissionError(f"Unable to create private runtime file: {target}") from exc
    try:
        if hasattr(os, "fchmod"):
            try:
                os.fchmod(descriptor, PRIVATE_FILE_MODE)
            except OSError as exc:
                _handle_failure(f"Unable to harden open private runtime file: {target}", strict=strict, error=exc)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            yield handle
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    harden_private_file(target, strict=strict)


def atomic_write_private_text(path: Path, text: str, *, strict: bool = False) -> None:
    """Atomically replace a private UTF-8 text file while preserving 0600 mode."""
    target = Path(path)
    harden_private_directory(target.parent, strict=strict, create=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with open_private_binary_exclusive(temporary, strict=strict) as handle:
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        harden_private_file(target, strict=strict)
    finally:
        temporary.unlink(missing_ok=True)
