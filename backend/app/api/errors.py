"""One error shape for every failure:

    {"error": {"code": "NOT_FOUND", "message": "...", "correlation_id": "...", "details": [...]}}
"""
import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.processing.errors import DocumentError

logger = logging.getLogger(__name__)

HTTP_CODES = {400: "BAD_REQUEST", 401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "NOT_FOUND",
              405: "METHOD_NOT_ALLOWED", 409: "CONFLICT", 413: "FILE_TOO_LARGE",
              415: "UNSUPPORTED_TYPE", 422: "VALIDATION_ERROR", 429: "RATE_LIMITED",
              503: "SERVICE_UNAVAILABLE"}


_SOURCES = ("body", "query", "path", "header", "cookie")


def correlation_id(request: Request) -> str:
    return getattr(request.state, "correlation_id", None) or request.headers.get(
        "X-Correlation-ID", "-")


def envelope(request: Request, status_code: int, code: str, message: str,
             details=None, headers: dict | None = None) -> JSONResponse:
    body = {"code": code, "message": message, "correlation_id": correlation_id(request)}
    if details is not None:
        body["details"] = details
    return JSONResponse({"error": body}, status_code=status_code, headers=headers)


def status_for(err: DocumentError) -> int:
    return {"FILE_TOO_LARGE": 413, "UNSUPPORTED_TYPE": 415, "EMPTY_FILE": 422,
            "LLM_NOT_CONFIGURED": 422, "INTERNAL_ERROR": 500}.get(err.code.value, 400)


def install(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        code = HTTP_CODES.get(exc.status_code, f"HTTP_{exc.status_code}")
        return envelope(request, exc.status_code, code, str(exc.detail),
                        headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        details = [{"field": ".".join(str(p) for p in (
            e["loc"][1:] if e["loc"][:1] and e["loc"][0] in _SOURCES else e["loc"])),
            "message": e["msg"]} for e in exc.errors()]         # never echo the submitted input
        return envelope(request, 422, "VALIDATION_ERROR", "The request is not valid.", details)

    @app.exception_handler(DocumentError)
    async def document_error(request: Request, exc: DocumentError):
        return envelope(request, status_for(exc), exc.code.value, exc.message)

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception):
        logger.exception(f"Unhandled error [{correlation_id(request)}]")
        return envelope(request, 500, "INTERNAL_ERROR",
                        "Something went wrong on our side. Please try again.")
