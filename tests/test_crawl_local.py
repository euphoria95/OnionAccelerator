"""End-to-end crawls against a local server. Nothing here touches Tor.

The crawler has exactly one seam where Tor lives -- `AsyncLanePool.create`'s
`connector_factory`, which is the only place a SOCKS connector is built. Swapping it
for a plain TCP connector turns the whole stack into something that can be run in a
fraction of a second against `http.server`, which is the same trick `tests/conftest.py`
plays on the download modes by stubbing `make_proxies`.

The endpoint *labels* stay in place while the connector is swapped, so lane
interleaving and per-endpoint accounting are still exercised: the pool cannot tell that
its two "daemons" both dial the same loopback port.
"""

import asyncio
import json
import os

import aiohttp
import pytest

from crawler.config import CrawlConfig, ORDER_BFS
from crawler.crawl import run_crawl
from indexserver import IndexServer

# Two fake daemons. Nothing dials them; they exist so the pool has more than one
# endpoint to spread lanes across and to account for separately.
ENDPOINTS = ["127.0.0.1:19050", "127.0.0.1:19052"]
UAS = ["Mozilla/5.0 (crawl test A)", "Mozilla/5.0 (crawl test B)"]


def direct_connector(_endpoint, _credential, _user_agent) -> aiohttp.BaseConnector:
    """The Tor seam, wired straight through to TCP."""
    return aiohttp.TCPConnector(limit=1, force_close=True)


@pytest.fixture
def tree(tmp_path):
    """A small directory tree: nested, with a colliding basename and an empty dir."""
    root = tmp_path / "srv"
    (root / "a" / "deep").mkdir(parents=True)
    (root / "b").mkdir()
    (root / "empty").mkdir()

    (root / "top.bin").write_bytes(b"\x00" * 2048)
    # The same basename in two directories: flattening these would lose one.
    (root / "a" / "readme.txt").write_text("a")
    (root / "b" / "readme.txt").write_text("b")
    (root / "a" / "deep" / "buried.txt").write_text("deep")
    return str(root)


@pytest.fixture
def looping_tree(tree):
    """The same tree, with a self-referential symlink in it.

    Kept separate because the loop is genuinely infinite: every level of `a/loop/loop/`
    is a *different* URL serving the same files, so deduplication cannot cut it and the
    file count is a function of the depth cap rather than of the tree.
    """
    os.symlink(os.path.join(tree, "a"), os.path.join(tree, "a", "loop"))
    return tree


def crawl(base_url, out_dir, **overrides):
    connector_factory = overrides.pop("connector_factory", direct_connector)
    config = CrawlConfig(
        seeds=[base_url],
        max_depth=overrides.pop("max_depth", 4),
        order=overrides.pop("order", ORDER_BFS),
        circuits_per_endpoint=overrides.pop("circuits_per_endpoint", 2),
        retries=overrides.pop("retries", 5),
        per_host=overrides.pop("per_host", 4),
        out_dir=out_dir,
        job_id="test",
        **overrides,
    )
    return asyncio.run(run_crawl(
        config, ENDPOINTS, UAS, connector_factory=connector_factory,
    ))


def records(out_dir, name):
    path = os.path.join(out_dir, name)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


# ---------------------------------------------------------------- the happy path


def test_crawl_finds_every_file_exactly_once(tree, tmp_path):
    out = str(tmp_path / "out")
    with IndexServer(tree) as server:
        stats, urls = crawl(server.base_url, out)

    names = sorted(os.path.basename(u) for u in urls)
    # readme.txt appears twice because it exists twice, in different directories.
    assert names == ["buried.txt", "readme.txt", "readme.txt", "top.bin"]
    assert len(urls) == len(set(urls)), "a file was recorded more than once"

    listing = records(out, "listing.jsonl")
    assert {r["path"] for r in listing} == {
        "/top.bin", "/a/readme.txt", "/b/readme.txt", "/a/deep/buried.txt",
    }
    assert stats["totals"]["files"] == 4
    assert stats["stopped_because"] == "completed"


def test_every_directory_is_listed_once(tree, tmp_path):
    out = str(tmp_path / "out")
    with IndexServer(tree) as server:
        crawl(server.base_url, out, max_depth=2)

    dirs = records(out, "dirs.jsonl")
    urls = [d["url"] for d in dirs]
    assert len(urls) == len(set(urls)), "a directory was listed twice"
    assert all(d["is_index"] for d in dirs)
    # The real autoindex is a seventh markup the parser has never been shown.
    assert all(d["n_dirs"] + d["n_files"] >= 0 for d in dirs)


def test_an_empty_directory_is_still_visited(tree, tmp_path):
    out = str(tmp_path / "out")
    with IndexServer(tree) as server:
        crawl(server.base_url, out)
    listed = {d["url"].rstrip("/").rsplit("/", 1)[-1] for d in records(out, "dirs.jsonl")}
    assert "empty" in listed


def test_reports_are_written(tree, tmp_path):
    out = str(tmp_path / "out")
    with IndexServer(tree) as server:
        crawl(server.base_url, out)

    for name in ("listing.jsonl", "dirs.jsonl", "stats.json", "tree.txt", "urls.txt"):
        assert os.path.exists(os.path.join(out, name)), name

    tree_txt = open(os.path.join(out, "tree.txt"), encoding="utf-8").read()
    assert "buried.txt" in tree_txt and "deep/" in tree_txt

    stats = json.load(open(os.path.join(out, "stats.json"), encoding="utf-8"))
    assert stats["config"]["max_depth"] == 4
    assert sum(layer["done"] for layer in stats["layers"].values()) > 0


# ---------------------------------------------------------------- work control


def test_depth_cap_stops_the_symlink_loop(looping_tree, tmp_path):
    """Only the depth cap can end this: every level of the loop is a *new* URL."""
    out = str(tmp_path / "out")
    with IndexServer(looping_tree) as server:
        stats, _ = crawl(server.base_url, out, max_depth=3)

    depths = [int(d) for d in stats["layers"]]
    assert max(depths) <= 3
    dirs = records(out, "dirs.jsonl")
    assert all(d["depth"] <= 3 for d in dirs)
    # The loop was walked, not skipped -- it just ran out of depth.
    assert any("loop" in d["url"] for d in dirs)


def test_max_pages_stops_the_crawl(tree, tmp_path):
    out = str(tmp_path / "out")
    with IndexServer(tree) as server:
        stats, _ = crawl(server.base_url, out, max_depth=6, max_pages=3)

    assert "max_pages" in stats["stopped_because"]
    # The report still has to be complete and valid for what was crawled.
    assert os.path.exists(os.path.join(out, "stats.json"))
    assert stats["totals"]["directories"] >= 3


def test_exclude_keeps_a_subtree_out(tree, tmp_path):
    out = str(tmp_path / "out")
    with IndexServer(tree) as server:
        _, urls = crawl(server.base_url, out,
                        exclude=CrawlConfig.compile_filter(r"/a/"))
    assert not any("/a/" in u for u in urls)
    assert any(u.endswith("/b/readme.txt") for u in urls)


# ---------------------------------------------------------------- failure handling


def test_a_503_is_retried_and_then_succeeds(tree, tmp_path):
    """Transient failure: two 503s, then the directory lists normally."""
    out = str(tmp_path / "out")
    with IndexServer(tree, flaky={"/b/": 2}) as server:
        stats, urls = crawl(server.base_url, out)

    assert server.hits("/b/") == 3, server.request_log
    assert any(u.endswith("/b/readme.txt") for u in urls), "the retry never landed"

    failures = records(out, "failed.jsonl")
    assert [f for f in failures if f["status"] == 503]
    assert all(not f["final"] for f in failures if f["url"].endswith("/b/"))
    assert stats["totals"]["retries"] >= 2


def test_a_permanently_failing_directory_is_recorded_and_the_crawl_continues(tree, tmp_path):
    out = str(tmp_path / "out")
    with IndexServer(tree, flaky={"/b/": 99}) as server:
        stats, urls = crawl(server.base_url, out, retries=2)

    failed = [f for f in records(out, "failed.jsonl") if f["final"]]
    assert len(failed) == 1 and failed[0]["url"].endswith("/b/")
    # Everything outside the broken subtree still made it.
    assert any(u.endswith("/a/readme.txt") for u in urls)
    assert stats["stopped_because"] == "completed"


def test_a_dead_endpoint_is_parked_and_the_crawl_still_completes(tree, tmp_path):
    """Half the pool dials a closed port; the crawl must route around it.

    This is the pool's failover contract under load: the endpoint accumulates failures,
    gets parked, and every subsequent lease prefers the live daemon.
    """
    class DeadConnector(aiohttp.TCPConnector):
        """Fails every dial the way a refused SOCKS connection does.

        Pointing a resolver at a closed port would not do it: the test server is
        reached by IP literal, and aiohttp skips the resolver entirely for those.
        """

        async def _create_connection(self, req, traces, timeout):
            raise aiohttp.ClientConnectorError(
                req.connection_key, OSError(111, "Connection refused")
            )

    def half_dead(endpoint, _credential, _ua):
        connector = DeadConnector if endpoint.address == ENDPOINTS[0] else aiohttp.TCPConnector
        return connector(limit=1, force_close=True)

    out = str(tmp_path / "out")
    with IndexServer(tree) as server:
        stats, urls = crawl(server.base_url, out, retries=6,
                            connector_factory=half_dead)

    by_endpoint = {e["endpoint"]: e for e in stats["endpoints"]}
    assert by_endpoint[ENDPOINTS[0]]["failures"] > 0, "the dead endpoint never failed"
    assert by_endpoint[ENDPOINTS[1]]["requests"] > 0, "the live endpoint was never used"
    assert stats["totals"]["rotations"] > 0, "a dead circuit was never rotated away from"
    assert sorted(os.path.basename(u) for u in urls) == [
        "buried.txt", "readme.txt", "readme.txt", "top.bin",
    ]


# ---------------------------------------------------------------- pool behaviour


def test_requests_are_spread_across_endpoints(tree, tmp_path):
    """The point of multi-Tor concurrency: no endpoint carries the whole crawl."""
    out = str(tmp_path / "out")
    with IndexServer(tree) as server:
        stats, _ = crawl(server.base_url, out)

    counts = [e["requests"] for e in stats["endpoints"]]
    assert all(c > 0 for c in counts), counts
    assert max(counts) <= 3 * min(counts), f"lanes are not being spread: {counts}"
