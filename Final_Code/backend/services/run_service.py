"""
services/run_service.py — run lifecycle: admission, execution, cancellation,
and draining on shutdown.

The original was four lines of ``asyncio.create_task`` with no admission
control, no task ownership, no timeout and no error path. This owns all four:

* **admission** — a bounded number of runs may be in flight or queued; past
  that the API sheds load with 429 rather than accepting work it cannot do;
* **ownership** — task references are held, because a task that only the event
  loop refers to can be garbage-collected mid-flight (a documented asyncio
  footgun) and the run would silently stay "running" forever;
* **deadline** — every run is wrapped in a whole-run timeout, so a wedged
  upstream produces a failed run instead of a permanent one;
* **drain** — on shutdown in-flight runs are cancelled and recorded as such,
  instead of vanishing with the process while a client polls.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import config
from observability import metrics, run_id_var
from services import stubs
from services.pipeline import NoUsableSources, run_pipeline
from services.retry import TimeoutExceeded
from services.store import RunRecord, RunStore

logger = logging.getLogger("research_runs.service")


class CapacityExceeded(Exception):
    """The service is at its admission limit."""

    def __init__(self, retry_after_s: int = 5):
        super().__init__("service at capacity")
        self.retry_after_s = retry_after_s


class RunService:
    def __init__(self, store: Optional[RunStore] = None) -> None:
        self.store = store or RunStore()
        self._tasks: set[asyncio.Task] = set()
        self._executor: Optional[ThreadPoolExecutor] = None
        self._slots: Optional[asyncio.Semaphore] = None
        self._sweeper: Optional[asyncio.Task] = None
        self._admitted = 0          # accepted and not yet finished
        self._executing = 0         # past the semaphore, actually working
        self._shutting_down = False

    # --- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, config.settings.worker_threads),
            thread_name_prefix="run-worker",
        )
        self._slots = asyncio.Semaphore(max(1, config.settings.max_concurrent_runs))
        self._sweeper = asyncio.create_task(self._sweep_loop(), name="run-store-sweeper")
        logger.info(
            "run service started max_concurrent=%d max_queued=%d workers=%d",
            config.settings.max_concurrent_runs,
            config.settings.max_queued_runs,
            config.settings.worker_threads,
        )

    async def stop(self) -> None:
        """Drain in-flight work so a deploy does not silently drop live runs."""
        self._shutting_down = True

        if self._sweeper is not None:
            self._sweeper.cancel()
            await asyncio.gather(self._sweeper, return_exceptions=True)
            self._sweeper = None

        pending = list(self._tasks)
        if pending:
            logger.info("draining %d in-flight run(s)", len(pending))
            for task in pending:
                task.cancel()
            done, still_running = await asyncio.wait(
                pending, timeout=config.settings.shutdown_grace_s
            )
            if still_running:
                logger.warning("%d run(s) did not stop within the grace period", len(still_running))

        if self._executor is not None:
            # Do not block the loop waiting on abandoned worker threads.
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None
        logger.info("run service stopped")

    async def _sweep_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(config.settings.sweep_interval_s)
                self.store.sweep()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A sweeper crash must not take maintenance down permanently.
                logger.exception("run store sweep failed")

    # --- admission ---------------------------------------------------------

    def capacity_available(self) -> bool:
        limit = config.settings.max_concurrent_runs + config.settings.max_queued_runs
        return self._admitted < limit

    def create_run(
        self,
        prompt: str,
        urls: list[str],
        idempotency_key: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> tuple[str, bool]:
        """Admit a run and schedule it. Returns (run_id, created).

        ``created`` is False when an idempotency key replays an existing run —
        a double-submitted form must not buy a second LLM call.
        """
        if self._shutting_down:
            raise CapacityExceeded(retry_after_s=15)

        if idempotency_key:
            existing = self.store.lookup_key(idempotency_key)
            if existing and self.store.get(existing) is not None:
                logger.info("idempotent replay of run %s", existing)
                metrics.incr("runs_idempotent_replay")
                return existing, False

        if not self.capacity_available():
            metrics.incr("runs_rejected_capacity")
            logger.warning("rejecting run: %d already admitted", self._admitted)
            raise CapacityExceeded()

        run_id = uuid.uuid4().hex
        self.store.put(
            RunRecord(
                run_id=run_id,
                status="running",
                prompt=prompt,
                url_count=len(urls),
                request_id=request_id,
            )
        )
        if idempotency_key:
            self.store.remember_key(idempotency_key, run_id)

        self._admitted += 1
        task = asyncio.create_task(self._execute(run_id, prompt, urls), name=f"run-{run_id}")
        # Hold a strong reference: asyncio only keeps a weak one, so a task with
        # no other referent can be collected mid-await.
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

        metrics.incr("runs_started")
        logger.info(
            "run queued prompt_chars=%d urls=%d admitted=%d",
            len(prompt), len(urls), self._admitted,
            extra={"run_id": run_id},
        )
        return run_id, True

    # --- execution ---------------------------------------------------------

    async def _execute(self, run_id: str, prompt: str, urls: list[str]) -> None:
        token = run_id_var.set(run_id)
        started = time.perf_counter()
        try:
            assert self._slots is not None
            async with self._slots:          # queue here when at concurrency cap
                self._executing += 1
                try:
                    result = await asyncio.wait_for(
                        run_pipeline(prompt, urls, self._executor),
                        timeout=config.settings.run_timeout_s,
                    )
                finally:
                    self._executing -= 1

            self._finish(run_id, status="done", result=result)
            metrics.incr("runs_done")
            metrics.incr("pages_scraped", result["pages_scraped"])
            metrics.incr("pages_failed", result["pages_failed"])
            metrics.incr("tokens_used", result["tokens_used"])
            logger.info(
                "run done pages=%d success_rate=%.2f tokens=%d duration_ms=%d",
                result["pages_scraped"], result["success_rate"],
                result["tokens_used"], result["duration_ms"],
            )

        except asyncio.CancelledError:
            # Cancelled by an operator, by shutdown, or by a client DELETE.
            self._finish(
                run_id,
                status="cancelled",
                error="Run was cancelled before it completed.",
                error_code="cancelled",
            )
            metrics.incr("runs_cancelled")
            logger.warning("run cancelled")
            raise

        except asyncio.TimeoutError:
            self._fail(run_id, "run_timeout",
                       f"Run exceeded the {config.settings.run_timeout_s:.0f}s time limit.")
        except NoUsableSources as exc:
            self._fail(run_id, "all_sources_failed", str(exc), level=logging.WARNING)
        except TimeoutExceeded:
            self._fail(run_id, "upstream_timeout", "An upstream service timed out. Please retry.")
        except stubs.LLMError as exc:
            self._fail(run_id, exc.kind or "llm_error",
                       "The synthesis step failed. Please retry.", detail=str(exc))
        except Exception as exc:
            # Nothing may escape: an uncaught exception here would leave the run
            # stuck at "running" and the client polling forever.
            self._fail(run_id, "internal_error",
                       "An internal error occurred. Please try again.",
                       detail=f"{type(exc).__name__}: {exc}", level=logging.ERROR, exc_info=True)
        finally:
            self._admitted = max(0, self._admitted - 1)
            metrics.observe_run_duration(time.perf_counter() - started)
            run_id_var.reset(token)

    def _finish(self, run_id: str, *, status: str, result: Optional[dict] = None,
                error: Optional[str] = None, error_code: Optional[str] = None) -> None:
        record = self.store.get(run_id)
        if record is None:                   # evicted mid-flight; nothing to update
            return
        record.status = status
        record.result = result
        record.error = error
        record.error_code = error_code
        record.finished_at = time.time()

    def _fail(self, run_id: str, code: str, message: str, *, detail: str = "",
              level: int = logging.ERROR, exc_info: bool = False) -> None:
        """Record a failed run.

        ``message`` is what the client sees — no stack traces, no upstream
        internals. ``detail`` goes to the log only, correlated by run id.
        """
        self._finish(run_id, status="error", error=message, error_code=code)
        metrics.incr("runs_failed")
        metrics.incr(f"runs_failed_{code}")
        logger.log(level, "run failed code=%s %s", code, detail or message, exc_info=exc_info)

    # --- queries -----------------------------------------------------------

    def get(self, run_id: str) -> Optional[RunRecord]:
        return self.store.get(run_id)

    def list(self, limit: int = 50, offset: int = 0):
        return self.store.list(limit=limit, offset=offset)

    def cancel(self, run_id: str) -> bool:
        """Cancel an in-flight run. Returns False if it is already terminal."""
        record = self.store.get(run_id)
        if record is None or record.is_terminal:
            return False

        task = next((t for t in self._tasks if t.get_name() == f"run-{run_id}"), None)
        if task is not None:
            task.cancel()

        # Record the terminal state now rather than waiting for the cancellation
        # to be delivered, so the DELETE response is truthful. The task's own
        # handler writes the same state; the write is idempotent.
        self._finish(run_id, status="cancelled",
                     error="Run was cancelled before it completed.", error_code="cancelled")
        return True

    def health(self) -> dict:
        return {
            "admitted": self._admitted,
            "executing": self._executing,
            "stored_runs": len(self.store),
            "active_runs": self.store.active_count(),
            "evicted_total": self.store.evicted_total,
            "capacity_available": self.capacity_available(),
            "shutting_down": self._shutting_down,
        }
