"""
models.py — request and response schemas.

The original accepted ``{prompt: str, urls: list[str]}`` with no constraints:
blank prompts, empty lists, 10,000 URLs, ``file:///etc/passwd`` and a 5 MB
prompt were all valid requests. Validation happens here so a bad request costs
a 422 instead of a worker thread.
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

import config
from services.urls import UrlRejected, normalise_many


class RunRequest(BaseModel):
    prompt: str
    urls: list[str]

    model_config = {"extra": "forbid"}

    @field_validator("prompt")
    @classmethod
    def _check_prompt(cls, value: str) -> str:
        prompt = value.strip()
        if not prompt:
            raise ValueError("prompt must not be blank")
        limit = config.settings.max_prompt_chars
        if len(prompt) > limit:
            raise ValueError(f"prompt exceeds {limit} characters (got {len(prompt)})")
        return prompt

    @field_validator("urls")
    @classmethod
    def _check_urls(cls, value: list[str]) -> list[str]:
        # Be liberal about the shape the textarea produces: a trailing newline
        # should not be a validation error. Be strict about everything else.
        candidates = [u.strip() for u in value if isinstance(u, str) and u.strip()]
        if not candidates:
            raise ValueError("at least one URL is required")
        try:
            return normalise_many(candidates)
        except UrlRejected as exc:
            raise ValueError(str(exc)) from exc


class PageFailure(BaseModel):
    url: str
    kind: str
    error: str


class RunResult(BaseModel):
    # --- the documented contract ------------------------------------------
    timestamp: str
    prompt: str
    pages_scraped: int
    success_rate: float
    tokens_used: int
    sources: list[str]
    brief: str
    # --- additive: what the KPIs could not previously express --------------
    pages_succeeded: int = 0
    pages_failed: int = 0
    duplicates_dropped: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    failures: list[PageFailure] = Field(default_factory=list)


class RunResponse(BaseModel):
    run_id: str
    status: str                          # running | done | error | cancelled
    result: Optional[RunResult] = None
    error: Optional[str] = None          # caller-safe message, never a stack trace
    error_code: Optional[str] = None     # stable, machine-readable
    created_at: str
    finished_at: Optional[str] = None


class RunSummary(BaseModel):
    run_id: str
    status: str
    prompt: str
    url_count: int
    created_at: str
    finished_at: Optional[str] = None
    success_rate: Optional[float] = None
    tokens_used: Optional[int] = None


class RunListResponse(BaseModel):
    runs: list[RunSummary]
    total: int
    limit: int
    offset: int


class AcceptedResponse(BaseModel):
    run_id: str


class ErrorResponse(BaseModel):
    """One envelope for every error, so clients parse one shape."""

    error: str
    error_code: str
    detail: Optional[Any] = None
    request_id: Optional[str] = None
