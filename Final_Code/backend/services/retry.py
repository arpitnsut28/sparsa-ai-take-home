"""
services/retry.py — run a blocking callable on a worker thread, with a timeout
and bounded retries.

Every external call in this service goes through here. Three rules it enforces:

* a call that hangs is bounded by ``timeout_s`` — a scraper with no timeout is
  how one slow host holds a worker forever;
* only errors flagged ``retryable`` are retried, so a 404 is not hammered three
  times;
* backoff is exponential with full jitter, which stops a batch of retries from
  re-colliding in lockstep against an upstream that is already struggling.

``asyncio.CancelledError`` is re-raised untouched so run cancellation and
shutdown stay prompt.
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Callable, TypeVar

import config

logger = logging.getLogger("research_runs.retry")

T = TypeVar("T")


class TimeoutExceeded(Exception):
    """A call exceeded its per-attempt timeout on every attempt."""


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with full jitter (attempt is 1-based)."""
    ceiling = min(
        config.settings.retry_max_delay_s,
        config.settings.retry_base_delay_s * (2 ** (attempt - 1)),
    )
    return random.uniform(0, ceiling)


async def call_with_retry(
    fn: Callable[..., T],
    *args,
    executor,
    timeout_s: float,
    max_attempts: int,
    retry_on: tuple[type[Exception], ...] = (),
    is_retryable: Callable[[Exception], bool] | None = None,
    label: str = "call",
) -> T:
    """Await ``fn(*args)`` on ``executor``, retrying transient failures.

    Raises the last exception if every attempt fails, or ``TimeoutExceeded``
    if the failures were timeouts.
    """
    loop = asyncio.get_running_loop()
    last_exc: Exception | None = None

    for attempt in range(1, max(1, max_attempts) + 1):
        try:
            # run_in_executor cannot interrupt a thread that is already running;
            # wait_for bounds how long *we* wait, and the worker is abandoned.
            # Real clients get a client-side timeout too, so the thread ends.
            return await asyncio.wait_for(
                loop.run_in_executor(executor, fn, *args), timeout=timeout_s
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            last_exc = TimeoutExceeded(f"{label} timed out after {timeout_s:.1f}s")
            retryable = True
            logger.warning("%s attempt %d/%d timed out", label, attempt, max_attempts)
        except retry_on as exc:  # type: ignore[misc]
            last_exc = exc
            retryable = is_retryable(exc) if is_retryable else True
            logger.warning(
                "%s attempt %d/%d failed: %s", label, attempt, max_attempts, exc
            )
        if not retryable or attempt >= max_attempts:
            break
        await asyncio.sleep(_backoff_delay(attempt))

    assert last_exc is not None
    raise last_exc
