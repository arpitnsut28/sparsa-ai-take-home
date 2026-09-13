"""
services/stubs.py — the stubbed externals, kept behind the same shapes as prod.

The brief ships these so the service runs with no keys and no network. They are
quarantined in one module so the seam with the real clients is obvious: replace
the two functions, delete the fault-injection knobs, and nothing above this
layer changes.

Both functions are *blocking* on purpose — they stand in for ``requests.get``
and a synchronous SDK call. The callers run them on a worker thread. When the
real async clients land (``httpx.AsyncClient``, ``AsyncAnthropic``), these
become ``async def`` and the executor in run_service.py goes away.
"""
from __future__ import annotations

import random
import time

import config


class ScrapeError(Exception):
    """Scraping a single page failed. ``kind`` is a short, stable label."""

    def __init__(self, message: str, kind: str = "scrape_error", retryable: bool = False):
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable


class LLMError(Exception):
    """The synthesis call failed."""

    def __init__(self, message: str, kind: str = "llm_error", retryable: bool = False):
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable


def _simulated_latency() -> None:
    delay_ms = config.settings.stub_latency_ms
    if delay_ms > 0:
        time.sleep(delay_ms / 1000.0)


def _maybe_fail(url: str) -> None:
    """Fault injection for the stub only.

    Real deployments see 404s, TLS errors, robots blocks and timeouts on a
    meaningful share of URLs; with a stub that always succeeds, ``success_rate``
    is permanently 1.0 and the failure paths are never exercised. Set
    STUB_FAILURE_RATE (0.0-1.0) to make the stub behave like the real world.
    Defaults to 0.0, and disappears with this module.
    """
    rate = config.settings.stub_failure_rate
    if rate <= 0:
        return
    # Seeded per URL: the same URL fails consistently within a deployment,
    # which keeps manual testing and reproduction sane.
    if random.Random(f"{url}|{rate}").random() < rate:
        raise ScrapeError(f"upstream returned 503 for {url}", kind="upstream_error", retryable=True)


def scrape_page(url: str) -> dict:
    """Fetch one page. Blocking.

    prod: Firecrawl (or requests + readability) — and the place to enforce
    resolved-IP checks, redirect limits, and a response size cap.
    """
    _simulated_latency()
    _maybe_fail(url)
    return {
        "url": url,
        "title": f"Title for {url}",
        "content": f"Scraped body of {url}. " * 20,
    }


def call_llm(prompt: str) -> object:
    """Synthesise a brief. Blocking. Returns an Anthropic-shaped message.

    prod: ``anthropic_client.messages.create(...)``.
    """
    _simulated_latency()

    class _Block:
        def __init__(self, t):
            self.text = t
            self.type = "text"

    class _Usage:
        input_tokens = 1200
        output_tokens = 280

    class _Msg:
        content = [_Block("- Key finding A\n- Key finding B")]
        usage = _Usage()
        stop_reason = "end_turn"

    return _Msg()
