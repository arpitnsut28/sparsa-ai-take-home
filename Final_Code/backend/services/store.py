"""
services/store.py — the in-memory run store.

``RUNS = {}`` in the original is a memory leak with a long fuse: nothing is ever
removed, so a service that accepts runs forever grows forever. This adds a TTL
and a hard cap, both swept on a schedule, and keeps the eviction policy in one
testable place.

Single-process and non-durable by design (see SIGNOFF.md). The interface is
deliberately the small set of operations Redis or Postgres would also provide,
so swapping the backing store does not reach into the service layer.
"""
from __future__ import annotations

import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Optional

import config

logger = logging.getLogger("research_runs.store")

# Terminal states: a run in one of these will never change again.
TERMINAL = ("done", "error", "cancelled")


@dataclass
class RunRecord:
    run_id: str
    status: str = "running"
    prompt: str = ""
    url_count: int = 0
    result: Optional[dict] = None
    error: Optional[str] = None
    error_code: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    request_id: Optional[str] = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL

    def to_response(self) -> dict:
        """Public shape. ``status``/``result`` match the documented contract."""
        return {
            "run_id": self.run_id,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "error_code": self.error_code,
            "created_at": _iso(self.created_at),
            "finished_at": _iso(self.finished_at) if self.finished_at else None,
        }

    def to_summary(self) -> dict:
        """Compact shape for the run list."""
        return {
            "run_id": self.run_id,
            "status": self.status,
            "prompt": self.prompt,
            "url_count": self.url_count,
            "created_at": _iso(self.created_at),
            "finished_at": _iso(self.finished_at) if self.finished_at else None,
            "success_rate": (self.result or {}).get("success_rate"),
            "tokens_used": (self.result or {}).get("tokens_used"),
        }


def _iso(epoch: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


class RunStore:
    """Bounded, TTL'd map of run_id -> RunRecord, newest last.

    All access happens on the event loop thread, so no lock is needed; the
    background work is handed to an executor but only ever reports back here
    from a coroutine.
    """

    def __init__(self) -> None:
        self._runs: "OrderedDict[str, RunRecord]" = OrderedDict()
        self._idempotency: dict[str, tuple[str, float]] = {}
        self.evicted_total = 0

    # --- runs --------------------------------------------------------------

    def put(self, record: RunRecord) -> None:
        self._runs[record.run_id] = record
        self._runs.move_to_end(record.run_id)
        self._enforce_capacity()

    def get(self, run_id: str) -> Optional[RunRecord]:
        record = self._runs.get(run_id)
        if record is None:
            return None
        if self._is_expired(record, time.time()):
            # Expired but not yet swept: behave as if it were already gone, so
            # a run's visible lifetime does not depend on sweeper timing.
            self._runs.pop(run_id, None)
            self.evicted_total += 1
            return None
        return record

    def list(self, limit: int = 50, offset: int = 0) -> tuple[list[RunRecord], int]:
        """Newest first, with the total for pagination."""
        records = [r for r in reversed(self._runs.values()) if not self._is_expired(r, time.time())]
        return records[offset : offset + limit], len(records)

    def __len__(self) -> int:
        return len(self._runs)

    def active_count(self) -> int:
        return sum(1 for r in self._runs.values() if not r.is_terminal)

    # --- idempotency -------------------------------------------------------

    def remember_key(self, key: str, run_id: str) -> None:
        self._idempotency[key] = (run_id, time.time() + config.settings.idempotency_ttl_s)

    def lookup_key(self, key: str) -> Optional[str]:
        entry = self._idempotency.get(key)
        if entry is None:
            return None
        run_id, expires_at = entry
        if expires_at < time.time():
            self._idempotency.pop(key, None)
            return None
        return run_id

    # --- maintenance -------------------------------------------------------

    def _is_expired(self, record: RunRecord, now: float) -> bool:
        # Only terminal runs expire. An in-flight run outliving the TTL is a
        # stuck run — evicting it would hide the problem and 404 a live client.
        return record.is_terminal and (now - record.created_at) > config.settings.run_ttl_s

    def _enforce_capacity(self) -> None:
        """Drop the oldest terminal runs once the cap is exceeded."""
        cap = max(1, config.settings.max_stored_runs)
        if len(self._runs) <= cap:
            return
        for run_id in list(self._runs.keys()):
            if len(self._runs) <= cap:
                break
            if self._runs[run_id].is_terminal:
                self._runs.pop(run_id, None)
                self.evicted_total += 1

    def sweep(self) -> int:
        """Remove expired runs and idempotency keys. Returns runs removed."""
        now = time.time()
        expired = [rid for rid, rec in self._runs.items() if self._is_expired(rec, now)]
        for run_id in expired:
            self._runs.pop(run_id, None)
        self.evicted_total += len(expired)

        for key in [k for k, (_, exp) in self._idempotency.items() if exp < now]:
            self._idempotency.pop(key, None)

        self._enforce_capacity()
        if expired:
            logger.info("swept %d expired run(s); %d retained", len(expired), len(self._runs))
        return len(expired)
