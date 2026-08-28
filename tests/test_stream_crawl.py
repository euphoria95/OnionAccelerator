"""A crawl with a consumer attached, end to end against a local server.

The invariant this file exists to protect: **what the stream says and what the crawl
writes to disk are the same records**. An analyst who greps the live stream and an
analyst who greps listing.jsonl afterwards have to reach the same conclusion, or the
feature is worse than useless -- it is a second, quieter version of the evidence.

Same trick as tests/test_crawl_local.py: the Tor seam is swapped for a plain TCP
connector, so the whole stack runs against http.server in a fraction of a second.
"""

import asyncio
import json
import os
import sys

import aiohttp
import pytest

import crawler  # noqa: F401  (imported for the module lookup below)
from crawler.config import CrawlConfig
from crawler.crawl import run_crawl
from eventstream import EventBus
from indexserver import IndexServer

# crawler/__init__.py re-exports the crawl() *function* under the package's own
# `crawl` attribute, so `crawler.crawl` is not the module. Reach it the unambiguous way.
CRAWL_MODULE = sys.modules["crawler.crawl"]

ENDPOINTS = ["127.0.0.1:19050", "127.0.0.1:19052"]
UAS = ["Mozilla/5.0 (stream test)"]


def direct_connector(_endpoint, _credential, _user_agent) -> aiohttp.BaseConnector:
    return aiohttp.TCPConnector(limit=1, force_close=True)


class Recorder:
    """A sink that keeps everything, standing in for an attached consumer."""

    def __init__(self):
        self.events = []

    def __call__(self, kind, data):
        self.events.append((kind, data))

    def of(self, kind):
        return [data for name, data in self.events if name == kind]

    @property
    def kinds(self):
        return {kind for kind, _ in self.events}


class _Pool:
    """Enough of AsyncLanePool for the progress watcher to sample."""

    def balance_line(self):
        return "127.0.0.1:19050=0"

    def __len__(self):
        return 1


@pytest.fixture
def tree(tmp_path):
    """A small tree with something worth hunting for in it."""
    root = tmp_path / "srv"
    (root / "hr" / "2019").mkdir(parents=True)
    (root / "photos").mkdir()
    (root / "hr" / "2019" / "payroll.xlsx").write_bytes(b"x" * 4096)
    (root / "hr" / "staff.csv").write_text("name\n")
    (root / "photos" / "party.jpg").write_bytes(b"\xff" * 128)
    (root / "readme.txt").write_text("hello")
    return str(root)


def crawl(base_url, out_dir, sink=None, **overrides):
    config = CrawlConfig(
        seeds=[base_url],
        max_depth=overrides.pop("max_depth", 4),
        circuits_per_endpoint=1,
        retries=overrides.pop("retries", 3),
        per_host=4,
        out_dir=out_dir,
        job_id="stream-test",
        **overrides,
    )
    return asyncio.run(run_crawl(config, ENDPOINTS, UAS,
                                 connector_factory=direct_connector, sink=sink))


def lines(out_dir, name):
    path = os.path.join(out_dir, name)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


# ----------------------------------------------------------------- the invariant


def test_every_streamed_file_is_a_line_of_listing_jsonl(tree, tmp_path):
    sink = Recorder()
    out = str(tmp_path / "out")
    with IndexServer(tree) as server:
        crawl(server.base_url, out, sink)

    assert sink.of("crawl.file") == lines(out, "listing.jsonl")


def test_every_streamed_directory_is_a_line_of_dirs_jsonl(tree, tmp_path):
    """dirs.jsonl holds both: the ones that were listed and the ones that were refused."""
    sink = Recorder()
    out = str(tmp_path / "out")
    with IndexServer(tree, applications=["/photos/"]) as server:
        crawl(server.base_url, out, sink)

    streamed = [data for kind, data in sink.events if kind in ("crawl.dir", "crawl.skip")]
    assert streamed == lines(out, "dirs.jsonl")
    assert sink.of("crawl.skip"), "the application page should have been refused"


def test_the_counts_agree_with_stats_json(tree, tmp_path):
    sink = Recorder()
    out = str(tmp_path / "out")
    with IndexServer(tree) as server:
        stats, _ = crawl(server.base_url, out, sink)

    assert len(sink.of("crawl.file")) == stats["totals"]["files"]
    assert len(sink.of("crawl.dir")) == stats["totals"]["directories"]
    assert len(sink.of("crawl.skip")) == stats["totals"]["skipped"]


def test_a_failure_reaches_the_stream_as_it_reaches_failed_jsonl(tree, tmp_path):
    sink = Recorder()
    out = str(tmp_path / "out")
    # 503 on every attempt for one directory: retried, then given up on.
    with IndexServer(tree, flaky={"/hr/": 99}) as server:
        crawl(server.base_url, out, sink, retries=2)

    assert sink.of("crawl.fail") == lines(out, "failed.jsonl")
    assert any(record["final"] for record in sink.of("crawl.fail"))


def test_the_records_are_the_objects_on_disk_not_a_second_description(tree, tmp_path):
    """Field for field, including the ones nobody would think to reproduce."""
    sink = Recorder()
    out = str(tmp_path / "out")
    with IndexServer(tree) as server:
        crawl(server.base_url, out, sink)

    payroll = next(r for r in sink.of("crawl.file") if "payroll" in r["url"])
    assert set(payroll) >= {"url", "host", "path", "name", "size_bytes", "mtime_text",
                            "depth", "parent", "endpoint", "discovered_at"}
    # The size is None here because a stdlib autoindex does not advertise one; what
    # matters is that the stream reproduces whatever the listing said, and where it sat.
    assert payroll["name"] == "payroll.xlsx"
    assert payroll["path"] == "/hr/2019/payroll.xlsx"
    assert payroll["parent"].endswith("/hr/2019/")
    assert payroll["depth"] == 3


# ----------------------------------------------------------------- ordering


def test_findings_arrive_during_the_crawl_not_after_it(tree, tmp_path):
    """A stream that only fills up at the end would be a slower way to read a file."""
    seen_before_finish = []

    class Watcher(Recorder):
        def __call__(self, kind, data):
            super().__call__(kind, data)
            if kind == "crawl.file":
                # The report writes urls.txt and tree.txt only in finalize().
                seen_before_finish.append(
                    os.path.exists(os.path.join(out, "urls.txt")))

    out = str(tmp_path / "out")
    with IndexServer(tree) as server:
        crawl(server.base_url, out, Watcher())

    assert seen_before_finish and not any(seen_before_finish)


# ----------------------------------------------------------------- page bodies


def test_page_text_stays_off_the_stream_unless_it_is_asked_for(tree, tmp_path):
    sink = Recorder()
    out = str(tmp_path / "out")
    with IndexServer(tree) as server:
        crawl(server.base_url, out, sink)
    assert "crawl.page" not in sink.kinds


def test_stream_bodies_publishes_the_text_truncated_and_says_so(tree, tmp_path):
    sink = Recorder()
    out = str(tmp_path / "out")
    with IndexServer(tree) as server:
        crawl(server.base_url, out, sink, stream_bodies=True, stream_body_bytes=32)

    pages = sink.of("crawl.page")
    assert pages
    assert all(len(page["body"]) <= 32 for page in pages)
    assert all(page["truncated"] for page in pages)
    assert all(page["bytes"] > 32 for page in pages)


def test_page_bodies_are_never_written_to_disk(tree, tmp_path):
    out = str(tmp_path / "out")
    with IndexServer(tree) as server:
        crawl(server.base_url, out, Recorder(), stream_bodies=True)

    written = "\n".join(open(os.path.join(out, name), encoding="utf-8").read()
                        for name in os.listdir(out))
    assert "<!DOCTYPE" not in written and "<html" not in written.lower()


# ----------------------------------------------------------------- progress


def test_progress_events_say_the_run_is_still_moving(tmp_path, monkeypatch):
    """A consumer has to be able to tell "finding nothing" from "stuck".

    The watcher is driven directly rather than through a crawl: against a loopback
    server the whole run finishes inside one tick, so a test that waited for the timer
    would be testing how busy the machine is.
    """
    monkeypatch.setattr(CRAWL_MODULE, "STATS_INTERVAL", 0.01)
    sink = Recorder()
    config = CrawlConfig(seeds=["http://x.onion/"], out_dir=str(tmp_path), job_id="t")
    report = CRAWL_MODULE.CrawlReport(str(tmp_path), "t", config.seeds, sink=sink)
    crawler = CRAWL_MODULE.Crawler(
        config, _Pool(), CRAWL_MODULE.Frontier(seeds=config.seeds, max_depth=1),
        fetcher=None, report=report, listing=None,
    )

    async def tick():
        watcher = asyncio.ensure_future(crawler._progress())
        await asyncio.sleep(0.05)
        watcher.cancel()

    asyncio.run(tick())

    progress = sink.of("crawl.progress")
    assert progress
    assert set(progress[0]) >= {"directories", "files", "failures", "queued",
                                "in_flight", "seen", "bytes_fetched", "at"}


# ----------------------------------------------------------------- no consumer


def test_a_crawl_without_a_sink_writes_exactly_what_it_always_did(tree, tmp_path):
    with IndexServer(tree) as server:
        plain = str(tmp_path / "plain")
        watched = str(tmp_path / "watched")
        stats_a, urls_a = crawl(server.base_url, plain)
        stats_b, urls_b = crawl(server.base_url, watched, Recorder())

    assert urls_a == urls_b
    assert stats_a["totals"] == stats_b["totals"]

    def findings(records):
        """What the crawl found, without what is different every time it runs.

        A crawl is concurrent, so neither the order records are written in, which lane
        fetched a given file, nor the second it was discovered in is something two runs
        promise to agree on. What they do promise is the same tree.
        """
        return sorted((r["url"], r["name"], r["size_bytes"], r["depth"], r["parent"])
                      for r in records)

    assert findings(lines(plain, "listing.jsonl")) == findings(lines(watched, "listing.jsonl"))


def test_a_consumer_that_throws_does_not_take_the_crawl_with_it(tree, tmp_path):
    """The crawl is hours of Tor traffic. A broken grep cannot be allowed to cost it."""
    def explode(kind, data):
        raise RuntimeError("this consumer is broken")

    out = str(tmp_path / "out")
    with IndexServer(tree) as server:
        stats, urls = crawl(server.base_url, out, explode)

    assert stats["totals"]["files"] == len(urls) > 0
    assert lines(out, "listing.jsonl")


# ----------------------------------------------------------------- through the bus


def test_a_real_bus_carries_a_real_crawl(tree, tmp_path):
    """The same run, through the object --stream actually hands the crawler."""
    bus = EventBus("job-x", "crawl")
    subscriber = bus.subscribe(since=0)
    out = str(tmp_path / "out")
    with IndexServer(tree) as server:
        crawl(server.base_url, out, bus)

    events, lost, _ = subscriber.take(0)
    assert lost == 0
    files = [json.loads(e.line())["data"] for e in events if e.kind == "crawl.file"]
    assert files == lines(out, "listing.jsonl")
