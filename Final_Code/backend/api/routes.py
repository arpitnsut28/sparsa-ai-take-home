"""
api/routes.py — the HTTP surface.

Thin on purpose: parse, authorise (rate limit / capacity), delegate, map the
service's exceptions onto status codes. No business logic lives here.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response

import config
from models import (
    AcceptedResponse,
    ErrorResponse,
    RunListResponse,
    RunRequest,
    RunResponse,
)
from observability import metrics
from services.run_service import CapacityExceeded, RunService

logger = logging.getLogger("research_runs.api")

router = APIRouter()


def get_service(request: Request) -> RunService:
    return request.app.state.run_service


def client_key(request: Request) -> str:
    """Identify the caller for rate limiting."""
    if config.settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# --- operational endpoints -------------------------------------------------

@router.get("/health", tags=["ops"], summary="Liveness probe")
async def health() -> dict:
    """Cheap and dependency-free: is this process up?"""
    return {"status": "ok"}


@router.get("/ready", tags=["ops"], summary="Readiness probe")
async def ready(response: Response, service: RunService = Depends(get_service)) -> dict:
    """Should the load balancer send this instance work?

    Reports not-ready while draining or at capacity, so a rolling deploy stops
    routing new runs here instead of collecting 429s.
    """
    state = service.health()
    is_ready = state["capacity_available"] and not state["shutting_down"]
    if not is_ready:
        response.status_code = 503
    return {"status": "ready" if is_ready else "not_ready", **state}


@router.get("/metrics", tags=["ops"], summary="Counters and run latency")
async def get_metrics(service: RunService = Depends(get_service)) -> dict:
    return {**metrics.snapshot(), "service": service.health()}


# --- runs ------------------------------------------------------------------

@router.post(
    "/runs",
    response_model=AcceptedResponse,
    status_code=202,
    tags=["runs"],
    responses={
        422: {"model": ErrorResponse, "description": "Invalid prompt or URLs"},
        429: {"model": ErrorResponse, "description": "Rate limited or at capacity"},
    },
)
async def start_run(
    req: RunRequest,
    request: Request,
    response: Response,
    service: RunService = Depends(get_service),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    """Start a run. Returns immediately; poll ``GET /runs/{run_id}``.

    202 rather than 200: the work is accepted, not completed.
    """
    allowed, retry_after = request.app.state.rate_limiter.check(client_key(request))
    if not allowed:
        metrics.incr("requests_rate_limited")
        raise HTTPException(
            status_code=429,
            detail={"error": "Too many runs started. Please slow down.",
                    "error_code": "rate_limited"},
            headers={"Retry-After": str(max(1, int(retry_after)))},
        )

    if idempotency_key is not None and len(idempotency_key) > 200:
        raise HTTPException(
            status_code=422,
            detail={"error": "Idempotency-Key is too long.", "error_code": "invalid_header"},
        )

    try:
        run_id, created = service.create_run(
            prompt=req.prompt,
            urls=req.urls,
            idempotency_key=idempotency_key,
            request_id=getattr(request.state, "request_id", None),
        )
    except CapacityExceeded as exc:
        raise HTTPException(
            status_code=429,
            detail={"error": "The service is at capacity. Please retry shortly.",
                    "error_code": "at_capacity"},
            headers={"Retry-After": str(exc.retry_after_s)},
        ) from exc

    if not created:
        response.status_code = 200          # replay, nothing new was started
    response.headers["Location"] = f"/runs/{run_id}"
    return {"run_id": run_id}


@router.get(
    "/runs",
    response_model=RunListResponse,
    tags=["runs"],
    summary="Recent runs, newest first",
)
async def list_runs(
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    service: RunService = Depends(get_service),
) -> dict:
    """The run log the original kept in a local variable and then discarded."""
    records, total = service.list(limit=limit, offset=offset)
    return {
        "runs": [r.to_summary() for r in records],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get(
    "/runs/{run_id}",
    response_model=RunResponse,
    tags=["runs"],
    responses={404: {"model": ErrorResponse, "description": "Unknown or expired run"}},
)
async def get_run(run_id: str, response: Response, service: RunService = Depends(get_service)) -> dict:
    """Poll a run. An unknown id is a 404, not the original's unhandled KeyError."""
    record = service.get(run_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "Run not found. It may have expired.",
                    "error_code": "run_not_found"},
        )
    # A terminal run never changes: let clients and proxies cache it, and tell
    # a polling client how soon it is worth asking again while it is running.
    if record.is_terminal:
        response.headers["Cache-Control"] = "private, max-age=60"
    else:
        response.headers["Cache-Control"] = "no-store"
        response.headers["Retry-After"] = "1"
    return record.to_response()


@router.delete(
    "/runs/{run_id}",
    response_model=RunResponse,
    tags=["runs"],
    summary="Cancel an in-flight run",
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
async def cancel_run(run_id: str, service: RunService = Depends(get_service)) -> dict:
    record = service.get(run_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "Run not found.", "error_code": "run_not_found"},
        )
    if not service.cancel(run_id):
        raise HTTPException(
            status_code=409,
            detail={"error": f"Run already finished with status '{record.status}'.",
                    "error_code": "run_not_cancellable"},
        )
    logger.info("run %s cancelled by client", run_id)
    refreshed = service.get(run_id)
    return (refreshed or record).to_response()
