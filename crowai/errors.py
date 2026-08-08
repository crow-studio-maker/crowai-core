from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class CoreError(Exception):
    message: str
    code: str = "CORE_ERROR"
    status: int = 400
    details: dict[str, Any] | None = None

    def __str__(self) -> str:
        return self.message


class ValidationError(CoreError):
    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message, "VALIDATION_ERROR", 400, details)


class AuthenticationRequired(CoreError):
    def __init__(self, message: str = "Sign in to continue."):
        super().__init__(message, "AUTHENTICATION_REQUIRED", 401)


class AuthorizationDenied(CoreError):
    def __init__(self, message: str = "You do not have access to this resource."):
        super().__init__(message, "AUTHORIZATION_DENIED", 403)


class ResourceNotFound(CoreError):
    def __init__(self, message: str = "The requested resource was not found."):
        super().__init__(message, "RESOURCE_NOT_FOUND", 404)


class ConflictError(CoreError):
    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message, "CONFLICT", 409, details)


class ModelUnavailable(CoreError):
    def __init__(self, message: str = "The selected model is not available.", details: dict[str, Any] | None = None):
        super().__init__(message, "MODEL_UNAVAILABLE", 503, details)


class ModelExecutionError(CoreError):
    def __init__(self, message: str = "The selected model could not complete the request."):
        super().__init__(message, "MODEL_EXECUTION_ERROR", 502)


class UploadRejected(CoreError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message, "UPLOAD_REJECTED", status)


class RateLimitExceeded(CoreError):
    def __init__(self, message: str, retry_after: int):
        super().__init__(message, "RATE_LIMIT_EXCEEDED", 429, {"retry_after": retry_after})
        self.retry_after = retry_after


def error_payload(error: CoreError, request_id: str = "") -> dict[str, Any]:
    item: dict[str, Any] = {"code": error.code, "message": error.message}
    payload: dict[str, Any] = {"success": False, "error": item, "message": error.message, "request_id": request_id}
    if error.details:
        item["details"] = error.details
        payload.update(error.details)
    return payload
