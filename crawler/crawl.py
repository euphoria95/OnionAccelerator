"""The crawl orchestrator: workers, layer control, stop conditions.

Everything hard has been pushed into the pieces this drives -- the parser decides what
is in a listing, the pool decides which circuit a request goes down, the fetcher decides
what a failure means, the frontier decides what runs next. What is left here is flow
control, and it is meant to stay readable as such.

The unit of work is one directory. When a listing is parsed, every subdirectory in it
becomes its own independent job, schedulable on any free circuit -- so a directory with
fifty subdirectories becomes fifty parallel units of work rather than one long walk.
That is the whole reason a crawl of an open directory over Tor is tractable at all.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time
from typing import Any, Optional, Sequence

from .config import (
    CrawlConfig,
    ORDER_BFS,
    ORDER_DFS,
    STATS_INTERVAL,
)
from .fetcher import AsyncFetcher, FetchResult, Verdict, backoff_delay
from .frontier import Frontier, HostLimiter, Job
from .listing import Finding, ListingEngine, Page, PageRequest, detect
from .listing.navigate import seed_request
from .proxypool import AsyncLanePool, ConnectorFactory, Endpoint
# _now is the stamp every record in this run carries; it comes from report.py rather
# than being written twice, so a progress event and a listing record cannot end up
# describing the same second in two different formats.
from .report import EVENT_PROGRESS, CrawlReport, Sink, _now

logger = logging.getLogger("OnionAccelerator.crawl")


class Crawler:
    """Drives N workers over one frontier until a stop condition fires."""

    def __init__(
        self,
        config: CrawlConfig,
        pool: AsyncLanePool,
        frontier: Frontier,
        fetcher: AsyncFetcher,
        report: CrawlReport,
        listing: ListingEngine,
    ) -> None:
        self.config = config
        self.pool = pool
        self.frontier = frontier
        self.fetcher = fetcher
        self.report = report
        self.listing = listing
        self.hosts = HostLimiter(config.per_host)

        self._pages = 0
        self._stopped_because = "completed"
        self._stopping = False
        # Which endpoint last failed a given URL, so its retry is steered elsewhere.
        # Keyed by URL because Job is frozen and a retry is a different Job object.
        self._last_failed: dict[str, Endpoint] = {}

    # ------------------------------------------------------------ run

    async def run(self) -> dict[str, Any]:
        """Seed, spawn workers, wait, and write the report. Always writes a report."""
        for seed in self.config.seeds:
            # A forced profile addresses the seed the way it addresses everything else:
            # an API target would otherwise be asked for its root with a GET, answer a
            # JavaScript shell, and the crawl would end before it started.
            request = None
            if self.listing.forced is not None:
                request = seed_request(self.listing.forced, seed)
            if not await self.frontier.add(seed, depth=0, parent=None, request=request):
                logger.warning("seed rejected by the frontier: %s", seed)

        n_workers = self.config.workers or len(self.pool)
        n_workers = max(1, min(n_workers, len(self.pool)))
        logger.info(
            "crawl starting: %d seed(s), %d worker(s) over %d circuit(s), max_depth=%s, order=%s",
            len(self.config.seeds), n_workers, len(self.pool),
            self.config.max_depth if self.config.max_depth is not None else "unlimited",
            self.frontier.order,
        )

        workers = [asyncio.create_task(self._worker(i), name=f"crawl-worker-{i}")
                   for i in range(n_workers)]
        watchers = [asyncio.create_task(self._progress(), name="crawl-progress")]
        if self.config.time_budget:
            watchers.append(asyncio.create_task(self._deadline(), name="crawl-deadline"))
        self._install_signal_handlers()

        try:
            await asyncio.gather(*workers)
        finally:
            for task in watchers:
                task.cancel()
            await asyncio.gather(*watchers, return_exceptions=True)
            self._remove_signal_handlers()

        return self.report.finalize(
            layers=self.frontier.layer_summary(),
            endpoints=self.pool.stats(),
            config=self._config_record(),
            stopped_because=self._stopped_because,
        )

    async def _worker(self, index: int) -> None:
        """Pull jobs until the frontier says the crawl is over."""
        while True:
            job = await self.frontier.get()
            if job is None:
                logger.debug("worker %d: frontier drained, exiting", index)
                return
            failed = False
            requeued = False
            try:
                requeued, failed = await self._process(job)
            except asyncio.CancelledError:
                # Put the work back before unwinding, so a cancelled run's report still
                # reflects what was outstanding rather than silently losing it.
                await self.frontier.requeue(job, 0.0)
                await self.frontier.complete(job, requeued=True)
                raise
            except Exception as exc:                      # noqa: BLE001
                logger.exception("worker %d: unhandled error on %s: %s", index, job.url, exc)
                self.report.record_failure(
                    job, {"url": job.url, "error": f"{type(exc).__name__}: {exc}"}, final=True
                )
                failed = True
            finally:
                await self.frontier.complete(job, failed=failed, requeued=requeued)

    # ------------------------------------------------------------ one directory

    async def _process(self, job: Job) -> tuple[bool, bool]:
        """Fetch, parse and expand one directory. Returns (requeued, failed)."""
        async with self.hosts.for_url(job.url):
            result = await self.fetcher.fetch(job, exclude=self._last_failed.get(job.url))

        self.report.totals.requests += 1
        self.report.totals.bytes_fetched += result.nbytes
        record = result.as_record()

        if result.verdict is Verdict.OK:
            self._last_failed.pop(job.url, None)
            await self._expand(job, result, record)
            return False, False

        if result.verdict is Verdict.LEAF:
            self._last_failed.pop(job.url, None)
            self.report.record_leaf(job, record)
            return False, False

        if result.verdict is Verdict.DROP:
            self.report.record_failure(job, record, final=True)
            return False, True

        return await self._retry(job, result, record)

    async def _expand(self, job: Job, result: FetchResult, record: dict[str, Any]) -> None:
        """Read a fetched page and turn each subdirectory into its own job.

        Every decision about *what the page says* belongs to the listing layer; what is
        left here is what the crawler does about it.
        """
        page = Page(
            url=result.final_url or job.url,
            body=result.body or "",
            status=result.status or 0,
            content_type=result.content_type,
            headers=result.headers,
            request=job.fetch,
        )
        # Before the parse, so a page that turns out not to be a listing is still
        # searchable by a hunt: "the name we are looking for appeared on a page this
        # crawl declined to expand" is exactly the kind of thing worth knowing.
        self.report.record_page(
            page.url, page.body, status=page.status, content_type=page.content_type,
            depth=job.depth, parent=job.parent,
        )
        listing = self.listing.parse(page)

        if not listing.is_index:
            # Not an open directory: a landing page, an app, a file served as HTML.
            # Not expanding it is what stops the crawl turning into a site crawl.
            logger.info("[SKIP] not a directory index (confidence %.2f, profile %s): %s",
                        listing.confidence, listing.profile, job.url)
            self.report.record_skipped(job, listing, record)
            return

        self.report.record_listing(job, listing, record)
        self._pages += 1

        if listing.expandable:
            for entry in listing.directories:
                await self.frontier.add(entry.url, depth=job.depth + 1, parent=job.url,
                                        request=entry.listing_request())

        # Another page of *this* directory, not a level below it: queued at the same
        # depth, or a manager with forty pages would grow a tree forty levels deep.
        for following in listing.more:
            await self.frontier.add(following.url, depth=job.depth, parent=job.parent,
                                    request=following)

        await self._check_layer_switch()
        await self._check_page_budget()

    async def _retry(
        self, job: Job, result: FetchResult, record: dict[str, Any]
    ) -> tuple[bool, bool]:
        """Handle a RETRY or ROTATE verdict. Returns (requeued, failed)."""
        budget = self.config.retries
        if result.retry_budget is not None:
            budget = min(budget, result.retry_budget)

        if result.endpoint is not None:
            self._last_failed[job.url] = result.endpoint
        if result.verdict is Verdict.ROTATE:
            self.report.totals.rotations += 1

        if job.attempt + 1 >= budget:
            self.report.record_failure(job, record, final=True)
            self._last_failed.pop(job.url, None)
            return False, True

        self.report.record_failure(job, record, final=False)
        self.report.totals.retries += 1
        delay = backoff_delay(job.attempt, result.retry_after)
        logger.warning(
            "[RETRY] %s (attempt %d/%d, %s) in %.1fs: %s",
            job.url, job.attempt + 1, budget, result.verdict.value, delay, result.error,
        )
        await self.frontier.requeue(job, delay)
        return True, False

    # ------------------------------------------------------------ flow control

    async def _check_page_budget(self) -> None:
        """Stop cleanly once --max-pages directories have been listed."""
        if self.config.max_pages and self._pages >= self.config.max_pages and not self._stopping:
            await self._stop(f"max_pages ({self.config.max_pages}) reached")

    async def _check_layer_switch(self) -> None:
        """Flip traversal order once --switch-after directories have been listed.

        The use for this is a wide, shallow root: sweep breadth-first until the shape of
        the tree is known, then dive depth-first so that whole branches are finished (and
        written out) instead of every branch being left half-done.
        """
        if not self.config.switch_after or self._pages != self.config.switch_after:
            return
        target = ORDER_DFS if self.frontier.order == ORDER_BFS else ORDER_BFS
        await self.frontier.set_order(target)

    async def _deadline(self) -> None:
        """Stop the crawl when --time-budget expires."""
        assert self.config.time_budget is not None
        await asyncio.sleep(self.config.time_budget)
        await self._stop(f"time budget ({self.config.time_budget:.0f}s) expired")

    async def _stop(self, reason: str) -> None:
        if self._stopping:
            return
        self._stopping = True
        self._stopped_because = reason
        logger.warning("stopping crawl: %s", reason)
        await self.frontier.close()

    async def _progress(self) -> None:
        """Periodic progress and per-endpoint balance.

        The balance line is the one number that says whether multi-Tor concurrency is
        actually working: if one endpoint's count is far ahead of the others, lanes are
        not being spread and the extra daemons are decoration.
        """
        while True:
            await asyncio.sleep(STATS_INTERVAL)
            queued = self.frontier.pending - self.frontier.in_flight
            logger.info(
                "progress: %d dir(s), %d file(s), %d queued, %d in flight, %d seen | %s",
                self.report.totals.directories, self.report.totals.files,
                queued, self.frontier.in_flight, self.frontier.seen,
                self.pool.balance_line(),
            )
            # The same facts to anyone attached to the stream, so a consumer can tell a
            # crawl that is finding nothing from a crawl that has stopped moving.
            self.report.emit(EVENT_PROGRESS, {
                "directories": self.report.totals.directories,
                "files": self.report.totals.files,
                "failures": self.report.totals.failures,
                "queued": queued,
                "in_flight": self.frontier.in_flight,
                "seen": self.frontier.seen,
                "bytes_fetched": self.report.totals.bytes_fetched,
                "endpoints": self.pool.balance_line(),
                "at": _now(),
            })

    # ------------------------------------------------------------ signals

    def _install_signal_handlers(self) -> None:
        """Ctrl-C stops the crawl cleanly instead of killing it.

        A crawl that dies on SIGINT still has its JSONL (it is flushed per line), but it
        has no stats.json, no tree and no urls.txt -- and those are the files anyone
        actually opens first.
        """
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, ValueError):
                loop.add_signal_handler(
                    sig, lambda s=sig: asyncio.create_task(self._stop(f"signal {s.name}"))
                )

    def _remove_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, ValueError):
                loop.remove_signal_handler(sig)

    def _config_record(self) -> dict[str, Any]:
        """The knobs this run used, echoed into stats.json so it can be reproduced."""
        return {
            "max_depth": self.config.max_depth,
            "order": self.frontier.order,
            "workers": self.config.workers or len(self.pool),
            "circuits": len(self.pool),
            "per_host": self.config.per_host,
            "retries": self.config.retries,
            "max_pages": self.config.max_pages,
            "time_budget": self.config.time_budget,
            "allow_offsite": self.config.allow_offsite,
            "include": self.config.include.pattern if self.config.include else None,
            "exclude": self.config.exclude.pattern if self.config.exclude else None,
            "profile": self.config.profile,
            "templates": list(self.config.templates),
        }


# ---------------------------------------------------------------- entry points


async def run_crawl(
    config: CrawlConfig,
    endpoints: Sequence[str],
    user_agents: Sequence[str],
    *,
    connector_factory: Optional[ConnectorFactory] = None,
    sink: Optional[Sink] = None,
) -> tuple[dict[str, Any], list[str]]:
    """Run one crawl to completion. Returns (stats, discovered file URLs).

    `connector_factory` is the Tor seam: leave it None in production, pass a direct
    connector in tests.

    `sink` is the live-output seam: anything callable as sink(kind, record) is handed
    every record as it is written, which is how `--stream` mirrors a running crawl to an
    external process. Leave it None and the crawl behaves exactly as it always did.
    """
    pool = await AsyncLanePool.create(
        endpoints,
        circuits_per_endpoint=config.circuits_per_endpoint,
        user_agents=list(user_agents),
        connector_factory=connector_factory,
    )
    frontier = Frontier(
        seeds=config.seeds,
        max_depth=config.max_depth,
        order=config.order,
        include=config.include,
        exclude=config.exclude,
        allow_offsite=config.allow_offsite,
    )
    fetcher = AsyncFetcher(pool, max_page_bytes=config.max_page_bytes)
    listing = ListingEngine.build(
        forced=config.profile,
        templates=config.templates,
        allow_offsite=config.allow_offsite,
    )

    try:
        with CrawlReport(config.out_dir, config.job_id, config.seeds, sink=sink,
                         stream_bodies=config.stream_bodies,
                         stream_body_bytes=config.stream_body_bytes) as report:
            crawler = Crawler(config, pool, frontier, fetcher, report, listing)
            stats = await crawler.run()
            return stats, report.file_urls
    finally:
        await pool.close()


async def run_detect(
    config: CrawlConfig,
    endpoints: Sequence[str],
    user_agents: Sequence[str],
    *,
    connector_factory: Optional[ConnectorFactory] = None,
) -> list[tuple[str, list[Finding]]]:
    """Fetch each seed once and report which templates read it, without crawling.

    One request per seed, plus one per probe. That is the whole cost of finding out what
    a target is, against the thousands a crawl spends discovering the same thing by
    failing -- and failing quietly, because a target nothing can read looks exactly like
    a target with nothing in it.
    """
    pool = await AsyncLanePool.create(
        endpoints,
        circuits_per_endpoint=config.circuits_per_endpoint,
        user_agents=list(user_agents),
        connector_factory=connector_factory,
    )
    fetcher = AsyncFetcher(pool, max_page_bytes=config.max_page_bytes)
    engine = ListingEngine.build(
        forced=config.profile,
        templates=config.templates,
        allow_offsite=config.allow_offsite,
    )

    async def fetch(request: PageRequest) -> Optional[Page]:
        result = await fetcher.fetch(Job(url=request.url, depth=0, request=request))
        if result.verdict is not Verdict.OK or result.body is None:
            logger.debug("detect: %s %s -> %s (%s)", request.method, request.url,
                         result.status, result.error or result.verdict.value)
            return None
        return Page(
            url=result.final_url or request.url,
            body=result.body,
            status=result.status or 0,
            content_type=result.content_type,
            headers=result.headers,
            request=request,
        )

    try:
        return [(seed, await detect(engine, seed, fetch)) for seed in config.seeds]
    finally:
        await pool.close()


def detect_targets(
    config: CrawlConfig,
    endpoints: Sequence[str],
    user_agents: Sequence[str],
    *,
    connector_factory: Optional[ConnectorFactory] = None,
) -> list[tuple[str, list[Finding]]]:
    """Synchronous wrapper around run_detect(), for the CLI."""
    return asyncio.run(run_detect(
        config, endpoints, user_agents, connector_factory=connector_factory
    ))


def crawl(
    config: CrawlConfig,
    endpoints: Sequence[str],
    user_agents: Sequence[str],
    *,
    connector_factory: Optional[ConnectorFactory] = None,
    sink: Optional[Sink] = None,
) -> tuple[dict[str, Any], list[str]]:
    """Synchronous wrapper, so the rest of OnionAccelerator never has to see a loop."""
    started = time.monotonic()
    try:
        return asyncio.run(run_crawl(
            config, endpoints, user_agents,
            connector_factory=connector_factory, sink=sink,
        ))
    finally:
        logger.debug("crawl mode wall time: %.1fs", time.monotonic() - started)
