"""Tests for the one seam the other crawl tests stub out: the SOCKS5 connector.

`test_crawl_local.py` replaces `socks_connector` with a plain TCP connector so the crawl
logic can be exercised in milliseconds. That is the right trade, but it means the real
connector -- and with it the credential isolation the entire multi-circuit design rests on
-- would otherwise never run in the suite. A regression there does not fail loudly: the
crawl still works, it just quietly runs every lane down one circuit, which is the exact
outcome `--mode crawl` exists to avoid.

So these tests run the genuine `aiohttp_socks` connector against a local SOCKS5 server
(`tests/socks5server.py`) that records what each connection presented. Still no Tor, still
no network.
"""

import asyncio
import functools
import os
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

from crawler.config import CrawlConfig, ORDER_BFS
from crawler.crawl import run_crawl
from crawler.proxypool import AsyncLanePool, socks_connector
from socks5server import Socks5Server

pytest.importorskip("aiohttp_socks")

UAS = ["Mozilla/5.0 (socks test)"]


def async_test(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))
    return wrapper


@pytest.fixture
def origin(tmp_path):
    """A real HTTP origin, reached only through the proxies.

    Wide rather than deep on purpose: four sibling directories give the four lanes
    something to do at the same time, so more than one circuit is actually dialled.
    """
    root = tmp_path / "srv"
    root.mkdir()
    (root / "top.bin").write_bytes(b"\x00" * 1024)
    for name in ("leaks", "docs", "media", "dumps"):
        (root / name).mkdir(parents=True)
        (root / name / f"{name}.txt").write_text(name)

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root), **kwargs)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------- the isolation claim


@async_test
async def test_every_lane_presents_its_own_credential():
    """Distinct SOCKS credentials are what make lanes distinct Tor circuits.

    Tor keys circuits on the SOCKS username/password (`IsolateSOCKSAuth`). Lanes that
    shared a credential would share a circuit, and the pool would be four sessions
    queueing through one guard.
    """
    with Socks5Server() as proxy, Socks5Server() as other:
        pool = await AsyncLanePool.create(
            [proxy.address, other.address], circuits_per_endpoint=3, user_agents=UAS,
            connector_factory=socks_connector,
        )
        try:
            lanes = pool._lanes                       # deliberately reaching in
            assert len({lane.credential for lane in lanes}) == len(lanes) == 6
            # ...and the credential is a real secret, not a lane number.
            assert all(len(lane.credential.partition(":")[2]) >= 16 for lane in lanes)
        finally:
            await pool.close()


@async_test
async def test_rotate_replaces_the_credential():
    """`rotate()` is the only way off a dead circuit, so it must actually change one."""
    with Socks5Server() as proxy:
        pool = await AsyncLanePool.create(
            [proxy.address], circuits_per_endpoint=1, user_agents=UAS,
            connector_factory=socks_connector,
        )
        try:
            lane = pool._lanes[0]
            before = lane.credential
            await pool.rotate(lane)
            assert lane.credential != before
        finally:
            await pool.close()


# ---------------------------------------------------------------- the real transport


def test_a_crawl_runs_over_real_socks5(origin, tmp_path):
    """The whole stack, with the genuine connector: two proxies, two circuits each."""
    out = str(tmp_path / "out")
    with Socks5Server() as first, Socks5Server() as second:
        config = CrawlConfig(
            seeds=[origin], max_depth=2, order=ORDER_BFS,
            circuits_per_endpoint=2, per_host=4, retries=3,
            out_dir=out, job_id="socks",
        )
        stats, urls = asyncio.run(run_crawl(
            config, [first.address, second.address], UAS,
            connector_factory=socks_connector,
        ))

        # Every request really did traverse a proxy: the origin is only reachable that way
        # as far as the crawler is concerned, and both proxies logged the CONNECT.
        assert first.targets and second.targets
        assert all(t.startswith("127.0.0.1:") for t in first.targets + second.targets)

        # Every connection authenticated, so every one of them is its own circuit. An
        # anonymous connection would mean the isolation was silently skipped.
        assert "<anonymous>" not in first.credentials
        assert "<anonymous>" not in second.credentials
        # A lane belongs to one daemon, so its credential must never show up at the other.
        assert not set(first.credentials) & set(second.credentials)
        # How many of the four lanes get dialled depends on scheduling, but a run that
        # only ever used one circuit would mean the pool is not spreading work at all.
        assert first.distinct_credentials + second.distinct_credentials >= 2

    assert sorted(os.path.basename(u) for u in urls) == [
        "docs.txt", "dumps.txt", "leaks.txt", "media.txt", "top.bin",
    ]
    assert stats["stopped_because"] == "completed"
    assert stats["totals"]["failures"] == 0
    assert all(e["requests"] > 0 for e in stats["endpoints"])
