"""
main.py — Research Runs API.

A client POSTs a prompt and source URLs; the API admits the run, returns a
``run_id`` immediately, scrapes and synthesises in the background, and the
client polls ``GET /runs/{run_id}`` until the status is terminal.

    uvicorn main:app --reload        # http://localhost:8000

This module is wiring only — configuration, logging, middleware, error
envelopes and lifecycle. Behaviour lives in services/, the HTTP surface in
api/routes.py.
"""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

import config
from api.routes import router
from observability import configure_logging, metrics, new_request_id, request_id_var
from services.ratelimit import RateLimiter
from services.run_service import RunService

configure_logging()
logger = logging.getLogger("research_runs")

FRONTEND_FILE = Path(__file__).resolve().parent.parent / "frontend.html"


@asynccontextmanager
async def lifespan(app: FastAPI):
    if config.settings.is_production and not config.settings.allowed_origins:
        # Fail loudly rather than silently serving a browser API no browser can call.
        raise RuntimeError(
            "ENV=production requires ALLOWED_ORIGINS to be set explicitly."
        )
    if config.settings.is_production and config.settings.allow_private_network_urls:
        logger.warning("ALLOW_PRIVATE_NETWORK_URLS is enabled in production — SSRF guard is off")

    app.state.run_service = RunService()
    app.state.rate_limiter = RateLimiter()
    await app.state.run_service.start()
    logger.info("api ready env=%s origins=%s", config.settings.env, config.settings.cors_origins())
    try:
        yield
    finally:
        await app.state.run_service.stop()


app = FastAPI(
    title="Research Runs API",
    version="2.0.0",
    description="Submit a research prompt with source URLs; poll for the brief and KPIs.",
    lifespan=lifespan,
)

# Explicit allow-list. The original used allow_origins=["*"], which lets any
# site on the internet drive this API from a visitor's browser.
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.settings.cors_origins(),
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Idempotency-Key", "X-Request-ID"],
    expose_headers=["X-Request-ID", "Retry-After", "Location"],
    max_age=600,
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    """Correlate every log line and response with one request id, and cap body size."""
    request_id = request.headers.get("x-request-id") or new_request_id()
    # Never echo an unbounded client-supplied value back into logs and headers.
    request_id = request_id[:64]
    token = request_id_var.set(request_id)
    request.state.request_id = request_id

    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > config.settings.max_body_bytes:
        metrics.incr("requests_too_large")
        request_id_var.reset(token)
        return JSONResponse(
            status_code=413,
            content={
                "error": f"Request body exceeds {config.settings.max_body_bytes} bytes.",
                "error_code": "payload_too_large",
                "request_id": request_id,
            },
            headers={"X-Request-ID": request_id},
        )

    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        # The 500 handler below builds the response; this only records the miss.
        metrics.incr("requests_failed")
        logger.exception("unhandled error for %s %s", request.method, request.url.path)
        raise
    finally:
        request_id_var.reset(token)

    duration_ms = (time.perf_counter() - started) * 1000
    response.headers["X-Request-ID"] = request_id
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")

    metrics.incr("requests_total")
    metrics.incr(f"responses_{response.status_code // 100}xx")
    logger.info(
        "%s %s -> %d in %.1fms",
        request.method, request.url.path, response.status_code, duration_ms,
        extra={"request_id": request_id, "status": response.status_code,
               "duration_ms": round(duration_ms, 1)},
    )
    return response


# --- one error envelope for every failure ----------------------------------

def _envelope(status: int, error: str, code: str, request: Request, detail=None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": error,
            "error_code": code,
            "detail": detail,
            "request_id": getattr(request.state, "request_id", None),
        },
    )


@app.exception_handler(RequestValidationError)
async def on_validation_error(request: Request, exc: RequestValidationError):
    """422 with the field that failed — not FastAPI's raw internal dump."""
    problems = [
        {"field": ".".join(str(p) for p in err.get("loc", ()) if p != "body"),
         "message": err.get("msg", "invalid value")}
        for err in exc.errors()
    ]
    metrics.incr("requests_invalid")
    logger.info("422 %s %s: %s", request.method, request.url.path, problems)
    return _envelope(422, "The request was not valid.", "validation_error", request, problems)


@app.exception_handler(StarletteHTTPException)
async def on_http_error(request: Request, exc: StarletteHTTPException):
    detail = exc.detail
    if isinstance(detail, dict):
        message = detail.get("error", "Request failed.")
        code = detail.get("error_code", f"http_{exc.status_code}")
        extra = detail.get("detail")
    else:
        message = str(detail)
        code = f"http_{exc.status_code}"
        extra = None
    response = _envelope(exc.status_code, message, code, request, extra)
    for key, value in (getattr(exc, "headers", None) or {}).items():
        response.headers[key] = value
    return response


@app.exception_handler(Exception)
async def on_unhandled(request: Request, exc: Exception):
    """Never leak an internal error to a client; always leave a trace in the log."""
    logger.exception("unhandled exception on %s %s", request.method, request.url.path)
    return _envelope(500, "An internal error occurred.", "internal_error", request)


app.include_router(router)


# --- the page ---------------------------------------------------------------
# Serving the frontend from the API means the browser makes same-origin calls:
# no CORS in the request path at all, and one URL to open.

if config.settings.serve_frontend and FRONTEND_FILE.is_file():

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(
            FRONTEND_FILE,
            media_type="text/html",
            headers={
                "Cache-Control": "no-cache",
                "Content-Security-Policy": (
                    "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                    "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                    "connect-src 'self' http://localhost:8000 http://127.0.0.1:8000; "
                    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
                ),
            },
        )
