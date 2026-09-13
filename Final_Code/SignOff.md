# Production Sign-Off — Research Runs

**Feature:** Research Runs (backend-focused)

**Date:** 12 September 2026

**Verdict:** **Ship it** — with two release conditions, both mentioned near the end.

---

## The short version

When I first looked at the service, it was basically working for one very specific situation.

A short prompt. Two reachable URLs. One request at a time. And, most importantly, nothing goes wrong.

That was the happy path.

The moment I started pushing it outside that little box, things got interesting.

And not in a good way.

A failed run could just sit at `"running"` forever while the browser kept polling it. An unknown `run_id` gave a 500. Two requests arriving together could get the same ID, which meant one run could overwrite the other.

And then there was the big one.

`POST /runs` would happily fetch `http://169.254.169.254/` if someone asked it to.

So I stopped looking at the happy path and started looking at what happens when things go wrong. That became the main approach for the rewrite.

I rebuilt the backend around those failure cases and did a lighter pass over the frontend.

The result is much closer to something I’d actually be comfortable shipping. The feature now does what it says, the user can see what is happening while a run is in progress, and when something upstream fails, the run fails properly instead of just hanging forever.

There are now **118 tests** covering the behaviour, including regression tests for the issues I found.

But there was one thing that stood out more than all the individual bugs.

The original system never actually passed the scraped pages to the LLM.

That means `call_llm(prompt)` only received the user's prompt. The pages that were supposedly being researched were never included.

So the system was showing a "brief synthesised from your sources", while the model had never actually seen those sources.

That one bothered me the most.

The code was technically doing what it was written to do. But the feature itself wasn't really doing the thing it existed to do.

---

## What changed, and how I got there

### 1. Correctness — getting the run to finish properly

The first pass was mostly about making sure one run actually stays one run.

The biggest issue was the LLM context.

`call_llm(prompt)` never received the scraped pages. So I changed that. The pages are now assembled into a bounded context and passed to the model.

There was another small-looking issue which turned into a very obvious frontend problem.

`brief = msg.content` was assigning the whole block list returned by the API. The API gave something like `[{…}]`, while the page expected text.

So the browser ended up rendering:

`[object Object]`

Not exactly the brief we were going for.

I added `extract_text()` so the text blocks are joined together and non-text blocks are ignored.

Then I found the mutable default:

`do_run(…, run_log=[])`

That list was being shared between calls. Which means history from one run could leak into another run and just keep growing.

Not great.

The fix was simple: remove the mutable default and keep the history in the run store instead.

Run IDs had another problem.

The original code used:

`run_id = str(len(RUNS) + 1)`

At first glance that looks harmless. But with two requests arriving at the same time, both could calculate the same ID. One run could overwrite the other.

Also, because the IDs were predictable, someone could just enumerate them and potentially read another user's run.

That became `uuid4().hex`.

Unknown runs had a simpler problem.

`RUNS[run_id]` raised a `KeyError`, which eventually became an unhandled 500.

Now it returns a proper `404` with `run_not_found`.

Then came one of the more serious reliability issues.

There was no `try`/`except` around `process_run`.

So if basically anything unexpected happened, the run could stay `"running"` forever.

From the browser's point of view, it looked like the system was still working.

It wasn't.

Now every exception path ends in a terminal state, with a safe message for the caller and a stable `error_code`. The internal details still go into the logs where they belong.

There was also a `time.sleep()` being reached from an `async def`.

That is the kind of thing which looks fine until you have actual traffic.

A 20-URL run could block the entire event loop for around four seconds. So it wasn't just that one run becoming slow. Other requests could freeze too.

Blocking work now runs in a dedicated thread pool.

The `success_rate` calculation had a classic edge case as well.

It divided by `len(pages)` without checking whether the list was empty. An empty list meant `ZeroDivisionError`.

That is now guarded, and the input is validated earlier too.

Token reporting was also misleading.

The old code reported:

`tokens_used = output_tokens`

So a run which actually used 1480 tokens could report only 280.

Now `tokens_used` includes both prompt and completion tokens, while `input_tokens` and `output_tokens` are exposed separately.

The de-duplication logic also needed changing.

It was based on the page title. But the same document can exist at multiple URLs. Canonical URL, AMP URL, tracking parameters, all that stuff.

So title wasn't really identity.

It's now based on a content fingerprint.

And finally, there was a task-lifetime problem.

`asyncio.create_task(...)` was called without keeping a reference to the task. asyncio only holds a weak reference in that situation, so a task could be collected and leave the run sitting at `"running"` forever.

Task references are now held for the lifetime of the task.

Small things individually. Together, they made a pretty big difference.

---

## 2. Resilience — one bad URL shouldn't kill everything

The old `success_rate` looked useful, but it was mostly decorative.

The stub couldn't really fail, so it always ended up being `1.0`.

Production won't behave like that.

Some URLs will 404. Some will timeout. Some will block robots. Some will just return an empty body.

So the run needs to expect failure.

That is what changed here.

**Per-URL isolation** comes first.

If one page fails, that failure gets recorded and the rest of the sources continue processing.

The `success_rate` is now an actual number, and `failures[]` tells us which sources failed and what happened.

Then there are timeouts.

There is now a timeout for individual scrapes, one for LLM calls, and also a deadline for the whole run.

Nothing should be allowed to hang indefinitely.

If an upstream service gets stuck, the run becomes failed instead of becoming a permanent `"running"` job.

Retries are more selective too.

Transient failures get retried with exponential backoff and full jitter.

A 404 doesn't get three more chances.

A 503 does.

That distinction matters.

Scraping is also concurrent now, but it isn't unlimited. A semaphore caps the concurrency so one run can't open an unbounded number of connections.

And there is a fairly important rule now:

If every source fails, don't call the LLM.

There is nothing useful to give it.

Calling the model anyway would just be paying for a potentially hallucinated answer over no usable sources.

So the run returns `all_sources_failed`.

---

## 3. Security — this was where things got serious

The original endpoint was allowing the server to fetch URLs supplied by whoever called it.

That means we have to assume those URLs are hostile.

The original implementation accepted things like:

`file:///etc/passwd`

and:

`http://169.254.169.254/`

The second one is especially nasty because that address is the cloud metadata endpoint and can potentially expose instance credentials.

Private network addresses were also accepted.

So I added one audited URL-validation module.

It checks the scheme, rejects embedded credentials and control characters, and blocks loopback, private, link-local, reserved and multicast addresses.

That includes some less obvious cases too.

IPv4-mapped IPv6 addresses such as:

`::ffff:10.0.0.1`

And legacy `inet_aton` formats such as:

`127.1`

`0177.0.0.1`

`0x7f000001`

`2130706433`

These are interesting because Python's `ipaddress` module considers some of them invalid, while HTTP clients may still resolve them.

So just trusting `ipaddress` wasn't enough.

CORS had a problem too.

It was:

`allow_origins=["*"]`

Which basically means any website could potentially drive this API from a visitor's browser.

That's not something I wanted to leave in production.

It is now an explicit allow-list, and when `ENV=production`, the application refuses to start unless that list is configured.

I also added input limits.

Prompt length. URL count. URL length. Body size.

The goal is simple: a ridiculous request should get a `422`, not consume a worker.

Then there was XSS.

The page was using `innerHTML` with model output and user-supplied URLs.

That is a bad combination.

The frontend now builds output as text nodes instead. Links are only constructed from `http(s)` URLs.

Internal errors are handled differently too.

The client doesn't get stack traces or upstream implementation details.

It gets a stable `error_code` and a safe message.

The actual details go to the logs, correlated by ID.

---

## 4. Observability — if something breaks, I want to know about it

The original system basically had no useful logging.

If a run failed, there wasn't much of a trail left behind.

That makes debugging production issues unnecessarily painful.

So this part was built around answering a simple question:

**What actually happened to this run?**

Logging is now structured, with `LOG_FORMAT=json`.

Every line gets a request ID, and that ID follows the background task as well. So the logs for a run can be connected back to the request which started it.

Clients can also provide `X-Request-ID`, and the server echoes it back.

There are now `/health` and `/ready` endpoints.

`/health` handles liveness.

`/ready` is more about whether the instance should actually receive traffic. It returns `503` while the service is draining or at capacity.

That matters during rolling deploys.

There is also `/metrics`.

It tracks runs started, completed, failed and cancelled, pages scraped and failed, tokens consumed, rejection reasons and run-duration percentiles.

And `GET /runs` now restores the run history that the original implementation created in a local variable and then basically threw away.

---

## 5. Capacity and lifecycle — because traffic doesn't politely arrive one request at a time

One thing I kept coming back to was what happens when more users show up.

The old code created an `asyncio.create_task` for every request without a real bound.

That works until it doesn't.

Enough traffic could mean too many threads, too much memory, and no clear signal that the system was running out of room.

Runs are now bounded.

There is a limit on both in-flight and queued work.

Once that limit is reached, the API sheds load with `429` and includes `Retry-After`.

The endpoint that actually costs money, `POST /runs`, also has rate limiting per client IP.

Then there is idempotency.

If a user double-clicks the form or a client retries a POST, we don't want to buy the same run twice.

`Idempotency-Key` now makes the second request return the original run instead.

The run store itself needed attention too.

The original was just:

`RUNS = {}`

Nothing was ever evicted.

So technically, over enough time, that becomes a memory leak.

A slow one. But still a leak.

It now has both a TTL and a cap. In-flight runs are protected from eviction so a polling client doesn't suddenly lose the run it is watching.

Shutdown behaviour was another gap.

If the process went down during a run, the client could be left polling for something the process had forgotten.

Now in-flight runs are cancelled and recorded as `cancelled`.

There is also:

`DELETE /runs/{id}`

So a user can stop a run they started.

---

## 6. The page — I didn't rebuild it, but I did fix the parts that hurt

I kept this pass deliberately light.

It's vanilla HTML/JS. There was no reason to introduce a build system just for the sake of it.

But there were a few things that needed fixing.

The first was polling.

`setInterval` was never cleared.

So after a run finished, the browser could keep polling forever.

And every new run added another timer, with all of them writing to the same element.

That is the kind of bug which probably doesn't show up in a quick demo.

It does show up later.

Polling now uses `setTimeout`, stops when the run reaches a terminal state, and starting a new run cancels the previous timer and in-flight request.

The frontend `fetch` also had no `try`/`catch` and no timeout.

So if the network disappeared, the page just sat there.

Now network failures are surfaced. Temporary failures are retried with backoff, and if the outage continues, the UI actually says so.

The URL box had another tiny issue.

Blank lines were submitted as empty strings.

URLs are now trimmed and de-duplicated client-side, while the server also tolerates them.

There was no double-click guard either.

No proper validation.

And anything other than `"done"` was basically ignored, meaning an errored run could keep spinning forever.

Those are all fixed now.

`success_rate` is also rendered as a proper percentage rather than:

`0.6666666666666666`

And partial failures are shown to the user, including which sources failed and why.

---

## The response shape

The documented response contract is still valid.

Most of the changes are additive.

There is one deliberate exception.

`tokens_used` now counts prompt + completion tokens.

So it is **1480**, rather than just the **280** completion tokens.

The stub defines:

`input_tokens = 1200`

and:

`output_tokens = 280`

The original implementation only reported the second number.

Which means the KPI called "tokens used" was hiding roughly 80% of what the run actually consumed.

That's a problem if anyone is using that number to understand cost.

I considered the misleading KPI worse than changing the number.

So I changed it.

At the same time, `input_tokens` and `output_tokens` are kept as separate fields, so no information is lost.

There is one thing I'd flag here.

If some downstream consumer already relies on the old `tokens_used` behaviour, this is the one line I'd revert:

`services/pipeline.py`

Specifically the `tokens_used` key.

---

## How I verified all this

At this point I didn't want to rely on "it seems to work."

So I ran **118 automated tests**.

All passing.

This was on a clean install with Python 3.14, FastAPI 0.141 and Pydantic 2.13.

Every major bug above has a regression test behind it.

Some of the more interesting ones:

- A partial failure produces `success_rate == 2/3` and identifies the failed source.
- If all sources fail, the run returns `all_sources_failed` and the LLM is confirmed to never have been called.
- A hung page times out without holding the entire run hostage.
- Transient failures retry, while a 404 does not.
- If a scraper raises `ZeroDivisionError`, only that page degrades instead of taking down the whole run.
- Six concurrent runs remain independent, with separate IDs, prompts and sources.
- Backpressure returns `429`, and `/ready` changes to `503`.
- Five pages scrape in substantially less time than they would serially.
- Shutdown marks an in-flight run as `cancelled` rather than silently dropping it.
- Terminal runs expire, while in-flight runs are protected from eviction.
- There are 26 URL-rejection vectors tested — 16 private-network targets and 10 malformed ones.
- Digit-leading domains such as `1and1.com` are still accepted as normal hostnames.

I also tested the thing against an actual running server.

Not just the test client.

The full response contract. Error envelope. Fourteen edge-case requests including `404`, `422`, `413` and `429`. Idempotent replay. Cancellation. Load shedding with `Retry-After`. `/ready` switching to `503`. JSON logging. Graceful shutdown.

All of it.

Then I went one step further and opened the actual page in a real browser using headless Chrome.

A full run from start to finish.

Partial-failure rendering.

Client-side validation.

Backend going down.

Server-side errors.

Polling stopping when the run finishes.

Double-start prevention.

And then I tried to break the renderer.

I gave it a hostile payload.

An `<img onerror>`.

A `<script>` inside the brief.

Markup inside the failure list.

And a `javascript:` URL inside `sources`.

Nothing executed.

That test actually found the last real issue.

The page was still willing to construct an anchor from a `javascript:` href.

The server already rejects those URLs when they come in, but the frontend shouldn't blindly trust that.

So that got fixed too.

---

## Is it production-ready?

**Yes.**

At least against the bar defined in the brief.

The backend now handles the failure cases a real deployment is going to hit. The system is observable while it runs, and the page doesn't just fall apart when something upstream decides to misbehave.

But I would still want two things settled before sending real traffic through it.

These aren't really code defects.

They're product/infrastructure decisions.

### 1. What happens to runs when the process restarts?

Runs currently live in memory.

So a restart means they're gone.

In-flight runs are cancelled cleanly and recorded, but if a client is polling across a deploy, the new process won't know about that run and will return `404`.

For a dashboard where losing a run just means clicking Run again, that's probably survivable.

If a run is ever going to be billed, audited, or referenced later, though, memory isn't enough.

That needs Redis or Postgres.

The good part is that `services/store.py` already has a small interface, so either backend can fit behind it without changing the rest of the system too much.

**My recommendation:** ship with the in-memory store for now, but add persistence before the first paying customer.

### 2. Rate limiting is currently per-process

This one is easy to miss.

If there are N replicas, the effective rate limit becomes roughly N times the configured limit.

So the current rate limiter is a real limit on what one instance can spend.

It just isn't the global ceiling that the name might suggest.

The actual global limit belongs at the gateway or behind a shared counter.

---

There is one more thing I'd mention to whoever wires up the real integrations.

The SSRF protection here validates the URL.

That's useful, but it can't completely protect against DNS rebinding.

A public hostname could resolve to something like `10.0.0.1` at connection time.

That check needs to happen inside the HTTP client.

The client should resolve the hostname first, validate the resolved address, pin that address for the connection, and then re-check every redirect hop.

`services/stubs.py` is the only module expected to change for that integration, and the comment marking that seam is already there.

---

Beyond all of that, the stubs are still stubs.

The interfaces match what production should look like. The failure handling around them is real. The tests are real.

But the first run against live Firecrawl and a live model will almost certainly find things the stubs can't.

That's normal.

You can't fake every weird timeout, malformed response, rate limit or upstream behaviour.

That's also why the fault-injection knob and metrics endpoint are there.

So, after going through the whole thing, I'm comfortable signing off.

Not because nothing can go wrong.

Things will go wrong.

That's production.

The difference now is that when they do, the system should fail in a way we can see, understand and recover from.

**Signed off.**
