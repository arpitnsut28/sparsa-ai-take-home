"""
Behaviour under the conditions a real deployment hits: partial failures,
timeouts, retries, concurrency, backpressure, cancellation and shutdown.
"""
import asyncio
import time

import pytest

import config
from services import stubs
from services.pipeline import (
    NoUsableSources,
    PageOutcome,
    build_context,
    deduplicate,
    extract_text,
    extract_usage,
    run_pipeline,
)
from tests.conftest import VALID, start_and_wait


# --- partial failure --------------------------------------------------------

def test_one_bad_source_degrades_the_run_instead_of_failing_it(client, monkeypatch):
    def flaky(url):
        if "bad" in url:
            raise stubs.ScrapeError("404 not found", kind="not_found", retryable=False)
        return {"url": url, "title": f"T {url}", "content": f"body of {url} " * 5}

    monkeypatch.setattr(stubs, "scrape_page", flaky)
    body = start_and_wait(
        client,
        {"prompt": "x", "urls": ["https://good.example/a", "https://bad.example/b",
                                 "https://good.example/c"]},
    )

    assert body["status"] == "done"
    result = body["result"]
    assert result["pages_scraped"] == 3
    assert result["pages_succeeded"] == 2
    assert result["pages_failed"] == 1
    assert result["success_rate"] == pytest.approx(2 / 3, abs=1e-4)
    assert result["failures"][0]["kind"] == "not_found"
    assert "https://bad.example/b" not in result["sources"]


def test_all_sources_failing_is_an_error_not_a_fabricated_brief(client, monkeypatch):
    """No sources means nothing to ground a brief in — and no LLM call to pay for."""
    calls = []
    monkeypatch.setattr(
        stubs, "scrape_page",
        lambda url: (_ for _ in ()).throw(stubs.ScrapeError("boom", kind="upstream_error")),
    )
    monkeypatch.setattr(stubs, "call_llm", lambda prompt: calls.append(prompt))

    body = start_and_wait(client, {"prompt": "x", "urls": ["https://a.example/1"]})
    assert body["status"] == "error"
    assert body["error_code"] == "all_sources_failed"
    assert body["result"] is None
    assert calls == [], "the LLM must not be called with zero sources"


def test_empty_body_counts_as_a_failed_scrape(client, monkeypatch):
    monkeypatch.setattr(
        stubs, "scrape_page",
        lambda url: {"url": url, "title": "t", "content": "   " if "empty" in url else "text " * 20},
    )
    body = start_and_wait(
        client, {"prompt": "x", "urls": ["https://ok.example/a", "https://empty.example/b"]}
    )
    assert body["result"]["pages_failed"] == 1
    assert body["result"]["failures"][0]["kind"] == "empty_content"


def test_scraper_crash_does_not_take_down_the_run(client, monkeypatch):
    def explode(url):
        if "boom" in url:
            raise ZeroDivisionError("a bug in the scraper")
        return {"url": url, "title": "t", "content": "text " * 20}

    monkeypatch.setattr(stubs, "scrape_page", explode)
    body = start_and_wait(
        client, {"prompt": "x", "urls": ["https://ok.example/a", "https://boom.example/b"]}
    )
    assert body["status"] == "done"
    assert body["result"]["failures"][0]["kind"] == "internal_error"


# --- timeouts and retries ---------------------------------------------------

def test_slow_page_times_out_rather_than_hanging(client, monkeypatch):
    config.settings.scrape_timeout_s = 0.05
    config.settings.scrape_max_attempts = 1

    def slow(url):
        if "slow" in url:
            time.sleep(5)
        return {"url": url, "title": "t", "content": "text " * 20}

    monkeypatch.setattr(stubs, "scrape_page", slow)
    started = time.time()
    body = start_and_wait(
        client, {"prompt": "x", "urls": ["https://ok.example/a", "https://slow.example/b"]},
        timeout_s=5,
    )
    assert time.time() - started < 4, "the run waited on the hung page"
    assert body["status"] == "done"
    assert body["result"]["failures"][0]["kind"] == "timeout"


def test_transient_failures_are_retried(client, monkeypatch):
    attempts = {"n": 0}

    def flaky(url):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise stubs.ScrapeError("503", kind="upstream_error", retryable=True)
        return {"url": url, "title": "t", "content": "text " * 20}

    monkeypatch.setattr(stubs, "scrape_page", flaky)
    body = start_and_wait(client, {"prompt": "x", "urls": ["https://a.example/1"]})
    assert body["status"] == "done"
    assert attempts["n"] == 3


def test_permanent_failures_are_not_retried(client, monkeypatch):
    attempts = {"n": 0}

    def not_found(url):
        attempts["n"] += 1
        raise stubs.ScrapeError("404", kind="not_found", retryable=False)

    monkeypatch.setattr(stubs, "scrape_page", not_found)
    start_and_wait(client, {"prompt": "x", "urls": ["https://a.example/1"]})
    assert attempts["n"] == 1, "a 404 must not be retried"


def test_llm_failure_is_reported_as_a_failed_run(client, monkeypatch):
    config.settings.llm_max_attempts = 2
    monkeypatch.setattr(
        stubs, "call_llm",
        lambda prompt: (_ for _ in ()).throw(stubs.LLMError("rate limited", kind="llm_error")),
    )
    body = start_and_wait(client, VALID)
    assert body["status"] == "error"
    assert body["error_code"] == "llm_error"
    assert "stack" not in (body["error"] or "").lower()
    assert "rate limited" not in body["error"], "upstream detail must not leak to the client"


def test_whole_run_deadline_is_enforced(client, monkeypatch):
    config.settings.run_timeout_s = 0.1
    config.settings.scrape_timeout_s = 10

    monkeypatch.setattr(
        stubs, "scrape_page",
        lambda url: (time.sleep(2), {"url": url, "title": "t", "content": "x" * 50})[1],
    )
    body = start_and_wait(client, {"prompt": "x", "urls": ["https://a.example/1"]}, timeout_s=5)
    assert body["status"] == "error"
    assert body["error_code"] == "run_timeout"


def test_a_run_never_gets_stuck_in_running(client, monkeypatch):
    """The original had no try/except: any exception left the run 'running'
    forever and the client polling forever."""
    monkeypatch.setattr(
        stubs, "call_llm", lambda prompt: (_ for _ in ()).throw(RuntimeError("unexpected"))
    )
    body = start_and_wait(client, VALID)
    assert body["status"] == "error"
    assert body["error_code"] == "internal_error"
    assert body["error"] == "An internal error occurred. Please try again."


# --- concurrency and backpressure -------------------------------------------

def test_concurrent_runs_stay_independent(client, monkeypatch):
    """The original's ``run_id = str(len(RUNS) + 1)`` handed two simultaneous
    requests the same id, and its ``run_log=[]`` default leaked rows between runs."""
    monkeypatch.setattr(
        stubs, "scrape_page",
        lambda url: {"url": url, "title": f"t {url}", "content": f"body {url} " * 10},
    )
    payloads = [
        {"prompt": f"prompt-{i}", "urls": [f"https://example.com/{i}"]} for i in range(6)
    ]
    ids = [client.post("/runs", json=p).json()["run_id"] for p in payloads]
    assert len(set(ids)) == 6

    deadline = time.time() + 15
    results = {}
    while time.time() < deadline and len(results) < 6:
        for i, run_id in enumerate(ids):
            if i in results:
                continue
            body = client.get(f"/runs/{run_id}").json()
            if body["status"] != "running":
                results[i] = body
        time.sleep(0.02)

    assert len(results) == 6
    for i, body in results.items():
        assert body["status"] == "done"
        assert body["result"]["prompt"] == f"prompt-{i}"
        assert body["result"]["sources"] == [f"https://example.com/{i}"]


def test_backpressure_sheds_load_with_429(client, monkeypatch):
    config.settings.max_concurrent_runs = 1
    config.settings.max_queued_runs = 1
    monkeypatch.setattr(
        stubs, "scrape_page",
        lambda url: (time.sleep(0.4), {"url": url, "title": "t", "content": "x " * 50})[1],
    )

    codes = [client.post("/runs", json=VALID).status_code for _ in range(6)]
    assert 429 in codes, f"expected load shedding, got {codes}"
    rejected = next(c for c in codes if c == 429)
    assert rejected == 429

    response = client.post("/runs", json=VALID)
    if response.status_code == 429:
        assert response.json()["error_code"] == "at_capacity"
        assert int(response.headers["Retry-After"]) >= 1


def test_ready_reports_not_ready_at_capacity(client, monkeypatch):
    config.settings.max_concurrent_runs = 1
    config.settings.max_queued_runs = 0
    monkeypatch.setattr(
        stubs, "scrape_page",
        lambda url: (time.sleep(0.5), {"url": url, "title": "t", "content": "x " * 50})[1],
    )
    client.post("/runs", json=VALID)
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"


def test_pages_are_scraped_concurrently(client, monkeypatch):
    """Serial scraping made an N-URL run take N x the per-page latency."""
    config.settings.page_concurrency = 5
    monkeypatch.setattr(
        stubs, "scrape_page",
        lambda url: (time.sleep(0.2), {"url": url, "title": f"t{url}", "content": f"b {url} " * 10})[1],
    )
    urls = [f"https://example.com/{i}" for i in range(5)]
    started = time.time()
    body = start_and_wait(client, {"prompt": "x", "urls": urls}, timeout_s=10)
    elapsed = time.time() - started

    assert body["status"] == "done"
    assert body["result"]["pages_scraped"] == 5
    assert elapsed < 0.8, f"5 pages took {elapsed:.2f}s — scraping looks serial"


# --- rate limiting ----------------------------------------------------------

def test_rate_limit_returns_429_with_retry_after(client):
    config.settings.rate_limit_enabled = True
    config.settings.rate_limit_burst = 2
    config.settings.rate_limit_per_minute = 1
    client.app.state.rate_limiter.reset()

    codes = [client.post("/runs", json=VALID).status_code for _ in range(4)]
    assert codes[:2] == [202, 202]
    assert 429 in codes[2:]

    response = client.post("/runs", json=VALID)
    assert response.status_code == 429
    assert response.json()["error_code"] == "rate_limited"
    assert int(response.headers["Retry-After"]) >= 1


# --- idempotency and cancellation -------------------------------------------

def test_idempotency_key_prevents_duplicate_work(client):
    headers = {"Idempotency-Key": "form-submit-abc"}
    first = client.post("/runs", json=VALID, headers=headers)
    second = client.post("/runs", json=VALID, headers=headers)

    assert first.status_code == 202
    assert second.status_code == 200, "a replay must not start a second run"
    assert first.json()["run_id"] == second.json()["run_id"]


def test_different_keys_start_different_runs(client):
    a = client.post("/runs", json=VALID, headers={"Idempotency-Key": "a"}).json()["run_id"]
    b = client.post("/runs", json=VALID, headers={"Idempotency-Key": "b"}).json()["run_id"]
    assert a != b


def test_cancel_stops_an_in_flight_run(client, monkeypatch):
    monkeypatch.setattr(
        stubs, "scrape_page",
        lambda url: (time.sleep(1.0), {"url": url, "title": "t", "content": "x " * 50})[1],
    )
    run_id = client.post("/runs", json=VALID).json()["run_id"]
    response = client.delete(f"/runs/{run_id}")

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert client.get(f"/runs/{run_id}").json()["error_code"] == "cancelled"


def test_cancelling_a_finished_run_is_409(client):
    body = start_and_wait(client)
    response = client.delete(f"/runs/{body['run_id']}")
    assert response.status_code == 409
    assert response.json()["error_code"] == "run_not_cancellable"


def test_cancelling_an_unknown_run_is_404(client):
    assert client.delete("/runs/nope").status_code == 404


# --- pipeline units ---------------------------------------------------------

def test_extract_text_handles_the_real_block_shape():
    class Block:
        def __init__(self, text, type="text"):
            self.text, self.type = text, type

    class Msg:
        content = [Block("first"), Block("ignored", "tool_use"), Block("second")]

    assert extract_text(Msg()) == "first\nsecond"


@pytest.mark.parametrize("content", [[], None, "already a string", 42])
def test_extract_text_survives_odd_shapes(content):
    class Msg:
        pass

    msg = Msg()
    msg.content = content
    assert isinstance(extract_text(msg), str)


def test_extract_usage_defaults_to_zero_when_missing():
    class Msg:
        pass

    assert extract_usage(Msg()) == (0, 0)


def test_deduplicate_matches_on_content_not_title():
    pages = [
        PageOutcome(url="https://a.example/x", ok=True, title="Canonical", content="same body"),
        PageOutcome(url="https://a.example/amp/x", ok=True, title="AMP", content="same  BODY"),
        PageOutcome(url="https://b.example/y", ok=True, title="Canonical", content="different"),
    ]
    unique = deduplicate(pages)
    assert [p.url for p in unique] == ["https://a.example/x", "https://b.example/y"]


def test_context_is_capped(monkeypatch):
    config.settings.max_chars_per_page = 50
    config.settings.max_context_chars = 400
    pages = [
        PageOutcome(url=f"https://e.example/{i}", ok=True, title="t", content="x" * 5000)
        for i in range(20)
    ]
    context = build_context(pages)
    assert len(context) <= 400 + 200


async def test_pipeline_raises_when_nothing_scrapes(monkeypatch):
    monkeypatch.setattr(
        stubs, "scrape_page",
        lambda url: (_ for _ in ()).throw(stubs.ScrapeError("down", kind="upstream_error")),
    )
    config.settings.scrape_max_attempts = 1
    with pytest.raises(NoUsableSources):
        await run_pipeline("x", ["https://a.example/1"], None)
