"""
observability.py — structured logging, request correlation, and counters.

"Runs stay observable" is one of the brief's explicit bars. Concretely that
means: every log line can be tied to a request and a run, failures carry a
stable machine-readable code, and there is a numeric endpoint to alert on.

The original service logged nothing at all — a failed run was invisible.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from contextvars import ContextVar

import config

# Propagates the current request id into log records emitted anywhere downstream,
# including inside background tasks that outlive the request.
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
run_id_var: ContextVar[str] = ContextVar("run_id", default="-")

_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message", "asctime", "taskName",
}


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        record.run_id = run_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line — what a log shipper wants."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
            "run_id": getattr(record, "run_id", "-"),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key not in payload:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.addFilter(ContextFilter())
    if config.settings.log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-7s [%(name)s] req=%(request_id)s run=%(run_id)s %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        )

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(getattr(logging, config.settings.log_level, logging.INFO))
    # uvicorn installs its own handlers; route them through ours so every line
    # is in one format with correlation ids attached.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


class Metrics:
    """Process-local counters.

    Intentionally tiny. In a real deployment these become Prometheus counters
    and histograms via prometheus-client; the endpoint below is the placeholder
    that makes the numbers visible today.
    """

    def __init__(self) -> None:
        self.started_at = time.time()
        self.counters: dict[str, float] = {}
        self._latencies: list[float] = []

    def incr(self, name: str, amount: float = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + amount

    def observe_run_duration(self, seconds: float) -> None:
        self._latencies.append(seconds)
        # Keep the window bounded — this is a metrics buffer, not a log.
        if len(self._latencies) > 1000:
            del self._latencies[: len(self._latencies) - 1000]

    def snapshot(self) -> dict:
        latencies = sorted(self._latencies)
        def pct(p: float) -> float | None:
            if not latencies:
                return None
            idx = min(len(latencies) - 1, int(round(p * (len(latencies) - 1))))
            return round(latencies[idx], 3)

        return {
            "uptime_s": round(time.time() - self.started_at, 1),
            "counters": dict(sorted(self.counters.items())),
            "run_duration_s": {
                "count": len(latencies),
                "p50": pct(0.50),
                "p95": pct(0.95),
                "max": round(latencies[-1], 3) if latencies else None,
            },
        }

    def reset(self) -> None:
        self.counters.clear()
        self._latencies.clear()


metrics = Metrics()
