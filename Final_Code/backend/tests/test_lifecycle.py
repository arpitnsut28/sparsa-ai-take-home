"""Startup checks and shutdown draining."""
import asyncio
import time

import pytest
from fastapi.testclient import TestClient

import config
from services import stubs
from tests.conftest import VALID


def test_production_requires_explicit_cors_origins():
    """A production deploy that silently allows nothing (or everything) is worse
    than one that refuses to start."""
    import main
    config.settings.env = "production"
    config.settings.allowed_origins = []
    with pytest.raises(RuntimeError, match="ALLOWED_ORIGINS"):
        with TestClient(main.app):
            pass


def test_shutdown_drains_in_flight_runs(monkeypatch):
    """A deploy must not leave a client polling a run the process forgot."""
    import main
    config.settings.shutdown_grace_s = 2
    monkeypatch.setattr(
        stubs, "scrape_page",
        lambda url: (time.sleep(5), {"url": url, "title": "t", "content": "x " * 50})[1],
    )

    with TestClient(main.app) as client:
        client.app.state.rate_limiter.reset()
        run_id = client.post("/runs", json=VALID).json()["run_id"]
        assert client.get(f"/runs/{run_id}").json()["status"] == "running"
        service = client.app.state.run_service

    # The context manager has exited, so lifespan shutdown has run.
    record = service.store.get(run_id)
    assert record is not None
    assert record.status == "cancelled"
    assert record.error_code == "cancelled"


def test_background_task_references_are_held(monkeypatch):
    """asyncio holds only a weak reference to a task; a run whose task is
    collected mid-flight would stay 'running' forever."""
    import main
    monkeypatch.setattr(
        stubs, "scrape_page",
        lambda url: (time.sleep(0.3), {"url": url, "title": "t", "content": "x " * 50})[1],
    )
    with TestClient(main.app) as client:
        client.app.state.rate_limiter.reset()
        client.post("/runs", json=VALID)
        service = client.app.state.run_service
        assert len(service._tasks) >= 1
