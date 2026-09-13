# Research Runs

A user submits a research prompt and a list of source URLs. The backend scrapes
the pages, synthesises a brief with an LLM, and records the run's KPIs. Runs are
slow, so `POST /runs` returns a `run_id` immediately and the page polls
`GET /runs/{run_id}` until the run reaches a terminal state.

The scrape and LLM calls are stubbed — no API keys, no network needed.

---

## Run it

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --reload          # http://localhost:8000
```

Then **open <http://localhost:8000/>** — the API serves the page itself, so the
browser makes same-origin calls and CORS never enters the picture.

Opening `frontend.html` from disk also works (`Origin: null` is allowed outside
production), as does serving it separately; point it anywhere with
`?api=http://host:port`.

### Tests

```bash
cd backend
pip install -r requirements-dev.txt
pytest                             # 118 tests, ~1s
```

### See the failure paths

The stub always succeeds, which makes `success_rate` permanently 1.0. To watch
the service behave like a real deployment:

```bash
STUB_FAILURE_RATE=0.4 uvicorn main:app          # ~40% of sources fail
MAX_CONCURRENT_RUNS=1 MAX_QUEUED_RUNS=1 uvicorn main:app   # load shedding (429)
LOG_FORMAT=json uvicorn main:app                 # structured logs
```

---

## API

| Method | Path             | Purpose |
|--------|------------------|---------|
| `POST` | `/runs`          | Start a run → `202 {"run_id": "..."}`. Honours `Idempotency-Key`. |
| `GET`  | `/runs/{id}`     | Poll status and result. `404` if unknown or expired. |
| `GET`  | `/runs`          | Recent runs, newest first (`?limit=&offset=`). |
| `DELETE` | `/runs/{id}`   | Cancel an in-flight run. `409` once terminal. |
| `GET`  | `/health`        | Liveness. |
| `GET`  | `/ready`         | Readiness — `503` while draining or at capacity. |
| `GET`  | `/metrics`       | Counters and run-duration percentiles. |
| `GET`  | `/docs`          | OpenAPI UI. |

`GET /runs/{id}` returns the documented shape, plus additive fields:

```json
{
  "run_id": "…", "status": "running | done | error | cancelled",
  "result": {
    "timestamp": "…", "prompt": "…", "pages_scraped": 3, "success_rate": 0.667,
    "tokens_used": 1480, "sources": ["https://…"], "brief": "…",

    "pages_succeeded": 2, "pages_failed": 1, "duplicates_dropped": 0,
    "input_tokens": 1200, "output_tokens": 280, "duration_ms": 411,
    "failures": [{"url": "…", "kind": "timeout", "error": "…"}]
  },
  "error": null, "error_code": null,
  "created_at": "…", "finished_at": "…"
}
```

Errors share one envelope: `{error, error_code, detail, request_id}`.

---

## Layout

```
backend/
  main.py                 app wiring: lifespan, middleware, error envelopes, CORS
  config.py               every tunable, read from the environment
  models.py               request/response schemas and validation
  observability.py        structured logging, request correlation, counters
  api/routes.py           HTTP surface only
  services/
    run_service.py        run lifecycle: admission, execution, cancel, drain
    pipeline.py           scrape → dedupe → synthesise → KPI row
    scraper/llm stubs     stubs.py — the only module that touches externals
    urls.py               URL canonicalisation + SSRF guard
    store.py              bounded, TTL'd run store
    ratelimit.py          per-client token bucket
    retry.py              timeout + bounded retry with jittered backoff
  tests/                  118 tests
frontend.html             the page — vanilla, no build step
SIGNOFF.md                is it production-ready, what changed, what I'd block on
DECISION_LOG.md           one line per meaningful AI interaction
```

## Configuration

Everything below is an environment variable with a safe default; see
`config.py`.

| Variable | Default | Notes |
|----------|---------|-------|
| `ENV` | `development` | `production` requires `ALLOWED_ORIGINS` and refuses to start without it |
| `ALLOWED_ORIGINS` | dev origins | Comma-separated CORS allow-list |
| `ALLOW_PRIVATE_NETWORK_URLS` | `false` | Off = SSRF guard on |
| `MAX_URLS_PER_RUN` / `MAX_PROMPT_CHARS` / `MAX_BODY_BYTES` | `20` / `2000` / `65536` | Request limits |
| `MAX_CONCURRENT_RUNS` / `MAX_QUEUED_RUNS` | `4` / `16` | Admission control; beyond this, `429` |
| `PAGE_CONCURRENCY` / `WORKER_THREADS` | `5` / `16` | Per-run scrape fan-out |
| `SCRAPE_TIMEOUT_S` / `LLM_TIMEOUT_S` / `RUN_TIMEOUT_S` | `10` / `60` / `180` | Deadlines |
| `SCRAPE_MAX_ATTEMPTS` / `LLM_MAX_ATTEMPTS` | `3` / `3` | Retries (transient failures only) |
| `RUN_TTL_S` / `MAX_STORED_RUNS` | `3600` / `1000` | Run store bounds |
| `RATE_LIMIT_PER_MINUTE` / `RATE_LIMIT_BURST` | `30` / `10` | Per client IP |
| `LOG_LEVEL` / `LOG_FORMAT` | `INFO` / `text` | `json` for shipping |
| `STUB_FAILURE_RATE` / `STUB_LATENCY_MS` | `0.0` / `200` | Stub-only; deleted with `stubs.py` |
