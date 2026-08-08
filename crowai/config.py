from __future__ import annotations

import os
import secrets
import stat
from datetime import timedelta
from pathlib import Path
from typing import Any, Mapping

from dotenv import load_dotenv
from flask import Flask

from crowai.storage.permissions import (
    atomic_write_private_text,
    harden_private_file,
    harden_private_tree,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}

# Keys managed by CrowAI itself. They may be supplied as documented inputs where
# explicitly read below, but they are never blindly copied over the normalized
# configuration. In particular, derived production/security values are written
# last so a programmatic app-factory override cannot weaken them.
_CORE_MANAGED_KEYS = {
    "ENVIRONMENT",
    "CROWAI_ENV",
    "PRODUCTION",
    "DEBUG",
    "TESTING",
    "SECRET_KEY",
    "CROWAI_SECRET_KEY",
    "PRIVATE_PERMISSIONS_STRICT",
    "INSTANCE_DIR",
    "CROWAI_INSTANCE_DIR",
    "DATABASE_PATH",
    "UPLOAD_DIR",
    "CROWAI_UPLOAD_DIR",
    "MODELS_DIR",
    "CROWAI_MODELS_DIR",
    "MODEL_STATE_DIR",
    "CROWAI_MODEL_STATE_DIR",
    "USERS_DIR",
    "CROWAI_USERS_DIR",
    "MAX_MESSAGE_LENGTH",
    "CROWAI_MAX_MESSAGE_LENGTH",
    "MAX_UPLOAD_BYTES",
    "CROWAI_MAX_UPLOAD_BYTES",
    "MAX_UPLOAD_FILES",
    "CROWAI_MAX_UPLOAD_FILES",
    "MAX_REQUEST_BYTES",
    "CROWAI_MAX_REQUEST_BYTES",
    "MAX_CONTENT_LENGTH",
    "SESSION_DAYS",
    "CROWAI_SESSION_DAYS",
    "LOG_LEVEL",
    "CROWAI_LOG_LEVEL",
    "ENABLE_WEB_SEARCH",
    "CROWAI_ENABLE_WEB_SEARCH",
    "STRICT_MODEL_CAPABILITIES",
    "CROWAI_STRICT_MODEL_CAPABILITIES",
    "MODEL_DEVELOPMENT_RELOAD",
    "CROWAI_MODEL_DEVELOPMENT_RELOAD",
    "SESSION_COOKIE_HTTPONLY",
    "SESSION_COOKIE_SECURE",
    "SESSION_COOKIE_SAMESITE",
    "SESSION_COOKIE_NAME",
    "SESSION_COOKIE_PATH",
    "PERMANENT_SESSION_LIFETIME",
    "SESSION_REFRESH_EACH_REQUEST",
    "JSON_SORT_KEYS",
}


def _override_value(
    overrides: Mapping[str, Any],
    key: str,
    env_key: str | None = None,
    *,
    default: Any = None,
) -> Any:
    """Return one normalized input source with programmatic overrides first.

    Both the Flask-style key and its CROWAI_* alias are accepted in an override
    mapping. Process environment is the next source. Security-derived Flask
    keys such as MAX_CONTENT_LENGTH are intentionally not read here.
    """
    if key in overrides and overrides[key] is not None:
        return overrides[key]
    if env_key and env_key in overrides and overrides[env_key] is not None:
        return overrides[env_key]
    if env_key:
        raw = os.getenv(env_key)
        if raw is not None:
            return raw
    return default


def _environment_name(overrides: Mapping[str, Any] | None = None) -> str:
    values = overrides or {}
    raw = _override_value(values, "ENVIRONMENT", "CROWAI_ENV", default="development")
    return str(raw or "development").strip().casefold()


def load_project_environment(overrides: Mapping[str, Any] | None = None) -> None:
    """Load project ``.env`` only when the requested app mode is non-production.

    The app factory may choose production through a programmatic override before
    process environment has CROWAI_ENV. That decision must be honored *before*
    dotenv is read, otherwise development-only values could leak into a
    production factory invocation.
    """
    if _environment_name(overrides) in {"production", "prod"}:
        return
    load_dotenv(dotenv_path=PROJECT_ROOT / ".env", override=False)


def _coerce_bool(name: str, raw: Any, *, default: bool = False) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int) and raw in {0, 1}:
        return bool(raw)
    value = str(raw).strip().casefold()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise RuntimeError(f"{name} must be a boolean value.")


def _bool_setting(
    overrides: Mapping[str, Any],
    key: str,
    env_key: str,
    *,
    default: bool,
) -> bool:
    return _coerce_bool(env_key, _override_value(overrides, key, env_key, default=default), default=default)


def _bounded_int(name: str, raw: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        value = default if raw is None else int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name} must be an integer.") from exc
    if value < minimum or value > maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}.")
    return value


def _int_setting(
    overrides: Mapping[str, Any],
    key: str,
    env_key: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = _override_value(overrides, key, env_key, default=default)
    return _bounded_int(env_key, raw, default=default, minimum=minimum, maximum=maximum)


PRIVATE_ROOT_MARKER = ".crowai-private-root"
PRIVATE_ROOT_MARKER_CONTENT = "CrowAI private runtime root v1\n"


def _lexical_absolute_path(value: Any, fallback: Path) -> tuple[Path, bool]:
    """Return an absolute lexical path without resolving filesystem links.

    The boolean indicates whether the path came from user/environment input.
    Using ``abspath`` instead of ``Path.resolve`` here is deliberate: security
    checks need to see symlink/junction components before canonicalization.
    """
    raw = str(value or "").strip()
    candidate = Path(raw).expanduser() if raw else Path(fallback).expanduser()
    explicit = bool(raw)
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    return Path(os.path.abspath(os.fspath(candidate))), explicit


def _link_like(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction) and is_junction():
        return True
    # Python 3.11 lacks Path.is_junction(). On Windows, reject any reparse-point
    # component so a junction cannot redirect recursive hardening either.
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if reparse_flag:
        try:
            attributes = getattr(os.lstat(path), "st_file_attributes", 0)
        except FileNotFoundError:
            return False
        if attributes & reparse_flag:
            return True
    return False


def _reject_link_components(path: Path, *, label: str) -> None:
    """Reject an existing symlink/junction anywhere in a private lexical path."""
    candidate = Path(path)
    parts = candidate.parts
    if not parts:
        return
    current = Path(parts[0])
    start = 1
    # Relative paths are made absolute before this helper, but keep the helper
    # well-defined for drive-relative/pathlib edge cases.
    if not current.anchor:
        current = Path.cwd() / current
    for part in parts[start:]:
        current = current / part
        try:
            if _link_like(current):
                raise RuntimeError(f"{label} must not contain symlink or junction components: {current}")
        except OSError as exc:
            raise RuntimeError(f"Unable to validate {label} path component: {current}") from exc


def _path_from(
    value: Any,
    fallback: Path,
    *,
    reject_links: bool = False,
    label: str = "CrowAI path",
) -> Path:
    lexical, explicit = _lexical_absolute_path(value, fallback)
    if reject_links and explicit:
        _reject_link_components(lexical, label=label)
    return lexical.resolve()


def _path_setting(
    overrides: Mapping[str, Any],
    key: str,
    env_key: str,
    fallback: Path,
    *,
    reject_links: bool = False,
) -> Path:
    return _path_from(
        _override_value(overrides, key, env_key),
        fallback,
        reject_links=reject_links,
        label=env_key,
    )


def _setting_is_explicit(overrides: Mapping[str, Any], key: str, env_key: str) -> bool:
    for candidate in (overrides.get(key), overrides.get(env_key), os.getenv(env_key)):
        if candidate is not None and str(candidate).strip():
            return True
    return False


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _validate_private_runtime_roots(*, instance_dir: Path, upload_dir: Path, users_dir: Path, models_dir: Path) -> None:
    """Reject dangerous chmod targets while still allowing dedicated custom roots.

    CrowAI recursively hardens its configured private roots. Pointing one of
    those settings at a home directory, filesystem root, project checkout, or
    model tree could unexpectedly change unrelated permissions, so such broad
    roots fail closed before any recursive chmod occurs.
    """
    project = PROJECT_ROOT.resolve()
    home = Path.home().expanduser().resolve()
    model_root = models_dir.resolve()
    private = {
        "instance": instance_dir.resolve(),
        "uploads": upload_dir.resolve(),
        "users": users_dir.resolve(),
    }
    for label, path in private.items():
        anchor = Path(path.anchor).resolve() if path.anchor else None
        if anchor is not None and path == anchor:
            raise RuntimeError(f"CROWAI_{label.upper()}_DIR must be a dedicated directory, not a filesystem root.")
        if path == home:
            raise RuntimeError(f"CROWAI_{label.upper()}_DIR must not point at the user home directory.")
        if path == project or _is_relative_to(project, path):
            raise RuntimeError(f"CROWAI_{label.upper()}_DIR must not contain the CrowAI project checkout.")
        if path == model_root or _is_relative_to(model_root, path):
            raise RuntimeError(f"CROWAI_{label.upper()}_DIR must not contain the model package root.")

    values = list(private.items())
    for index, (left_name, left) in enumerate(values):
        for right_name, right in values[index + 1:]:
            if left == right or _is_relative_to(left, right) or _is_relative_to(right, left):
                raise RuntimeError(
                    f"CrowAI private runtime roots must be dedicated and non-overlapping: {left_name}, {right_name}."
                )




def _claim_private_runtime_root(
    path: Path,
    *,
    label: str,
    production: bool,
    custom: bool,
) -> None:
    """Harden a runtime root and safely claim custom production directories.

    Recursive chmod is only safe when CrowAI owns the tree. A custom production
    root may therefore be new/empty (``.gitkeep`` is treated as an empty-source
    placeholder) or carry CrowAI's ownership marker from an earlier start.
    """
    target = Path(path)
    marker = target / PRIVATE_ROOT_MARKER
    if production and custom and target.exists():
        marker_valid = False
        if marker.exists() or marker.is_symlink():
            if _link_like(marker) or not marker.is_file():
                raise RuntimeError(f"Invalid CrowAI private-root ownership marker: {marker}")
            try:
                marker_valid = marker.read_text(encoding="utf-8") == PRIVATE_ROOT_MARKER_CONTENT
            except OSError as exc:
                raise RuntimeError(f"Unable to read CrowAI private-root ownership marker: {marker}") from exc
            if not marker_valid:
                raise RuntimeError(f"Invalid CrowAI private-root ownership marker: {marker}")
        if not marker_valid:
            try:
                entries = [item for item in target.iterdir() if item.name != ".gitkeep"]
            except OSError as exc:
                raise RuntimeError(f"Unable to inspect custom private runtime root {label}: {target}") from exc
            if entries:
                raise RuntimeError(
                    f"Custom production runtime root {label} must be new/empty or contain {PRIVATE_ROOT_MARKER}: {target}"
                )

    harden_private_tree(target, strict=production)
    if production and custom and not marker.exists():
        atomic_write_private_text(marker, PRIVATE_ROOT_MARKER_CONTENT, strict=True)

def _ensure_private_secret(instance_dir: Path, production: bool, supplied: str) -> str:
    secret_path = instance_dir / "secret.key"
    if production:
        if len(supplied) < 32:
            raise RuntimeError("CROWAI_SECRET_KEY must be at least 32 characters in production.")
        return supplied
    if supplied:
        return supplied
    if secret_path.is_file():
        harden_private_file(secret_path, strict=False)
        value = secret_path.read_text(encoding="utf-8").strip()
        if len(value) >= 32:
            return value
    value = secrets.token_hex(32)
    atomic_write_private_text(secret_path, value + "\n", strict=False)
    return value


def initial_instance_path(overrides: Mapping[str, Any] | None = None) -> Path:
    values = overrides or {}
    return _path_setting(
        values,
        "INSTANCE_DIR",
        "CROWAI_INSTANCE_DIR",
        PROJECT_ROOT / "instance",
        reject_links=True,
    )


def load_configuration(app: Flask, overrides: Mapping[str, Any] | None = None) -> None:
    overrides = dict(overrides or {})
    environment = _environment_name(overrides)
    production = environment in {"production", "prod"}

    instance_dir = initial_instance_path(overrides)
    upload_dir = _path_setting(
        overrides, "UPLOAD_DIR", "CROWAI_UPLOAD_DIR", PROJECT_ROOT / "uploads", reject_links=True
    )
    models_dir = _path_setting(overrides, "MODELS_DIR", "CROWAI_MODELS_DIR", PROJECT_ROOT / "models")
    users_dir = _path_setting(
        overrides, "USERS_DIR", "CROWAI_USERS_DIR", PROJECT_ROOT / "users", reject_links=True
    )
    model_state_dir = _path_setting(
        overrides,
        "MODEL_STATE_DIR",
        "CROWAI_MODEL_STATE_DIR",
        instance_dir / "model_state",
        reject_links=True,
    )
    database_path = _path_from(
        overrides.get("DATABASE_PATH"),
        instance_dir / "workspace.db",
        reject_links=True,
        label="DATABASE_PATH",
    )

    _validate_private_runtime_roots(
        instance_dir=instance_dir,
        upload_dir=upload_dir,
        users_dir=users_dir,
        models_dir=models_dir,
    )
    if model_state_dir != instance_dir and not _is_relative_to(model_state_dir, instance_dir):
        raise RuntimeError("CROWAI_MODEL_STATE_DIR must stay inside CROWAI_INSTANCE_DIR.")

    model_development_reload = False if production else _bool_setting(
        overrides,
        "MODEL_DEVELOPMENT_RELOAD",
        "CROWAI_MODEL_DEVELOPMENT_RELOAD",
        default=True,
    )

    # Private application state never relies only on the process umask. Existing
    # runtime trees are tightened at startup as well. Model/source directories
    # are intentionally excluded from recursive permission changes. Custom
    # production roots must be explicitly CrowAI-owned before recursive chmod.
    private_roots = (
        (instance_dir, "CROWAI_INSTANCE_DIR", _setting_is_explicit(overrides, "INSTANCE_DIR", "CROWAI_INSTANCE_DIR")),
        (upload_dir, "CROWAI_UPLOAD_DIR", _setting_is_explicit(overrides, "UPLOAD_DIR", "CROWAI_UPLOAD_DIR")),
        (users_dir, "CROWAI_USERS_DIR", _setting_is_explicit(overrides, "USERS_DIR", "CROWAI_USERS_DIR")),
    )
    for path, label, custom in private_roots:
        _claim_private_runtime_root(path, label=label, production=production, custom=custom)
        if not os.access(path, os.W_OK):
            raise RuntimeError(f"CrowAI runtime path is not writable: {path}")
    harden_private_tree(model_state_dir, strict=production)
    if not os.access(model_state_dir, os.W_OK):
        raise RuntimeError(f"CrowAI model runtime state path is not writable: {model_state_dir}")

    # Model packages are immutable application inputs. Production supports an
    # absent or read-only model root and never attempts to create or chmod it.
    if models_dir.exists():
        if not models_dir.is_dir():
            raise RuntimeError(f"CrowAI model path is not a directory: {models_dir}")
        required_mode = os.R_OK | (os.X_OK if os.name == "posix" else 0)
        if not os.access(models_dir, required_mode):
            raise RuntimeError(f"CrowAI model path is not readable/traversable: {models_dir}")
    elif not production:
        models_dir.mkdir(parents=True, exist_ok=True)

    secret = str(_override_value(overrides, "SECRET_KEY", "CROWAI_SECRET_KEY", default="") or "").strip()
    session_days = _int_setting(
        overrides, "SESSION_DAYS", "CROWAI_SESSION_DAYS", default=7, minimum=1, maximum=365
    )
    max_message_length = _int_setting(
        overrides,
        "MAX_MESSAGE_LENGTH",
        "CROWAI_MAX_MESSAGE_LENGTH",
        default=12_000,
        minimum=2,
        maximum=1_000_000,
    )
    max_upload_bytes = _int_setting(
        overrides,
        "MAX_UPLOAD_BYTES",
        "CROWAI_MAX_UPLOAD_BYTES",
        default=32 * 1024 * 1024,
        minimum=1024,
        maximum=2 * 1024 * 1024 * 1024,
    )
    max_upload_files = _int_setting(
        overrides,
        "MAX_UPLOAD_FILES",
        "CROWAI_MAX_UPLOAD_FILES",
        default=10,
        minimum=1,
        maximum=100,
    )
    max_request_bytes = _int_setting(
        overrides,
        "MAX_REQUEST_BYTES",
        "CROWAI_MAX_REQUEST_BYTES",
        default=128 * 1024 * 1024,
        minimum=1024,
        maximum=2 * 1024 * 1024 * 1024,
    )

    # Preserve explicitly supplied extension-specific Flask keys while never
    # allowing them to overwrite CrowAI-managed/security values. This keeps the
    # app factory extensible without turning ``config`` into a security bypass.
    passthrough = {key: value for key, value in overrides.items() if key not in _CORE_MANAGED_KEYS}
    if passthrough:
        app.config.update(passthrough)

    app.config.update(
        ENVIRONMENT=environment,
        PRODUCTION=production,
        DEBUG=False if production else _coerce_bool("DEBUG", overrides.get("DEBUG"), default=False),
        TESTING=False if production else _coerce_bool("TESTING", overrides.get("TESTING"), default=False),
        SECRET_KEY=_ensure_private_secret(instance_dir, production, secret),
        PRIVATE_PERMISSIONS_STRICT=production,
        INSTANCE_DIR=instance_dir,
        DATABASE_PATH=database_path,
        UPLOAD_DIR=upload_dir,
        MODELS_DIR=models_dir,
        MODEL_STATE_DIR=model_state_dir,
        USERS_DIR=users_dir,
        MAX_MESSAGE_LENGTH=max_message_length,
        MAX_UPLOAD_BYTES=max_upload_bytes,
        MAX_UPLOAD_FILES=max_upload_files,
        SESSION_DAYS=session_days,
        LOG_LEVEL=str(_override_value(overrides, "LOG_LEVEL", "CROWAI_LOG_LEVEL", default="INFO") or "INFO").upper(),
        ENABLE_WEB_SEARCH=_bool_setting(
            overrides, "ENABLE_WEB_SEARCH", "CROWAI_ENABLE_WEB_SEARCH", default=True
        ),
        STRICT_MODEL_CAPABILITIES=_bool_setting(
            overrides,
            "STRICT_MODEL_CAPABILITIES",
            "CROWAI_STRICT_MODEL_CAPABILITIES",
            default=False,
        ),
        MODEL_DEVELOPMENT_RELOAD=model_development_reload,
        # Security-derived values are deliberately assigned last and are not
        # programmatic override inputs.
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=production,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_NAME="crowai_session",
        SESSION_COOKIE_PATH="/",
        PERMANENT_SESSION_LIFETIME=timedelta(days=session_days),
        SESSION_REFRESH_EACH_REQUEST=True,
        MAX_REQUEST_BYTES=max_request_bytes,
        MAX_CONTENT_LENGTH=max_request_bytes,
        JSON_SORT_KEYS=False,
    )
    validate_configuration(app.config)


def validate_configuration(config: Mapping[str, Any]) -> None:
    if config.get("MAX_CONTENT_LENGTH") != config.get("MAX_REQUEST_BYTES"):
        raise RuntimeError("MAX_CONTENT_LENGTH must be derived from MAX_REQUEST_BYTES.")
    if config.get("PRODUCTION"):
        if config.get("DEBUG") or config.get("TESTING"):
            raise RuntimeError("DEBUG and TESTING must be disabled in production.")
        if not config.get("PRIVATE_PERMISSIONS_STRICT"):
            raise RuntimeError("Strict private permissions are required in production.")
        if not config.get("SESSION_COOKIE_HTTPONLY"):
            raise RuntimeError("HttpOnly session cookies are required in production.")
        if not config.get("SESSION_COOKIE_SECURE"):
            raise RuntimeError("Secure session cookies are required in production.")
        if config.get("SESSION_COOKIE_SAMESITE") not in {"Lax", "Strict"}:
            raise RuntimeError("A restrictive SameSite cookie policy is required in production.")
        if config.get("MODEL_DEVELOPMENT_RELOAD"):
            raise RuntimeError("Model development reload must be disabled in production.")
        if len(str(config.get("SECRET_KEY") or "")) < 32:
            raise RuntimeError("A strong stable secret is required in production.")
