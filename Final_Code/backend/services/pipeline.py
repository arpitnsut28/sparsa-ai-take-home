"""
services/pipeline.py — the work of a single run: scrape, de-duplicate,
synthesise, and assemble the KPI row.

Deliberately free of HTTP and storage concerns so it can be tested directly and
so the orchestration in run_service.py stays about lifecycle, not content.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import config
from services import stubs
from services.retry import TimeoutExceeded, call_with_retry

logger = logging.getLogger("research_runs.pipeline")


class NoUsableSources(Exception):
    """Every page failed, so there is nothing to synthesise from."""


@dataclass
class PageOutcome:
    url: str
    ok: bool
    title: str = ""
    content: str = ""
    error_kind: str = ""
    error: str = ""


def _fingerprint(page: PageOutcome) -> str:
    """Identity of a page's *content*, for de-duplication.

    The original code de-duplicated on title, which misses the case this is
    actually for: the same document served from two URLs (canonical vs. amp,
    tracking parameters, mirrors). Hashing normalised content catches those and
    keeps identical-title-different-content pages separate.
    """
    normalised = " ".join(page.content.split()).lower()
    if not normalised:
        return f"title:{page.title.strip().lower()}"
    return hashlib.sha256(normalised.encode("utf-8", "replace")).hexdigest()


async def _scrape_one(url: str, executor, semaphore: asyncio.Semaphore) -> PageOutcome:
    """Scrape one page. Never raises for a page-level failure.

    Isolating failures here is what makes ``success_rate`` a real KPI instead of
    a constant: one dead link degrades a run, it does not fail it.
    """
    async with semaphore:
        try:
            page = await call_with_retry(
                stubs.scrape_page,
                url,
                executor=executor,
                timeout_s=config.settings.scrape_timeout_s,
                max_attempts=config.settings.scrape_max_attempts,
                retry_on=(stubs.ScrapeError,),
                is_retryable=lambda exc: getattr(exc, "retryable", False),
                label=f"scrape {url}",
            )
        except asyncio.CancelledError:
            raise
        except TimeoutExceeded as exc:
            return PageOutcome(url=url, ok=False, error_kind="timeout", error=str(exc))
        except stubs.ScrapeError as exc:
            return PageOutcome(url=url, ok=False, error_kind=exc.kind, error=str(exc))
        except Exception as exc:  # a scraper bug must not take the run down
            logger.exception("unexpected scrape failure for %s", url)
            return PageOutcome(
                url=url,
                ok=False,
                error_kind="internal_error",
                error=f"{type(exc).__name__}: {exc}",
            )

    content = (page.get("content") or "").strip()
    if not content:
        # A 200 with an empty body is a failed scrape, not a successful one.
        return PageOutcome(
            url=url, ok=False, error_kind="empty_content", error="page returned no content"
        )
    return PageOutcome(url=url, ok=True, title=page.get("title") or url, content=content)


async def scrape_all(urls: list[str], executor) -> list[PageOutcome]:
    """Scrape every URL with bounded concurrency, preserving input order.

    The original scraped serially, so a 20-URL run took 20 x the per-page
    latency. The semaphore keeps that bounded without letting one run open
    an unbounded number of connections.
    """
    semaphore = asyncio.Semaphore(max(1, config.settings.page_concurrency))
    return list(
        await asyncio.gather(*(_scrape_one(url, executor, semaphore) for url in urls))
    )


def deduplicate(pages: list[PageOutcome]) -> list[PageOutcome]:
    """Keep the first page of each distinct content fingerprint."""
    seen: set[str] = set()
    unique: list[PageOutcome] = []
    for page in pages:
        key = _fingerprint(page)
        if key not in seen:
            seen.add(key)
            unique.append(page)
    return unique


def build_context(pages: list[PageOutcome]) -> str:
    """Assemble the scraped text the brief must be grounded in, within budget.

    The original called the LLM with the bare prompt and never passed the
    scraped pages, so the "brief" was unrelated to the sources it cited. Budgets
    are enforced here because context size is the cost and latency driver, and
    an unbounded 20-page context is how a run turns into a surprise invoice.
    """
    per_page = config.settings.max_chars_per_page
    total_budget = config.settings.max_context_chars

    chunks: list[str] = []
    used = 0
    for page in pages:
        body = page.content[:per_page]
        block = f"<source url=\"{page.url}\" title=\"{page.title}\">\n{body}\n</source>"
        if used + len(block) > total_budget:
            logger.info("context budget reached; %d source(s) included", len(chunks))
            break
        chunks.append(block)
        used += len(block)
    return "\n\n".join(chunks)


def extract_text(message) -> str:
    """Pull the brief out of an Anthropic-shaped message.

    ``message.content`` is a *list of blocks*, not a string — assigning it
    straight to ``brief`` is the bug that made the API return
    ``[{"text": ...}]`` and the page render ``[object Object]``. Blocks that
    are not text (tool_use, thinking) are skipped rather than stringified.
    """
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, (list, tuple)):
        return ""
    parts = [
        block.text
        for block in content
        if getattr(block, "type", "text") == "text" and isinstance(getattr(block, "text", None), str)
    ]
    return "\n".join(parts).strip()


def extract_usage(message) -> tuple[int, int]:
    """Return (input_tokens, output_tokens), defaulting to 0 when absent."""
    usage = getattr(message, "usage", None)

    def _as_int(value) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    return _as_int(getattr(usage, "input_tokens", 0)), _as_int(getattr(usage, "output_tokens", 0))


async def synthesise(prompt: str, pages: list[PageOutcome], executor) -> tuple[str, int, int]:
    """Call the LLM over the scraped context. Returns (brief, in_tokens, out_tokens)."""
    context = build_context(pages)
    full_prompt = (
        "Answer the research request using only the sources below. "
        "Cite source URLs inline where they support a point.\n\n"
        f"<request>{prompt}</request>\n\n{context}"
    )

    message = await call_with_retry(
        stubs.call_llm,
        full_prompt,
        executor=executor,
        timeout_s=config.settings.llm_timeout_s,
        max_attempts=config.settings.llm_max_attempts,
        retry_on=(stubs.LLMError,),
        is_retryable=lambda exc: getattr(exc, "retryable", False),
        label="llm",
    )

    brief = extract_text(message)
    input_tokens, output_tokens = extract_usage(message)
    if not brief:
        raise stubs.LLMError("model returned an empty brief", kind="empty_brief")
    return brief, input_tokens, output_tokens


async def run_pipeline(prompt: str, urls: list[str], executor) -> dict:
    """Execute one run end to end and return the KPI row."""
    started = time.perf_counter()

    pages = await scrape_all(urls, executor)
    successful = [p for p in pages if p.ok]

    if not successful:
        # Nothing to synthesise from. Fail loudly instead of paying for an
        # LLM call that can only hallucinate, and instead of reporting a brief
        # that is grounded in nothing.
        raise NoUsableSources(
            f"all {len(pages)} source(s) failed to scrape: "
            + ", ".join(sorted({p.error_kind for p in pages}))
        )

    unique = deduplicate(successful)
    brief, input_tokens, output_tokens = await synthesise(prompt, unique, executor)

    pages_scraped = len(pages)
    success_rate = len(successful) / pages_scraped if pages_scraped else 0.0

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "prompt": prompt,
        "pages_scraped": pages_scraped,
        "success_rate": round(success_rate, 4),
        # "tokens used" is what the run actually costs: prompt + completion.
        # The original reported output tokens only, hiding ~80% of the spend.
        "tokens_used": input_tokens + output_tokens,
        "sources": [p.url for p in unique],
        "brief": brief,
        # Additive fields — the documented shape above is unchanged.
        "pages_succeeded": len(successful),
        "pages_failed": pages_scraped - len(successful),
        "duplicates_dropped": len(successful) - len(unique),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "failures": [
            {"url": p.url, "kind": p.error_kind, "error": p.error} for p in pages if not p.ok
        ],
    }
