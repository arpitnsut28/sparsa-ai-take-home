"""
Shared fixtures.

Every test gets a fresh app instance with the rate limiter and the run store
reset, so tests cannot leak state into each other — the kind of shared-state
bug the original ``RUNS = {}`` module global made easy to write.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from observability import metrics  # noqa: E402

# The test client's own HTTP chatter is not the code under test.
logging.getLogger("httpx").setLevel(logging.WARNING)


@pytest.fixture(autouse=True)
def fast_and_deterministic():
    """Reset settings between tests and make the stubs instant."""
    original = config.settings
    config.settings = config.Settings()
    config.settings.stub_latency_ms = 0
    config.settings.stub_failure_rate = 0.0
    config.settings.rate_limit_enabled = False
    config.settings.retry_base_delay_s = 0.001
    config.settings.retry_max_delay_s = 0.002
    config.settings.sweep_interval_s = 3600
    metrics.reset()
    yield
    config.settings = original


@pytest.fixture
def client():
    import main
    with TestClient(main.app) as c:
        c.app.state.rate_limiter.reset()
        yield c


VALID = {
    "prompt": "Summarise the state of play",
    "urls": ["https://example.com/a", "https://example.org/b"],
}


def start_and_wait(client, payload=None, timeout_s: float = 10.0) -> dict:
    """POST a run and poll until it reaches a terminal state."""
    import time

    response = client.post("/runs", json=payload or VALID)
    assert response.status_code in (200, 202), response.text
    run_id = response.json()["run_id"]

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        body = client.get(f"/runs/{run_id}").json()
        if body["status"] != "running":
            return body
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} never finished")
