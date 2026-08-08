from __future__ import annotations

import hmac
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from functools import wraps
from typing import Any, Callable
from urllib.parse import urlsplit

from flask import abort, g, request, session
from werkzeug.security import check_password_hash, generate_password_hash

_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class SlidingWindowLimiter:
    """Bounded in-process limiter for local/single-process deployments."""

    def __init__(self, *, maximum_keys: int = 20_000) -> None:
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self.maximum_keys = maximum_keys

    def allow(self, key: str, *, limit: int, window_seconds: int) -> tuple[bool, int]:
        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            if len(self._events) > self.maximum_keys:
                stale = [name for name, values in self._events.items() if not values or values[-1] <= cutoff]
                for name in stale[: max(1, len(stale))]:
                    self._events.pop(name, None)
                if len(self._events) > self.maximum_keys:
                    for name in list(self._events)[: len(self._events) - self.maximum_keys]:
                        self._events.pop(name, None)
            events = self._events[key]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= limit:
                retry_after = max(1, int(window_seconds - (now - events[0])))
                return False, retry_after
            events.append(now)
        return True, 0


def password_hash(value: str) -> str:
    if len(value) < 10:
        raise ValueError("Password must be at least 10 characters.")
    if len(value) > 256:
        raise ValueError("Password is too long.")
    return generate_password_hash(value, method="scrypt")


def valid_login(row: dict[str, Any] | None, password: str) -> bool:
    if len(password) > 256:
        return False
    return bool(row and check_password_hash(str(row["password_hash"]), password))


def validate_email(value: str) -> str:
    email = value.strip().casefold()
    if not _EMAIL.fullmatch(email) or len(email) > 254:
        raise ValueError("Enter a valid email address.")
    return email


def require_user(function: Callable):
    @wraps(function)
    def wrapped(*args, **kwargs):
        if not getattr(g, "user", None):
            abort(401)
        return function(*args, **kwargs)

    return wrapped


def same_origin() -> None:
    origin = request.headers.get("Origin")
    if not origin:
        return
    parsed = urlsplit(origin)
    if parsed.scheme not in {"http", "https"} or parsed.netloc.casefold() != request.host.casefold():
        abort(403, description="The request origin is not allowed.")


def csrf_token() -> str:
    token = session.get("csrf_token")
    if not isinstance(token, str) or len(token) < 32:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def require_csrf() -> None:
    same_origin()
    expected = csrf_token()
    supplied = request.headers.get("X-CSRF-Token", "")
    if not supplied or not hmac.compare_digest(expected, supplied):
        abort(403, description="The security token is missing or invalid.")


def rotate_session(*, user_id: int | None = None) -> None:
    current = session._get_current_object()
    current.clear()
    rotate = getattr(current, "rotate_id", None)
    if callable(rotate):
        rotate()
    if user_id is not None:
        current["user_id"] = int(user_id)
    current["csrf_token"] = secrets.token_urlsafe(32)
