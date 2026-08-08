"""Security controls for untrusted web content and URL fetching."""

from __future__ import annotations

import ipaddress
import re
import socket
import threading
import time
from collections import defaultdict
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit, urlunsplit


_PROMPT_INJECTION_PATTERNS = (
    r"ignore\s+(all|any|the|previous)\s+instructions",
    r"forget\s+(all|the)\s+(previous|prior)\s+instructions",
    r"system\s+prompt",
    r"developer\s+message",
    r"reveal\s+(your|the)\s+(prompt|instructions)",
    r"execute\s+this\s+(command|instruction)",
    r"you\s+are\s+now\s+",
)


class UnsafeUrlError(ValueError):
    """Raised when a URL violates the Agent fetch policy."""


def _is_forbidden_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False

    return any(
        (
            address.is_private,
            address.is_loopback,
            address.is_link_local,
            address.is_multicast,
            address.is_reserved,
            address.is_unspecified,
        )
    )


def normalize_http_url(
    value: str,
    *,
    allow_private_network: bool = False,
) -> str:
    raw = str(value or "").strip()

    if not raw:
        raise UnsafeUrlError("URL is empty.")

    parsed = urlsplit(raw)

    if parsed.scheme.casefold() not in {"http", "https"}:
        raise UnsafeUrlError("Only HTTP and HTTPS URLs are allowed.")

    if not parsed.hostname:
        raise UnsafeUrlError("URL does not contain a hostname.")

    if parsed.username or parsed.password:
        raise UnsafeUrlError("Credential-bearing URLs are not allowed.")

    hostname = parsed.hostname.casefold().rstrip(".")

    if hostname in {"localhost", "localhost.localdomain"}:
        raise UnsafeUrlError("Localhost URLs are not allowed.")

    if _is_forbidden_ip(hostname) and not allow_private_network:
        raise UnsafeUrlError("Private and local IP addresses are blocked.")

    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(
                hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                proto=socket.IPPROTO_TCP,
            )
        }
    except OSError as exc:
        raise UnsafeUrlError(
            f"Hostname could not be resolved: {hostname}"
        ) from exc

    if not allow_private_network and any(
        _is_forbidden_ip(address)
        for address in addresses
    ):
        raise UnsafeUrlError(
            "Hostname resolves to a private or local network address."
        )

    return urlunsplit(
        (
            parsed.scheme.casefold(),
            parsed.netloc,
            parsed.path or "/",
            parsed.query,
            "",
        )
    )


def safe_relative_path(value: Any) -> str | None:
    raw = str(value or "").replace("\\", "/").strip()

    if not raw:
        return None

    path = PurePosixPath(raw)

    if path.is_absolute():
        return None

    if any(part in {"", ".", ".."} for part in path.parts):
        return None

    if any(":" in part for part in path.parts):
        return None

    return path.as_posix()[:240]


def sanitize_untrusted_text(
    value: Any,
    *,
    maximum_chars: int,
) -> tuple[str, list[str]]:
    text = str(value or "").replace("\x00", " ").strip()
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)

    warnings: list[str] = []

    for pattern in _PROMPT_INJECTION_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            warnings.append(
                "Potential prompt-injection language was found in "
                "untrusted web content."
            )
            break

    return text[:maximum_chars], warnings


class DomainRateLimiter:
    """Simple thread-safe per-domain request pacing."""

    def __init__(self, interval_seconds: float) -> None:
        self.interval_seconds = max(0.0, float(interval_seconds))
        self._last_request: dict[str, float] = defaultdict(float)
        self._locks: dict[str, threading.Lock] = defaultdict(
            threading.Lock
        )

    def wait(self, domain: str) -> None:
        key = domain.casefold().strip()

        if not key or self.interval_seconds <= 0:
            return

        lock = self._locks[key]

        with lock:
            now = time.monotonic()
            elapsed = now - self._last_request[key]
            remaining = self.interval_seconds - elapsed

            if remaining > 0:
                time.sleep(remaining)

            self._last_request[key] = time.monotonic()
