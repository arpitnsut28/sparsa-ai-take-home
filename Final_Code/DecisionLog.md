# Decision Log

One line for every AI interaction that actually changed the direction of the work.

The point here isn't to reproduce the conversation with the AI. It's to show how I used it, what I took from it, what I didn't, and where I still had to make the engineering call myself.


| #   | What I asked the AI                                                                                                                                                | What it gave back                                                                                                                                                                                                                                                                                                                               | Accepted / rejected / modified and why                                                                                                                                                                                                                                                                                                                                                                                   |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 1   | Read `backend_main.py` and `front.html` and tell me every way this could break in production correctness, concurrency, security, ops and give each one a severity. | Around 30 findings across the two files: mutable default arguments, `msg.content` being assigned as a list, `len(RUNS)+1` IDs, `KeyError` on unknown IDs, `time.sleep` blocking the event loop, no proper error path, `allow_origins=["*"]`, missing validation, `setInterval` never being cleared, `innerHTML` creating an XSS risk, and more. | **Accepted after checking each finding against the code.** They were real issues. I did cut a few stylistic suggestions because they weren't actual production defects.                                                                                                                                                                                                                                                  |
| 2   | What's wrong with this feature *as a feature*, ignoring code quality for a moment?                                                                                 | It pointed out something much bigger: `call_llm(prompt)` never receives the scraped pages. So the brief is generated without the sources, and then displayed above a citation list as if the model had read them.                                                                                                                               | **Accepted and this changed how I approached the whole task.** This became the most important finding. It isn't really a normal "bug"; the code is doing what it was written to do. The problem is that the feature isn't actually doing what the user expects it to do. I moved this to the top of the sign-off.                                                                                                        |
| 3   | The stub can't fail, so `success_rate` is always `1.0`. How can I make the failure paths real without putting fake behaviour into production code?                 | Three options came back: monkeypatch only in tests, add a config-gated failure rate to the stub, or create a separate fake scraper module.                                                                                                                                                                                                      | **Modified.** I took the config-gated approach, but kept it quarantined inside `services/stubs.py`. There's a comment making it clear that it goes away with the stub, and the default is `0.0`. Tests use monkeypatching instead, so test results don't depend on randomness.                                                                                                                                           |
| 4   | Design the SSRF guard for user-supplied URLs. What exactly should it reject?                                                                                       | Scheme allow-listing, rejecting embedded credentials, and blocking loopback/private/link-local/reserved addresses using the `ipaddress` module. It also called out the `169.254.169.254` metadata endpoint.                                                                                                                                     | **Accepted, then I found a hole.** One of my own tests for `http://127.1/` failed. `ipaddress` treats the shorthand as invalid, which meant it could fall through the "this is a hostname" path. That's a bypass. I added `inet_aton`-style normalization for legacy forms like `127.1`, `0177.0.0.1`, `0x7f000001`, and `2130706433`. I also added a test making sure `1and1.com` is still accepted as a normal domain. |
| 5   | Should `tokens_used` stay as `output_tokens` to preserve the documented response shape, or should it become input + output?                                        | The recommendation was to report the total, since that's what the run actually costs. The example value in the brief was the main argument for being careful about changing it.                                                                                                                                                                 | **Accepted, but with a hedge.** I changed it to the total, kept `input_tokens` and `output_tokens` as separate fields, and called it out in the sign-off as the one deliberate contract change, including the exact line to revert. Reporting 280 of 1480 tokens as "tokens used" is a KPI that gives the wrong picture of cost.                                                                                         |
| 6   | What should happen when every source fails to scrape?                                                                                                              | The suggestion was to return `done` with `success_rate: 0.0` and an empty brief.                                                                                                                                                                                                                                                                | **Rejected.** That would still call the model. We'd be paying for an LLM request with no grounding, which could produce a confident-sounding brief over nothing. I made it a failed run with `all_sources_failed` instead, and added a test confirming that the LLM is never called in this path.                                                                                                                        |
| 7   | Design the run orchestrator: admission control, cancellation, whole-run deadline, and graceful draining.                                                           | It proposed a `RunService` using a semaphore, a task set, `wait_for`, and application lifespan hooks.                                                                                                                                                                                                                                           | **Accepted, with one correction.** The first version allowed `cancel()` to return before the task had actually transitioned state, so `DELETE` could report `running` immediately after the user had cancelled it. I changed the record to close out synchronously; the task handler also writes the same state idempotently.                                                                                            |
| 8   | Is holding task references actually necessary, or am I just doing cargo-cult async code?                                                                           | It confirmed that it matters: asyncio keeps only a weak reference to tasks, so a task that nothing else refers to can potentially be collected while it's still awaiting.                                                                                                                                                                       | **Accepted.** I kept the task set and added a test checking that it contains the task while a run is active. This one is easy to dismiss until the alternative is a run that just hangs forever.                                                                                                                                                                                                                         |
| 9   | The page needs to work when opened from `file://` as well as when it is served. How do I handle CORS without using `*`?                                            | The suggestion was to add `"null"` to the CORS allow-list for the `file://` origin.                                                                                                                                                                                                                                                             | **Modified.** `null` is only allowed outside `ENV=production`. Production refuses to start without an explicit `ALLOWED_ORIGINS`. I also made the API serve the page itself at `/`, so the normal path is same-origin and doesn't need CORS at all.                                                                                                                                                                      |
| 10  | Write the frontend polling loop so it can't leak timers or accidentally start two runs.                                                                            | It suggested recursive `setTimeout`, a session object, `AbortController`, backoff when requests fail, and stopping once a terminal status is reached.                                                                                                                                                                                           | **Accepted.** I verified it in headless Chrome by counting network responses. Polling stops when the run finishes, and starting another run doesn't leave the previous timer running behind it.                                                                                                                                                                                                                          |
| 11  | Attack my own renderer with a hostile brief, hostile source URLs, and markup in the failure list.                                                                  | It suggested a Chrome probe which intercepts the polling response and injects `<img onerror>`, `<script>`, and a `javascript:` URL.                                                                                                                                                                                                             | **Accepted — and it actually found something.** Nothing executed from the brief because it is rendered through text nodes. But the page was still happily setting `a.href` to `javascript:...`. The server rejects those URLs, but the frontend shouldn't assume the server always protects it. Now only `http(s)` URLs are turned into links.                                                                           |
| 12  | Review the whole thing like a staff engineer who will actually have to be on call for it. What's still missing?                                                    | It flagged the unbounded `RUNS` dictionary, the lack of a cleanup sweep for the per-process rate limiter, and the fact that `/ready` didn't distinguish "the process is alive" from "the process can actually take work."                                                                                                                       | **Accepted all three.** The store now has a TTL and cap, with in-flight runs exempt from eviction. The rate limiter has an idle-bucket sweep. `/ready` returns `503` while the service is draining or at capacity.                                                                                                                                                                                                       |
| 13  | Draft the final sign-off. Is this actually production-ready?                                                                                                       | The first draft basically said it was ready with no conditions.                                                                                                                                                                                                                                                                                 | **Modified heavily.** I rewrote it to start with what was actually wrong rather than simply listing what I built. I also added the two release conditions: in-memory storage and per-process rate limiting, plus the DNS-rebinding limitation of the SSRF guard. "Ready, with conditions I can clearly name" felt honest. "Ready, no notes" didn't.                                                                      |


---



## What this process actually looked like

Looking back at the interactions, the AI wasn't really acting as the person making the engineering decisions.

It was more like having another engineer in the room who keeps pointing at things and asking, "Did you think about this?"

Sometimes it was right.

Sometimes it wasn't.

And a few times, it gave me a good starting point but the real answer only showed up once I tested the code myself.

The SSRF work is probably the best example.

The initial recommendation sounded reasonable: validate the URL, use `ipaddress`, block private and loopback addresses.

Then I actually tried `127.1`.

That changed the story.

The library rejected it as an invalid IP, but the HTTP client could still understand it. So the "safe" validation path wasn't actually safe enough.

That was the point where the AI suggestion stopped being the answer and became just the starting point for another test.

The same thing happened with the renderer.

The initial security changes looked fine. Then I attacked the page with actual hostile input.

Most of it was handled correctly.

One wasn't.

The `javascript:` URL still got through the frontend's link-building logic.

Again, the test mattered more than the assumption.

There were also decisions where I deliberately went against the AI.

The all-sources-failed case is one of them.

Returning a successful run with an empty brief might look cleaner from an API perspective. But it would mean calling the LLM when there was nothing useful to give it.

That didn't make sense to me.

So I rejected the suggestion and made the run fail explicitly instead.

And then there was the `tokens_used` decision.

I didn't want to casually change an API contract just because a cleaner number seemed better. But the original metric said 280 tokens had been used when the run had actually consumed 1480.

At that point, keeping the old behaviour just to avoid changing the number felt worse.

So I changed it, kept the detailed fields, and documented exactly where to revert it if a downstream consumer depends on the old contract.

That became a recurring pattern throughout the work.

Ask.

Check the answer against the code.

Try to break it.

Keep what survives.

Reject what doesn't.

And when the answer is somewhere in between, make the engineering call and document it.

That's really what this decision log is meant to capture.

Not that an AI gave me 13 answers.

More that each answer became another step in the investigation, and sometimes the next step was simply trying to prove the answer wrong.