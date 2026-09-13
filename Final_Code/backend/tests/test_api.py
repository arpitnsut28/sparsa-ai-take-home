"""The HTTP contract: happy path, validation, 404s, and the error envelope."""
import pytest

from tests.conftest import VALID, start_and_wait


def test_health_and_ready(client):
    assert client.get("/health").json() == {"status": "ok"}
    ready = client.get("/ready")
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"


def test_start_run_returns_202_and_a_run_id(client):
    response = client.post("/runs", json=VALID)
    assert response.status_code == 202
    assert response.json()["run_id"]
    assert response.headers["Location"].startswith("/runs/")


def test_run_ids_are_not_guessable(client):
    """The original used str(len(RUNS) + 1): sequential, and racy under load."""
    ids = {client.post("/runs", json=VALID).json()["run_id"] for _ in range(5)}
    assert len(ids) == 5
    assert not any(i.isdigit() and len(i) < 8 for i in ids)


def test_full_run_matches_the_documented_shape(client):
    body = start_and_wait(client)
    assert body["status"] == "done"

    result = body["result"]
    for key in ("timestamp", "prompt", "pages_scraped", "success_rate",
                "tokens_used", "sources", "brief"):
        assert key in result, key

    assert result["pages_scraped"] == 2
    assert result["success_rate"] == 1.0
    assert result["prompt"] == VALID["prompt"]
    assert sorted(result["sources"]) == sorted(VALID["urls"])


def test_brief_is_a_string_not_a_list_of_blocks(client):
    """Regression: ``brief = msg.content`` returned the block list itself,
    which the page rendered as [object Object]."""
    result = start_and_wait(client)["result"]
    assert isinstance(result["brief"], str)
    assert "Key finding A" in result["brief"]


def test_tokens_used_counts_prompt_and_completion(client):
    """The original reported output tokens only, hiding most of the spend."""
    result = start_and_wait(client)["result"]
    assert result["input_tokens"] == 1200
    assert result["output_tokens"] == 280
    assert result["tokens_used"] == 1480


def test_unknown_run_id_is_404_not_500(client):
    """The original raised KeyError, which surfaced as an unhandled 500."""
    response = client.get("/runs/does-not-exist")
    assert response.status_code == 404
    body = response.json()
    assert body["error_code"] == "run_not_found"
    assert body["request_id"]


@pytest.mark.parametrize(
    "payload,reason",
    [
        ({"prompt": "", "urls": ["https://example.com"]}, "blank prompt"),
        ({"prompt": "   ", "urls": ["https://example.com"]}, "whitespace prompt"),
        ({"prompt": "x", "urls": []}, "no urls"),
        ({"prompt": "x", "urls": [""]}, "only blank urls"),
        ({"prompt": "x", "urls": ["not-a-url"]}, "unparseable url"),
        ({"prompt": "x", "urls": ["file:///etc/passwd"]}, "non-http scheme"),
        ({"prompt": "x", "urls": ["http://169.254.169.254/"]}, "metadata endpoint"),
        ({"prompt": "x"}, "missing urls"),
        ({"urls": ["https://example.com"]}, "missing prompt"),
        ({"prompt": "x", "urls": "https://example.com"}, "urls not a list"),
        ({"prompt": ["x"], "urls": ["https://example.com"]}, "prompt not a string"),
        ({"prompt": "x", "urls": ["https://example.com"], "admin": True}, "unknown field"),
    ],
)
def test_invalid_payloads_are_422(client, payload, reason):
    response = client.post("/runs", json=payload)
    assert response.status_code == 422, f"{reason}: {response.text}"
    assert response.json()["error_code"] == "validation_error"


def test_malformed_json_is_422_with_the_standard_envelope(client):
    response = client.post(
        "/runs", content="{not json", headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422
    body = response.json()
    assert set(body) >= {"error", "error_code", "request_id"}


def test_oversized_prompt_rejected(client):
    import config
    response = client.post(
        "/runs", json={"prompt": "x" * (config.settings.max_prompt_chars + 1),
                       "urls": ["https://example.com"]}
    )
    assert response.status_code == 422


def test_too_many_urls_rejected(client):
    import config
    urls = [f"https://example.com/{i}" for i in range(config.settings.max_urls_per_run + 1)]
    response = client.post("/runs", json={"prompt": "x", "urls": urls})
    assert response.status_code == 422


def test_oversized_body_is_413(client):
    import config
    config.settings.max_body_bytes = 500
    response = client.post(
        "/runs", json={"prompt": "x" * 5000, "urls": ["https://example.com"]}
    )
    assert response.status_code == 413
    assert response.json()["error_code"] == "payload_too_large"


def test_blank_url_lines_are_tolerated(client):
    """A trailing newline in the textarea must not be a validation error."""
    body = start_and_wait(
        client, {"prompt": "x", "urls": ["https://example.com/a", "", "  ", "\n"]}
    )
    assert body["status"] == "done"
    assert body["result"]["pages_scraped"] == 1


def test_duplicate_urls_are_scraped_once(client):
    body = start_and_wait(
        client,
        {"prompt": "x", "urls": ["https://example.com/a", "https://EXAMPLE.com/a",
                                 "https://example.com/a#top"]},
    )
    assert body["result"]["pages_scraped"] == 1


def test_every_response_carries_a_request_id(client):
    assert client.get("/health").headers["X-Request-ID"]
    assert client.get("/runs/nope").headers["X-Request-ID"]


def test_client_request_id_is_echoed_back(client):
    response = client.get("/health", headers={"X-Request-ID": "trace-me-123"})
    assert response.headers["X-Request-ID"] == "trace-me-123"


def test_run_list_shows_history(client):
    start_and_wait(client)
    start_and_wait(client)
    body = client.get("/runs?limit=10").json()
    assert body["total"] >= 2
    assert body["runs"][0]["created_at"] >= body["runs"][-1]["created_at"]
    assert {"run_id", "status", "prompt", "success_rate"} <= set(body["runs"][0])


def test_run_list_rejects_absurd_pagination(client):
    assert client.get("/runs?limit=0").status_code == 422
    assert client.get("/runs?limit=1000").status_code == 422
    assert client.get("/runs?offset=-1").status_code == 422


def test_metrics_endpoint_reports_counters(client):
    start_and_wait(client)
    body = client.get("/metrics").json()
    assert body["counters"]["runs_started"] >= 1
    assert body["counters"]["runs_done"] >= 1
    assert body["counters"]["tokens_used"] >= 1480
    assert body["run_duration_s"]["count"] >= 1


def test_terminal_runs_are_cacheable_and_running_ones_are_not(client, monkeypatch):
    """A finished run never changes, so it is cacheable; a live one must not be."""
    import time as _time

    from services import stubs

    monkeypatch.setattr(
        stubs, "scrape_page",
        lambda url: (_time.sleep(0.3), {"url": url, "title": "t", "content": "x " * 50})[1],
    )
    run_id = client.post("/runs", json=VALID).json()["run_id"]

    running = client.get(f"/runs/{run_id}")
    assert running.json()["status"] == "running"
    assert running.headers["Cache-Control"] == "no-store"
    assert running.headers["Retry-After"] == "1"

    for _ in range(200):
        response = client.get(f"/runs/{run_id}")
        if response.json()["status"] != "running":
            assert "max-age" in response.headers["Cache-Control"]
            return
        _time.sleep(0.02)
    raise AssertionError("run never finished")
