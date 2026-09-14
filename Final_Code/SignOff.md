# Production Sign-Off: Research Runs

**Date:** 14 September 2026 **Verdict:** Ship it, with two release conditions.

## Summary

I reviewed the Research Runs feature with a focus on real-world failure cases, concurrency, security and reliability. I fixed issues with stuck background tasks, concurrent runs, invalid run IDs, blocking operations, failed sources, token tracking, and ensuring scraped content is actually passed to the LLM. I also added timeouts, retries, input validation, SSRF protection, rate limiting, idempotency, structured logging, health/readiness checks, and improved the frontend error and polling handling.

I verified the changes with 118 automated tests covering concurrency, failures, timeouts, URL validation, cancellation, rate limiting, idempotency and frontend behaviour. I also tested the backend and frontend together to make sure the complete flow works correctly. Based on this testing, I'm comfortable signing off the feature as production-ready for the scope of this assignment.

## Key Change: Failure Visibility

One important change was making failures visible instead of allowing runs to remain stuck in a `"running"` state. Individual source failures are now isolated so one bad URL does not fail the entire run, while complete failures are reported clearly to the user. This also makes the KPIs more meaningful and gives enough information in the logs to investigate issues without exposing internal errors to the client.

## Release Conditions

Before a real production release, I would require two things:

1. Moving the in-memory run store to Redis/Postgres if runs need to survive restarts.
2. Using a shared rate limiter if the service is deployed across multiple instances.

Additionally, the live scraper integration should perform SSRF checks after DNS resolution and across redirects.

---

**Signed off.**
