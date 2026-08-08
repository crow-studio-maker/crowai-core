from __future__ import annotations

import logging

from flask import Flask, g, jsonify, request
from werkzeug.exceptions import HTTPException

from crowai.errors import CoreError, RateLimitExceeded, error_payload

_LOG = logging.getLogger("crowai.errors")


def register_error_handlers(app: Flask) -> None:
    @app.errorhandler(CoreError)
    def handle_core_error(error: CoreError):
        response = jsonify(error_payload(error, getattr(g, "request_id", "")))
        response.status_code = error.status
        if isinstance(error, RateLimitExceeded):
            response.headers["Retry-After"] = str(error.retry_after)
        return response

    @app.errorhandler(HTTPException)
    def handle_http_error(error: HTTPException):
        if request.path.startswith("/api/"):
            core = CoreError(
                str(error.description or "The request could not be completed."),
                f"HTTP_{error.code or 500}",
                error.code or 500,
            )
            return jsonify(error_payload(core, getattr(g, "request_id", ""))), core.status
        return error

    @app.errorhandler(Exception)
    def handle_unexpected_error(error: Exception):
        _LOG.exception("Unhandled request error [%s]", getattr(g, "request_id", "unknown"))
        if request.path.startswith("/api/"):
            core = CoreError("An unexpected error occurred.", "INTERNAL_ERROR", 500)
            return jsonify(error_payload(core, getattr(g, "request_id", ""))), 500
        return "An unexpected error occurred.", 500
