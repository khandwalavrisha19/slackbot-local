from fastapi import Request, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.constants import MAX_BODY_BYTES
from app.logger import logger


# ── REQUEST SIZE LIMIT MIDDLEWARE ─────────────────────────────────────────────

async def limit_request_size(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_BODY_BYTES:
        logger.warning("Request body too large", extra={
            "path":           request.url.path,
            "content_length": content_length,
            "limit_bytes":    MAX_BODY_BYTES,
        })
        return JSONResponse(
            status_code=413,
            content={"ok": False, "error": f"Request body exceeds {MAX_BODY_BYTES // 1024} KB limit"},
        )
    return await call_next(request)


# ── GLOBAL EXCEPTION HANDLERS ─────────────────────────────────────────────────

async def validation_error_handler(request: Request, exc: RequestValidationError):
    errors = exc.errors()
    logger.warning("Validation error", extra={
        "path":   request.url.path,
        "errors": [{"field": ".".join(str(l) for l in e["loc"]), "msg": e["msg"]} for e in errors],
    })
    return JSONResponse(
        status_code=422,
        content={
            "ok":      False,
            "error":   "Validation failed",
            "details": [
                {"field": ".".join(str(l) for l in e["loc"]), "message": e["msg"]}
                for e in errors
            ],
        },
    )


async def http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code >= 500:
        logger.error("HTTP error", extra={
            "path": request.url.path, "status": exc.status_code, "detail": exc.detail,
        })
    else:
        logger.warning("HTTP error", extra={
            "path": request.url.path, "status": exc.status_code, "detail": exc.detail,
        })
    return JSONResponse(
        status_code=exc.status_code,
        content={"ok": False, "error": exc.detail},
    )


async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.error("Unhandled exception", extra={
        "path":  request.url.path,
        "error": str(exc),
        "type":  type(exc).__name__,
    })
    return JSONResponse(
        status_code=500,
        content={"ok": False, "error": "An internal server error occurred. Please try again."},
    )


def register_exception_handlers(app):
    """Register all exception handlers and middleware onto the FastAPI app."""
    from fastapi.exceptions import RequestValidationError

    app.middleware("http")(limit_request_size)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)