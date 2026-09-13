"""
config.py — every tunable in one place, read from the environment.

Import ``settings`` and read attributes at call time (``config.settings.foo``)
rather than copying values into module constants: tests override attributes on
this instance, and a copied constant would not see the override.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _csv(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name)
    if raw is None:
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


# Origins that are useful in local development. A page opened straight from
# disk (file://) sends `Origin: null`, which is why that literal is here — the
# brief says "open frontend.html in your browser", and that is the request the
# browser actually makes. It is only ever added when ENV != "production".
_DEV_ORIGINS = [
    "null",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "http://localhost:3000",
    "http://localhost:5173",
    "http://localhost:8080",
]


@dataclass
class Settings:
    # --- environment -------------------------------------------------------
    env: str = field(default_factory=lambda: os.getenv("ENV", "development").lower())

    # --- logging / observability ------------------------------------------
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO").upper())
    # "json" for shippable structured logs, "text" for readable local output.
    log_format: str = field(default_factory=lambda: os.getenv("LOG_FORMAT", "text").lower())

    # --- CORS --------------------------------------------------------------
    allowed_origins: list[str] = field(default_factory=lambda: _csv("ALLOWED_ORIGINS", []))

    # --- request limits ----------------------------------------------------
    max_body_bytes: int = field(default_factory=lambda: _int("MAX_BODY_BYTES", 64 * 1024))
    max_prompt_chars: int = field(default_factory=lambda: _int("MAX_PROMPT_CHARS", 2000))
    max_urls_per_run: int = field(default_factory=lambda: _int("MAX_URLS_PER_RUN", 20))
    max_url_chars: int = field(default_factory=lambda: _int("MAX_URL_CHARS", 2048))

    # --- SSRF guard --------------------------------------------------------
    # Off by default: user-supplied URLs are fetched server-side, so private
    # and loopback targets are refused unless an operator opts in.
    allow_private_network_urls: bool = field(
        default_factory=lambda: _bool("ALLOW_PRIVATE_NETWORK_URLS", False)
    )

    # --- concurrency & backpressure ---------------------------------------
    max_concurrent_runs: int = field(default_factory=lambda: _int("MAX_CONCURRENT_RUNS", 4))
    max_queued_runs: int = field(default_factory=lambda: _int("MAX_QUEUED_RUNS", 16))
    page_concurrency: int = field(default_factory=lambda: _int("PAGE_CONCURRENCY", 5))
    worker_threads: int = field(default_factory=lambda: _int("WORKER_THREADS", 16))

    # --- timeouts & retries (seconds) -------------------------------------
    scrape_timeout_s: float = field(default_factory=lambda: _float("SCRAPE_TIMEOUT_S", 10.0))
    llm_timeout_s: float = field(default_factory=lambda: _float("LLM_TIMEOUT_S", 60.0))
    run_timeout_s: float = field(default_factory=lambda: _float("RUN_TIMEOUT_S", 180.0))
    scrape_max_attempts: int = field(default_factory=lambda: _int("SCRAPE_MAX_ATTEMPTS", 3))
    llm_max_attempts: int = field(default_factory=lambda: _int("LLM_MAX_ATTEMPTS", 3))
    retry_base_delay_s: float = field(default_factory=lambda: _float("RETRY_BASE_DELAY_S", 0.25))
    retry_max_delay_s: float = field(default_factory=lambda: _float("RETRY_MAX_DELAY_S", 4.0))
    shutdown_grace_s: float = field(default_factory=lambda: _float("SHUTDOWN_GRACE_S", 10.0))

    # --- run store ---------------------------------------------------------
    run_ttl_s: float = field(default_factory=lambda: _float("RUN_TTL_S", 3600.0))
    max_stored_runs: int = field(default_factory=lambda: _int("MAX_STORED_RUNS", 1000))
    sweep_interval_s: float = field(default_factory=lambda: _float("SWEEP_INTERVAL_S", 60.0))
    idempotency_ttl_s: float = field(default_factory=lambda: _float("IDEMPOTENCY_TTL_S", 900.0))

    # --- rate limiting (per client IP, this process only) ------------------
    rate_limit_enabled: bool = field(default_factory=lambda: _bool("RATE_LIMIT_ENABLED", True))
    rate_limit_per_minute: float = field(default_factory=lambda: _float("RATE_LIMIT_PER_MINUTE", 30.0))
    rate_limit_burst: int = field(default_factory=lambda: _int("RATE_LIMIT_BURST", 10))
    # Only trust X-Forwarded-For when a proxy you control actually sets it.
    # Trusting it by default lets any client forge its own rate-limit identity.
    trust_proxy_headers: bool = field(default_factory=lambda: _bool("TRUST_PROXY_HEADERS", False))

    # --- LLM context budget ------------------------------------------------
    max_chars_per_page: int = field(default_factory=lambda: _int("MAX_CHARS_PER_PAGE", 8000))
    max_context_chars: int = field(default_factory=lambda: _int("MAX_CONTEXT_CHARS", 60000))

    # --- stub behaviour (delete together with services/stubs.py) -----------
    stub_latency_ms: int = field(default_factory=lambda: _int("STUB_LATENCY_MS", 200))
    stub_failure_rate: float = field(default_factory=lambda: _float("STUB_FAILURE_RATE", 0.0))

    # --- static frontend ---------------------------------------------------
    serve_frontend: bool = field(default_factory=lambda: _bool("SERVE_FRONTEND", True))

    @property
    def is_production(self) -> bool:
        return self.env in ("production", "prod")

    def cors_origins(self) -> list[str]:
        """Effective CORS allow-list.

        Production uses exactly what the operator configured — nothing is added
        implicitly. Development falls back to the local origins above so the
        page works whether it is opened from disk or served.
        """
        if self.is_production:
            return list(self.allowed_origins)
        return list(dict.fromkeys([*self.allowed_origins, *_DEV_ORIGINS]))


settings = Settings()
