"""Building the index from a running job's event stream.

The stream's contract is that an event's `data` is byte-for-byte the record the run writes
to disk. That is what these tests lean on: an index built live and one built from the same
run's artefacts must be row-for-row equal, and a live consumer that lost events must be
able to say so and mend itself from the artefacts.
"""

import threading
import time

import pytest

import eventstream
from index import live, store
from index.ingest import Ingester, ingest_dir
from index.rules import load_rules
from indexfixtures import HOST_A, crawl_events, dump, write_crawl_dir

FILES = [("/pub/readme.txt", 120), ("/hr/payroll_2024.xlsx", 5000), ("/it/id_rsa", 2),
         ("/top.bin", None)]


@pytest.fixture
def job(tmp_path):
    return write_crawl_dir(tmp_path, "run1", files=FILES)


def open_index(tmp_path, name):
    return store.Index.open(str(tmp_path / name / "index.db"))


# ----------------------------------------------------------------------- the equality


def test_a_live_index_and_an_offline_one_are_row_for_row_equal(tmp_path, job):
    rules = load_rules()
    with open_index(tmp_path, "offline") as offline:
        ingest_dir(offline, job, rules=rules)
        expected = dump(offline)

    with open_index(tmp_path, "live") as livex:
        ing = Ingester(livex, rules)
        # A live consumer sees files before the directories that contain them, and
        # interleaves the two: shuffle the replay to prove order does not matter.
        events = crawl_events(job)
        stop = [e for e in events if e[0] == "run.stop"]
        rest = [e for e in events if e[0] != "run.stop"]
        rest = rest[len(rest) // 2:] + rest[:len(rest) // 2]
        for kind, data in rest + stop:
            ing.apply("run1", kind, data)
        ing.flush()
        assert dump(livex) == expected


def test_the_equality_holds_for_a_tree_walk_too(tmp_path):
    from index.ingest import ingest_ndjson
    from indexfixtures import tree_entries, write_ndjson
    members = tree_entries([("d", 0, "dir"), ("d/a.txt", 1), ("d/b.pst", 2)])
    url = f"http://{HOST_A}/x/backup.7z"
    nd = write_ndjson(tmp_path / "t.nd", members)
    with open_index(tmp_path, "offline") as offline:
        ingest_ndjson(offline, nd, archive_url=url, job_id="t1", rules=load_rules())
        expected = dump(offline)
    with open_index(tmp_path, "live") as livex:
        ing = Ingester(livex, load_rules())
        ing.apply("t1", "run.start", {"mode": "tree"})
        ing.apply("t1", "tree.open", {"url": url, "format": "7z", "size": 10})
        for m in members:
            ing.apply("t1", "tree.entry", m)
        ing.apply("t1", "tree.done", {"url": url, "entries": 3})
        ing.flush()
        assert dump(livex) == expected


# ----------------------------------------------------------------------- over the wire


class Run:
    """A real EventBus behind a real StreamServer, on a loopback port."""

    def __init__(self, job_id="run1", mode="crawl", token=None, status=None, buffer=10000):
        self.bus = eventstream.EventBus(job_id, mode, buffer=buffer)
        self.server = eventstream.StreamServer(self.bus, "127.0.0.1:0", token=token,
                                               status=status).start()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.server.stop(1.0)

    @property
    def url(self):
        return self.server.url

    def publish(self, events):
        for kind, data in events:
            self.bus(kind, data)


def consume(ix, url, **kwargs):
    """Run the consumer in a thread; returns (thread, result list)."""
    result = []
    t = threading.Thread(target=lambda: result.append(live.follow(
        ix, url, rules=kwargs.pop("rules", None), poll=0.05, **kwargs)), daemon=True)
    t.start()
    return t, result


def test_the_consumer_indexes_a_run_to_its_end_and_returns(tmp_path, job):
    with Run() as run, open_index(tmp_path, "live") as livex:
        run.publish([("run.start", {"mode": "crawl", "target": [f"http://{HOST_A}/"]})])
        t, result = consume(livex, run.url, rules=load_rules())
        time.sleep(0.2)
        run.publish(crawl_events(job))
        t.join(15)
        assert not t.is_alive(), "the consumer did not stop at run.stop"
        summary = result[0]
        assert summary.runs == ["run1"] and summary.gaps == 0
        live_dump = dump(livex)

    with open_index(tmp_path, "offline") as offline:
        ingest_dir(offline, job, rules=load_rules())
        assert live_dump == dump(offline)


def test_a_consumer_that_attaches_late_replays_what_the_run_still_holds(tmp_path, job):
    with Run() as run, open_index(tmp_path, "live") as livex:
        run.publish(crawl_events(job))
        t, result = consume(livex, run.url)
        t.join(15)
        assert result and result[0].runs == ["run1"]
        assert livex.conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 8


def test_a_dropped_connection_resumes_from_the_last_sequence_number(tmp_path, job):
    events = crawl_events(job)
    seen = []
    with Run() as run, open_index(tmp_path, "live") as livex:
        real = live._events

        def flaky(base, selector, token, since):
            # Cut the first connection after four events; later ones go through.
            first = not seen
            for i, event in enumerate(real(base, selector, token, since)):
                seen.append((since, event.get("seq")))
                yield event
                if first and i == 3:
                    raise ConnectionError("link dropped")

        live_events = live._events
        live._events = flaky
        try:
            run.publish(events)
            t, result = consume(livex, run.url)
            t.join(20)
        finally:
            live._events = live_events
        assert result and result[0].reconnects >= 1
        assert livex.conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 8
        assert any(since for since, _ in seen), "the reconnect did not carry a cursor"


def test_events_lost_to_a_slow_consumer_are_reported_and_mended_from_the_artefacts(tmp_path, job):
    """stream.gap says how many were lost; the run's own JSONL is complete."""
    rules = load_rules()
    with open_index(tmp_path, "offline") as offline:
        ingest_dir(offline, job, rules=rules)
        expected = dump(offline)

    with Run(buffer=3, status=lambda: {"job_id": "run1", "out_dir": job}) as run, \
            open_index(tmp_path, "live") as livex:
        run.publish(crawl_events(job))               # ten events into a ring of three
        t, result = consume(livex, run.url, rules=rules)
        t.join(20)
        summary = result[0]
        assert summary.gaps >= 1 and summary.lost >= 1
        assert summary.reconciled == [job], "the gap was not mended from the run's directory"
        assert dump(livex) == expected, "after reconciliation the two indexes still differ"


def test_a_gap_with_no_artefacts_to_mend_it_is_reported_not_hidden(tmp_path, job, caplog):
    with Run(buffer=3) as run, open_index(tmp_path, "live") as livex:
        run.publish(crawl_events(job))
        t, result = consume(livex, run.url)
        t.join(20)
        summary = result[0]
        assert summary.lost >= 1 and summary.reconciled == []
        assert "lost" in caplog.text and "ingest" in caplog.text


def test_reconcile_names_a_directory_explicitly(tmp_path, job):
    with Run(buffer=3) as run, open_index(tmp_path, "live") as livex:
        run.publish(crawl_events(job))
        t, result = consume(livex, run.url, reconcile=job)
        t.join(20)
        assert result[0].reconciled == [job]


def test_a_token_protected_stream_needs_the_token(tmp_path, job):
    with Run(token="s3cret") as run, open_index(tmp_path, "live") as livex:
        run.publish(crawl_events(job))
        with pytest.raises(live.StreamError, match="401|403|token"):
            live.follow(livex, run.url, poll=0.05)
        t, result = consume(livex, run.url, token="s3cret")
        t.join(15)
        assert result and result[0].runs == ["run1"]


def test_a_second_run_on_the_same_port_starts_from_its_own_first_event(tmp_path):
    """A cursor belongs to one run: carrying seq 12 into a new run would skip its first 12."""
    first = write_crawl_dir(tmp_path, "runA", files=[("/a.txt", 1)], at=0)
    second = write_crawl_dir(tmp_path, "runB", files=[("/b.txt", 2)], at=1000)
    with open_index(tmp_path, "live") as livex:
        with Run("runA") as run_a:
            run_a.publish(crawl_events(first))
            t, result = consume(livex, run_a.url)
            t.join(15)
        with Run("runB") as run_b:
            run_b.publish(crawl_events(second))
            t, result = consume(livex, run_b.url)
            t.join(15)
        paths = {r[0] for r in livex.conn.execute("SELECT path FROM entries")}
        assert {"/a.txt", "/b.txt"} <= paths
        assert {r[0] for r in livex.conn.execute("SELECT job_id FROM runs")} == {"runA", "runB"}


def test_an_unreachable_stream_is_an_error(tmp_path):
    with open_index(tmp_path, "live") as livex:
        with pytest.raises(live.StreamError, match="connect|refused|unreachable"):
            live.follow(livex, "http://127.0.0.1:9", poll=0.05, retries=1)


def test_events_of_other_kinds_are_ignored_not_fatal(tmp_path):
    with Run() as run, open_index(tmp_path, "live") as livex:
        run.publish([("crawl.progress", {"directories": 1}), ("something.new", {"x": 1}),
                     ("crawl.file", {"url": "not-a-record"}),
                     ("run.stop", {"mode": "crawl", "stopped_because": "completed"})])
        t, result = consume(livex, run.url)
        t.join(15)
        assert result and livex.conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0
