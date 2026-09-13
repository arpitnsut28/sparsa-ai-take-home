"""The run store: TTL, capacity, and the idempotency map."""
import time

import config
from services.store import RunRecord, RunStore


def _record(run_id: str, status: str = "done", age_s: float = 0.0) -> RunRecord:
    return RunRecord(run_id=run_id, status=status, created_at=time.time() - age_s)


def test_get_returns_none_for_unknown():
    assert RunStore().get("nope") is None


def test_terminal_runs_expire_after_the_ttl():
    config.settings.run_ttl_s = 10
    store = RunStore()
    store.put(_record("old", age_s=60))
    store.put(_record("new", age_s=1))

    assert store.get("old") is None, "an expired run must not be served"
    assert store.get("new") is not None


def test_in_flight_runs_are_never_expired():
    """Evicting a live run would 404 a client that is still legitimately polling."""
    config.settings.run_ttl_s = 1
    store = RunStore()
    store.put(_record("live", status="running", age_s=3600))
    assert store.get("live") is not None
    assert store.sweep() == 0


def test_sweep_removes_expired_runs():
    config.settings.run_ttl_s = 10
    store = RunStore()
    for i in range(5):
        store.put(_record(f"old{i}", age_s=100))
    store.put(_record("fresh"))

    assert store.sweep() == 5
    assert len(store) == 1
    assert store.evicted_total == 5


def test_capacity_cap_drops_the_oldest_terminal_runs():
    """``RUNS = {}`` grew without bound — a slow memory leak in a long-lived process."""
    config.settings.max_stored_runs = 10
    config.settings.run_ttl_s = 10_000
    store = RunStore()
    for i in range(50):
        store.put(_record(f"run{i}"))

    assert len(store) == 10
    assert store.get("run0") is None, "oldest should have been evicted"
    assert store.get("run49") is not None


def test_capacity_cap_keeps_in_flight_runs():
    config.settings.max_stored_runs = 5
    store = RunStore()
    store.put(_record("live", status="running"))
    for i in range(20):
        store.put(_record(f"done{i}"))

    assert store.get("live") is not None, "a live run must survive capacity pressure"


def test_list_is_newest_first_and_paginates():
    store = RunStore()
    for i in range(10):
        store.put(_record(f"run{i}"))

    page, total = store.list(limit=3, offset=0)
    assert total == 10
    assert [r.run_id for r in page] == ["run9", "run8", "run7"]

    page2, _ = store.list(limit=3, offset=3)
    assert [r.run_id for r in page2] == ["run6", "run5", "run4"]


def test_idempotency_keys_expire():
    config.settings.idempotency_ttl_s = 0.05
    store = RunStore()
    store.remember_key("k", "run-1")
    assert store.lookup_key("k") == "run-1"
    time.sleep(0.08)
    assert store.lookup_key("k") is None


def test_active_count_tracks_unfinished_runs():
    store = RunStore()
    store.put(_record("a", status="running"))
    store.put(_record("b", status="done"))
    store.put(_record("c", status="error"))
    assert store.active_count() == 1
