"""Uniform error responses: {"error": {"code", "message", "request_id"}}."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

# Machine-readable error codes (mirrored in api-contracts/errors/errors.yaml by scripts/export_contracts.py).
ERROR_CODES: dict[str, str] = {
    "validation_error": "The request body or parameters are invalid.",
    "unauthenticated": "Missing, invalid or expired token.",
    "forbidden": "The caller is not allowed to do this.",
    "not_found": "The resource does not exist or is not visible to the caller.",
    "conflict": "The request conflicts with the current state (for example a stale version).",
    "rate_limited": "Too many attempts. Try again later.",
    "guardian_verification_required": "A verified guardian is required before child data can be created.",
    "elevation_required": "Enter the parent PIN to continue.",
    "pin_not_set": "No parent PIN has been set.",
    "pin_locked": "The PIN is temporarily locked after too many attempts.",
    "pin_invalid": "The PIN is incorrect.",
    "otp_invalid": "The verification code is incorrect or expired.",
    "password_change_required": "You signed in with a temporary password. Set a new password to continue.",
    "email_taken": "An account with this email already exists.",
    "invalid_credentials": "Email or password is incorrect.",
    "content_invalid": "The content failed validation.",
    "delivery_failed": "The verification code could not be delivered.",
    "method_not_allowed": "This method is not supported on this path.",
    "not_implemented": "This endpoint is planned but not built yet.",
    "internal_error": "Unexpected server error.",
}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str | None = None, extra: dict | None = None):
        self.status = status
        self.code = code
        self.message = message or ERROR_CODES.get(code, code)
        self.extra = extra or {}


def unauthenticated(message: str | None = None) -> ApiError:
    return ApiError(401, "unauthenticated", message)


def forbidden(message: str | None = None, code: str = "forbidden") -> ApiError:
    return ApiError(403, code, message)


def not_found(message: str | None = None) -> ApiError:
    return ApiError(404, "not_found", message)


def conflict(message: str | None = None) -> ApiError:
    return ApiError(409, "conflict", message)


def _body(request: Request, code: str, message: str, extra: dict | None = None) -> dict:
    body = {"error": {"code": code, "message": message, "request_id": getattr(request.state, "request_id", None)}}
    if extra:
        body["error"].update(extra)
    return body


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        return JSONResponse(_body(request, exc.code, exc.message, exc.extra), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        details = [{"loc": [str(p) for p in e.get("loc", [])], "msg": e.get("msg", "")} for e in exc.errors()]
        return JSONResponse(
            _body(request, "validation_error", "Invalid request.", {"details": details}), status_code=422
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        code = {
            401: "unauthenticated",
            403: "forbidden",
            404: "not_found",
            405: "method_not_allowed",
            409: "conflict",
            501: "not_implemented",
        }.get(exc.status_code, "internal_error")
        return JSONResponse(_body(request, code, str(exc.detail)), status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        import logging

        logging.getLogger("app").exception(
            "unhandled error", extra={"request_id": getattr(request.state, "request_id", None)}
        )
        return JSONResponse(_body(request, "internal_error", "Unexpected server error."), status_code=500)
