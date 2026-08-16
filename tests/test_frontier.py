"""Tests for the crawl frontier.

The frontier is where a crawler quietly goes wrong: it revisits a directory forever, or
it loses one to a race and never says so, or it drains depth-first when it was asked for
breadth-first. None of those show up as a crash, and on Tor none of them are cheap.

pytest-asyncio isn't a dependency of this project, so each coroutine test is driven
through asyncio.run() by the `async_test` decorator -- the same thing the plugin does,
without the install.
"""

import asyncio
import functools
import time

import pytest

from crawler.config import ORDER_BFS, ORDER_DFS
from crawler.frontier import Frontier, HostLimiter, Job

SEED = "http://examplexyz.onion/files/"


def async_test(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))
    return wrapper


def make_frontier(**kwargs) -> Frontier:
    kwargs.setdefault("seeds", [SEED])
    kwargs.setdefault("max_depth", 3)
    return Frontier(**kwargs)


async def drain(frontier: Frontier) -> list[Job]:
    """Take every job the frontier hands out, completing each one immediately."""
    jobs = []
    while True:
        job = await frontier.get()
        if job is None:
            return jobs
        jobs.append(job)
        await frontier.complete(job)


# ---------------------------------------------------------------- deduplication


@async_test
async def test_a_url_is_queued_once():
    frontier = make_frontier()
    assert await frontier.add(SEED + "a/", depth=1)
    assert not await frontier.add(SEED + "a/", depth=1)
    # ...including when it arrives in a different but equivalent form.
    assert not await frontier.add(SEED + "a/#anchor", depth=1)
    assert not await frontier.add(SEED + "b/../a/", depth=1)
    assert not await frontier.add("HTTP://EXAMPLEXYZ.ONION/files/a/", depth=1)
    assert frontier.seen == 1
    assert len(await drain(frontier)) == 1


@async_test
async def test_a_symlink_loop_terminates():
    """The failure this exists to prevent: a directory that contains itself."""
    frontier = make_frontier(max_depth=10)
    await frontier.add(SEED + "loop/", depth=1)
    seen = 0
    while True:
        job = await frontier.get()
        if job is None:
            break
        seen += 1
        assert seen < 50, "the loop was not cut"
        # Every listing of loop/ links loop/ again, one level down.
        await frontier.add(job.url, depth=job.depth + 1)
        await frontier.complete(job)
    assert seen == 1


# ---------------------------------------------------------------- ordering


@async_test
async def test_bfs_drains_a_layer_before_the_next():
    frontier = make_frontier(order=ORDER_BFS)
    await frontier.add(SEED + "deep/a/b/", depth=3)
    await frontier.add(SEED + "a/", depth=1)
    await frontier.add(SEED + "mid/a/", depth=2)
    await frontier.add(SEED + "b/", depth=1)

    depths = [job.depth for job in await drain(frontier)]
    assert depths == sorted(depths), depths
    assert depths == [1, 1, 2, 3]


@async_test
async def test_dfs_takes_the_deepest_first():
    frontier = make_frontier(order=ORDER_DFS)
    await frontier.add(SEED + "a/", depth=1)
    await frontier.add(SEED + "mid/a/", depth=2)
    await frontier.add(SEED + "deep/a/b/", depth=3)

    depths = [job.depth for job in await drain(frontier)]
    assert depths == [3, 2, 1]


@async_test
async def test_order_switch_reorders_work_already_queued():
    """The reason this is a heap and not an asyncio.PriorityQueue.

    A PriorityQueue fixes each item's key when it is pushed, so flipping the traversal
    order would only affect jobs discovered afterwards -- which, mid-crawl, is most of
    the queue but never the part that matters.
    """
    frontier = make_frontier(order=ORDER_BFS)
    for depth, path in [(1, "a/"), (2, "mid/a/"), (3, "deep/a/b/")]:
        await frontier.add(SEED + path, depth=depth)

    first = await frontier.get()
    assert first is not None and first.depth == 1
    await frontier.complete(first)

    await frontier.set_order(ORDER_DFS)
    assert [job.depth for job in await drain(frontier)] == [3, 2]


# ---------------------------------------------------------------- retries


@async_test
async def test_a_requeued_job_comes_back_and_is_not_double_counted():
    frontier = make_frontier()
    await frontier.add(SEED + "a/", depth=1)

    job = await frontier.get()
    assert job is not None
    await frontier.requeue(job, delay=0.0)
    await frontier.complete(job, requeued=True)

    again = await frontier.get()
    assert again is not None and again.url == job.url
    assert again.attempt == 1
    await frontier.complete(again)

    assert frontier.layer_summary()[1] == {"queued": 1, "done": 1, "failed": 0}


@async_test
async def test_backoff_defers_without_blocking_and_without_terminating():
    """A deferred job keeps the crawl alive even when nothing else is runnable.

    If `get()` treated an empty ready-heap as "finished", every worker would exit while
    a job sat waiting out its backoff -- and the crawl would report success having
    silently dropped it.
    """
    frontier = make_frontier()
    await frontier.add(SEED + "a/", depth=1)
    job = await frontier.get()
    assert job is not None

    await frontier.requeue(job, delay=0.25)
    await frontier.complete(job, requeued=True)

    started = time.monotonic()
    again = await frontier.get()
    elapsed = time.monotonic() - started

    assert again is not None
    assert elapsed >= 0.2, f"returned after only {elapsed:.3f}s"
    await frontier.complete(again)


@async_test
async def test_the_last_worker_can_requeue_without_the_job_being_lost():
    """The race the in-flight count exists for.

    With one job outstanding and one worker holding it, the ready heap is empty. If a
    second worker read that as "the crawl is over" before the first requeued, the job
    would vanish. The in-flight count is what makes the second worker wait.
    """
    frontier = make_frontier()
    await frontier.add(SEED + "a/", depth=1)
    held = await frontier.get()
    assert held is not None

    # A second worker asks for work while the first is still holding the only job.
    waiter = asyncio.create_task(frontier.get())
    await asyncio.sleep(0.05)
    assert not waiter.done(), "the second worker concluded the crawl was over"

    await frontier.requeue(held, delay=0.0)
    await frontier.complete(held, requeued=True)

    job = await asyncio.wait_for(waiter, timeout=1.0)
    assert job is not None and job.url == held.url
    await frontier.complete(job)


# ---------------------------------------------------------------- filters


@async_test
async def test_depth_cap():
    frontier = make_frontier(max_depth=2)
    assert await frontier.add(SEED + "a/b/", depth=2)
    assert not await frontier.add(SEED + "a/b/c/", depth=3)


@async_test
async def test_scope_is_the_seed_directory():
    frontier = make_frontier()
    assert not await frontier.add("http://examplexyz.onion/other/", depth=1)
    assert not await frontier.add("http://elsewherexyz.onion/files/", depth=1)
    assert not await frontier.add("http://examplexyz.onion/", depth=1)
    assert await frontier.add(SEED + "deeper/", depth=1)


@async_test
async def test_allow_offsite_lifts_the_scope():
    frontier = make_frontier(allow_offsite=True)
    assert await frontier.add("http://elsewherexyz.onion/files/", depth=1)


@async_test
async def test_include_and_exclude():
    import re
    frontier = make_frontier(include=re.compile(r"/dumps?/"),
                             exclude=re.compile(r"/thumbs/"))
    assert await frontier.add(SEED + "dump/", depth=1)
    assert not await frontier.add(SEED + "photos/", depth=1)
    assert not await frontier.add(SEED + "dump/thumbs/", depth=2)


# ---------------------------------------------------------------- termination


@async_test
async def test_close_wakes_a_waiting_worker():
    frontier = make_frontier()
    await frontier.add(SEED + "a/", depth=1)
    held = await frontier.get()
    assert held is not None

    waiter = asyncio.create_task(frontier.get())
    await asyncio.sleep(0.05)
    assert not waiter.done()

    await frontier.close()
    assert await asyncio.wait_for(waiter, timeout=1.0) is None


# ---------------------------------------------------------------- host limiter


@async_test
async def test_host_limiter_is_per_host():
    limiter = HostLimiter(1)
    a = limiter.for_url("http://a.onion/x/")
    b = limiter.for_url("http://b.onion/x/")
    assert a is limiter.for_url("http://a.onion/y/")
    assert a is not b

    async with a:
        # A second request to the same host must wait; a different host must not.
        blocked = asyncio.create_task(a.acquire())
        await asyncio.sleep(0.05)
        assert not blocked.done()
        async with b:
            pass
    await asyncio.wait_for(blocked, timeout=1.0)
