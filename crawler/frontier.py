"""The crawl frontier: what to fetch next, exactly once, at the right depth.

Three things live here that a bare `asyncio.Queue` cannot do.

**A sort key that can change.** Breadth-first and depth-first differ only in how the
queue is ordered, so switching between them mid-run means re-ordering work that is
*already queued*. A `PriorityQueue` fixes its key at push time; a heap behind a
condition can be re-heapified, which is what `set_order()` does.

**Backoff without occupying a worker.** A job that has to wait 30s for a 503 to clear
is parked in a second heap keyed on its ready time, not slept on. The worker that
failed it goes straight back for other work, and the job re-enters the ready heap on
its own.

**Termination that cannot lose a job.** The run is over when the ready heap is empty,
nothing is deferred, *and* no worker is mid-request -- that last clause being the one
that matters, because the job a worker is about to requeue exists nowhere else at that
instant. This is the same hazard the `multi` mode's inline-retry comment
(`OnionAccelerator.py:418`) works around by refusing to requeue at all; here requeuing
is the whole point, so the accounting has to be right instead.
"""

from __future__ import annotations

import asyncio
import dataclasses
import heapq
import itertools
import logging
import time
from typing import Optional, Pattern, Sequence

from .config import ORDER_BFS, ORDER_DFS
from .urlnorm import dir_url, host_of, is_within, normalize_url

logger = logging.getLogger("OnionAccelerator.crawl.frontier")


@dataclasses.dataclass(frozen=True)
class Job:
    """One directory to fetch."""

    url: str
    depth: int
    parent: Optional[str] = None
    attempt: int = 0
    not_before: float = 0.0

    def retry(self, delay: float) -> "Job":
        """The same job, one attempt later, not runnable until `delay` from now."""
        return dataclasses.replace(
            self, attempt=self.attempt + 1, not_before=time.monotonic() + delay
        )


@dataclasses.dataclass
class LayerStats:
    """Per-depth counters, so a run's shape is readable from the log."""

    queued: int = 0
    done: int = 0
    failed: int = 0

    @property
    def outstanding(self) -> int:
        return self.queued - self.done - self.failed


class HostLimiter:
    """A concurrency cap per target host.

    An onion service is usually one process on one machine behind three relays. Pointing
    forty lanes at it does not make it forty times faster -- it makes it start refusing
    connections, which the crawler then reads as failure and retries, which makes it
    worse. This bounds the self-inflicted half of that.
    """

    def __init__(self, limit: int) -> None:
        self._limit = max(1, limit)
        self._semaphores: dict[str, asyncio.Semaphore] = {}

    def for_url(self, url: str) -> asyncio.Semaphore:
        host = host_of(url)
        sem = self._semaphores.get(host)
        if sem is None:
            sem = asyncio.Semaphore(self._limit)
            self._semaphores[host] = sem
        return sem


class Frontier:
    """Depth-ordered, deduplicated queue of directories to crawl."""

    def __init__(
        self,
        *,
        seeds: Sequence[str],
        max_depth: int,
        order: str = ORDER_BFS,
        include: Optional[Pattern[str]] = None,
        exclude: Optional[Pattern[str]] = None,
        allow_offsite: bool = False,
    ) -> None:
        self._order = order
        self._max_depth = max_depth
        self._include = include
        self._exclude = exclude
        self._allow_offsite = allow_offsite
        # Every seed's directory is a scope root. A crawl of /pub/ must not climb into
        # /admin/ just because something on the page linked there.
        self._scopes = [dir_url(s) for s in seeds]

        self._ready: list[tuple[tuple[int, int], int, Job]] = []
        self._deferred: list[tuple[float, int, Job]] = []
        self._seen: set[str] = set()
        self._counter = itertools.count()
        self._in_flight = 0
        self._closed = False
        self._finished = False
        self._cv = asyncio.Condition()
        self.layers: dict[int, LayerStats] = {}

    # ------------------------------------------------------------ ordering

    def _key(self, depth: int, seq: int) -> tuple[int, int]:
        """Heap key for the current traversal order.

        Breadth-first drains shallow layers first and, within a layer, keeps discovery
        order. Depth-first inverts both: deepest first, and newest-discovered first,
        which is what makes it dive rather than fan out.
        """
        if self._order == ORDER_DFS:
            return (-depth, -seq)
        return (depth, seq)

    async def set_order(self, order: str) -> None:
        """Switch traversal order, re-ordering everything already queued."""
        if order not in (ORDER_BFS, ORDER_DFS):
            raise ValueError(f"unknown order: {order!r}")
        async with self._cv:
            if order == self._order:
                return
            self._order = order
            self._ready = [
                (self._key(job.depth, seq), seq, job) for _, seq, job in self._ready
            ]
            heapq.heapify(self._ready)
            logger.info("traversal order switched to %s (%d job(s) re-ordered)",
                        order, len(self._ready))
            self._cv.notify_all()

    @property
    def order(self) -> str:
        return self._order

    # ------------------------------------------------------------ producing

    async def add(self, url: str, depth: int, parent: Optional[str] = None) -> bool:
        """Offer a newly discovered directory. True if it was accepted.

        Every rejection reason is logged at DEBUG rather than swallowed: "why didn't it
        crawl that directory" is the first question anyone asks of a crawl, and a silent
        filter makes it unanswerable.
        """
        normalized = normalize_url(url)
        if depth > self._max_depth:
            logger.debug("skip depth=%d > max_depth=%d url=%s", depth, self._max_depth, normalized)
            return False
        if not self._in_scope(normalized):
            logger.debug("skip out-of-scope url=%s", normalized)
            return False
        if self._exclude is not None and self._exclude.search(normalized):
            logger.debug("skip --exclude match url=%s", normalized)
            return False
        if self._include is not None and not self._include.search(normalized):
            logger.debug("skip no --include match url=%s", normalized)
            return False

        async with self._cv:
            if normalized in self._seen:
                logger.debug("skip already-seen url=%s", normalized)
                return False
            self._seen.add(normalized)
            self._push(Job(url=normalized, depth=depth, parent=parent))
            self._cv.notify()
        return True

    async def requeue(self, job: Job, delay: float) -> None:
        """Put a failed job back, runnable again after `delay` seconds.

        Must be called *before* the matching `complete()`, or the job can fall through
        the termination check: between the two calls it is in neither the queue nor the
        in-flight count.
        """
        async with self._cv:
            retried = job.retry(delay)
            heapq.heappush(self._deferred, (retried.not_before, next(self._counter), retried))
            logger.debug("requeued url=%s attempt=%d delay=%.1fs",
                         job.url, retried.attempt, delay)
            self._cv.notify_all()

    def _push(self, job: Job) -> None:
        """Heap insert. Caller holds the condition."""
        seq = next(self._counter)
        heapq.heappush(self._ready, (self._key(job.depth, seq), seq, job))
        self.layers.setdefault(job.depth, LayerStats()).queued += 1

    def _in_scope(self, url: str) -> bool:
        if self._allow_offsite:
            return True
        return any(is_within(scope, url) for scope in self._scopes)

    # ------------------------------------------------------------ consuming

    async def get(self) -> Optional[Job]:
        """The next runnable job, or None when the crawl is over.

        None means over, not "empty right now": a worker that gets None can exit.
        """
        async with self._cv:
            while True:
                if self._closed:
                    return None
                self._promote(time.monotonic())
                if self._ready:
                    _, _, job = heapq.heappop(self._ready)
                    self._in_flight += 1
                    return job
                if not self._deferred and self._in_flight == 0:
                    self._finished = True
                    self._cv.notify_all()
                    return None
                await self._wait_for_work()

    async def _wait_for_work(self) -> None:
        """Block until something might be runnable. Caller holds the condition."""
        timeout: Optional[float] = None
        if self._deferred:
            timeout = max(0.0, self._deferred[0][0] - time.monotonic())
        if timeout is None:
            await self._cv.wait()
            return
        try:
            await asyncio.wait_for(self._cv.wait(), timeout or 0.01)
        except asyncio.TimeoutError:
            return          # a deferred job's clock ran out; loop and promote it

    def _promote(self, now: float) -> None:
        """Move every deferred job whose backoff has expired into the ready heap."""
        while self._deferred and self._deferred[0][0] <= now:
            _, _, job = heapq.heappop(self._deferred)
            seq = next(self._counter)
            heapq.heappush(self._ready, (self._key(job.depth, seq), seq, job))

    async def complete(self, job: Job, *, failed: bool = False,
                       requeued: bool = False) -> None:
        """Release a job's in-flight slot. Always call this, exactly once, per get().

        A requeued job releases its slot without being counted: it has not finished, and
        counting every attempt would drive the layer's outstanding total negative.
        """
        async with self._cv:
            self._in_flight -= 1
            stats = self.layers.setdefault(job.depth, LayerStats())
            if requeued:
                self._cv.notify_all()
                return
            if failed:
                stats.failed += 1
            else:
                stats.done += 1
            if stats.outstanding == 0 and stats.queued:
                logger.info("layer %d drained: %d done, %d failed",
                            job.depth, stats.done, stats.failed)
            self._cv.notify_all()

    async def close(self) -> None:
        """Abandon the crawl: wake every worker so they can exit."""
        async with self._cv:
            self._closed = True
            self._cv.notify_all()

    # ------------------------------------------------------------ introspection

    @property
    def seen(self) -> int:
        return len(self._seen)

    @property
    def pending(self) -> int:
        return len(self._ready) + len(self._deferred) + self._in_flight

    @property
    def in_flight(self) -> int:
        return self._in_flight

    @property
    def finished(self) -> bool:
        return self._finished

    def layer_summary(self) -> dict[int, dict[str, int]]:
        return {
            depth: {"queued": s.queued, "done": s.done, "failed": s.failed}
            for depth, s in sorted(self.layers.items())
        }
