"""The event bus and the query filters.

Two properties matter more than the rest and get most of the file. A run publishing to
a wedged consumer must not slow down, because the consumer is a convenience and the
crawl is the job. And a consumer that does fall behind must be told exactly how far,
because a keyword index with an unannounced hole in it is the failure mode this whole
feature exists to avoid.
"""

import json
import threading
import time

import pytest

from eventstream import bus as bus_mod
from eventstream.bus import EventBus, Event
from eventstream.filters import LINE, NDJSON, Selector, SelectorError


@pytest.fixture
def bus():
    return EventBus("job-1", "crawl", buffer=100, queue=50)


def drain(subscriber):
    events, lost, closed = subscriber.take(0)
    return [e.kind for e in events], lost, closed


# ----------------------------------------------------------------- sequencing


def test_sequence_numbers_start_at_one_and_never_repeat(bus):
    events = [bus("crawl.file", {"url": f"u{i}"}) for i in range(5)]
    assert [e.seq for e in events] == [1, 2, 3, 4, 5]


def test_a_bus_is_itself_a_sink(bus):
    """Producers take 'something callable', which is what keeps them independent of us."""
    sink = bus                       # exactly what crawl_mode passes to crawler.crawl
    sink("crawl.file", {"url": "http://x.onion/a"})
    assert bus.seq == 1


def test_the_record_is_published_by_reference_not_by_copy(bus):
    """The stream and listing.jsonl must be describing the same object, not two of them."""
    record = {"url": "http://x.onion/a", "size_bytes": 12}
    subscriber = bus.subscribe()
    bus("crawl.file", record)
    events, _, _ = subscriber.take(0)
    assert events[0].data is record


def test_publishing_with_no_consumers_costs_nothing_and_still_counts(bus):
    bus("crawl.file", {"url": "u"})
    assert bus.published == 1 and bus.subscribers == 0


# ----------------------------------------------------------------- replay


def test_since_replays_what_a_late_consumer_missed(bus):
    for i in range(5):
        bus("crawl.file", {"url": f"u{i}"})
    subscriber = bus.subscribe(since=2)
    events, lost, _ = subscriber.take(0)
    assert [e.seq for e in events] == [3, 4, 5]
    assert lost == 0


def test_subscribing_without_since_starts_from_now(bus):
    bus("crawl.file", {"url": "old"})
    subscriber = bus.subscribe()
    assert drain(subscriber) == ([], 0, False)
    bus("crawl.file", {"url": "new"})
    events, _, _ = subscriber.take(0)
    assert [e.data["url"] for e in events] == ["new"]


def test_since_zero_takes_everything_still_held(bus):
    for i in range(3):
        bus("crawl.file", {"url": f"u{i}"})
    events, lost, _ = bus.subscribe(since=0).take(0)
    assert len(events) == 3 and lost == 0


def test_asking_for_events_the_ring_has_evicted_is_reported_as_loss():
    small = EventBus("job", "crawl", buffer=5)
    for i in range(20):
        small("crawl.file", {"url": f"u{i}"})
    events, lost, _ = small.subscribe(since=0).take(0)
    # Fifteen fell out of the ring before anyone asked for them. Saying so is the point.
    assert lost == 15
    assert [e.seq for e in events] == [16, 17, 18, 19, 20]


# ----------------------------------------------------------------- backpressure


def test_a_slow_consumer_loses_the_oldest_events_and_is_told_how_many():
    slow = EventBus("job", "crawl", queue=3)
    subscriber = slow.subscribe()
    for i in range(10):
        slow("crawl.file", {"url": f"u{i}"})

    events, lost, _ = subscriber.take(0)
    assert lost == 7
    assert [e.data["url"] for e in events] == ["u7", "u8", "u9"]


def test_the_loss_counter_resets_once_it_has_been_reported():
    slow = EventBus("job", "crawl", queue=2)
    subscriber = slow.subscribe()
    for i in range(6):
        slow("crawl.file", {"url": f"u{i}"})
    assert subscriber.take(0)[1] == 4
    slow("crawl.file", {"url": "next"})
    # A gap is announced once, for the events that were actually lost -- not repeated
    # on every subsequent read, which would make a consumer distrust a stream that is
    # now perfectly healthy.
    assert subscriber.take(0)[1] == 0


def test_publishing_to_a_consumer_that_never_reads_stays_fast():
    """The crawl is on the other end of this call. It cannot wait for a grep."""
    slow = EventBus("job", "crawl", queue=10)
    slow.subscribe()                                   # attaches and never takes
    for i in range(1000):                              # fill it far past its depth
        slow("crawl.file", {"url": f"u{i}"})

    started = time.perf_counter()
    for i in range(1000):
        slow("crawl.file", {"url": f"v{i}"})
    per_call = (time.perf_counter() - started) / 1000
    assert per_call < 0.001, f"{per_call * 1e6:.0f}us per publish into a wedged consumer"


def test_one_slow_consumer_does_not_cost_a_fast_one_anything():
    shared = EventBus("job", "crawl", queue=5)
    slow, fast = shared.subscribe(), shared.subscribe()
    for i in range(20):
        shared("crawl.file", {"url": f"u{i}"})
        fast.take(0)

    assert slow.take(0)[1] == 15
    shared("crawl.file", {"url": "last"})
    events, lost, _ = fast.take(0)
    assert lost == 0 and [e.data["url"] for e in events] == ["last"]


# ----------------------------------------------------------------- threading


def test_concurrent_publishers_produce_one_unbroken_sequence(bus):
    """rvtree walks an archive on several threads; the crawl publishes from its loop."""
    def publish():
        for i in range(200):
            bus("tree.entry", {"path": f"p{i}"})

    threads = [threading.Thread(target=publish) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert bus.seq == 1600 and bus.published == 1600


def test_a_waiting_consumer_wakes_when_something_is_published(bus):
    subscriber = bus.subscribe()
    woke = []

    def wait():
        woke.append(subscriber.take(2.0))

    thread = threading.Thread(target=wait)
    thread.start()
    time.sleep(0.05)
    bus("crawl.file", {"url": "u"})
    thread.join(2.0)
    assert woke and [e.data["url"] for e in woke[0][0]] == ["u"]


def test_closing_the_bus_releases_everyone_waiting(bus):
    subscriber = bus.subscribe()
    released = []

    def wait():
        released.append(subscriber.take(5.0))

    thread = threading.Thread(target=wait)
    thread.start()
    time.sleep(0.05)
    bus.close()
    thread.join(2.0)
    assert released and released[0][2] is True


def test_unsubscribing_stops_delivery(bus):
    subscriber = bus.subscribe()
    subscriber.close()
    bus("crawl.file", {"url": "u"})
    assert bus.subscribers == 0


# ----------------------------------------------------------------- the wire form


def test_the_line_carries_the_envelope_and_the_record(bus):
    event = bus("crawl.file", {"url": "http://x.onion/á b.txt", "size_bytes": 3})
    payload = json.loads(event.line())
    assert payload == {"seq": 1, "ts": event.ts, "run": "job-1", "mode": "crawl",
                       "kind": "crawl.file",
                       "data": {"url": "http://x.onion/á b.txt", "size_bytes": 3}}


def test_the_line_is_built_once_and_shared(bus):
    """A dozen consumers must not mean a dozen json.dumps of the same record."""
    event = bus("crawl.file", {"url": "u"})
    assert event.line() is event.line()


def test_a_record_that_json_cannot_reach_still_serialises(bus):
    """default=str, matching what report.py writes to disk: a stream cannot raise."""
    event = bus("crawl.fail", {"url": "u", "error": ValueError("nope")})
    assert "nope" in json.loads(event.line())["data"]["error"]


def test_the_locator_is_whatever_names_the_thing(bus):
    assert bus("crawl.file", {"url": "u"}).locator() == "u"
    assert bus("tree.entry", {"path": "p"}).locator() == "p"
    assert bus("crawl.progress", {"queued": 3}).locator() is None


# ----------------------------------------------------------------- selectors


def event(kind="crawl.file", **data):
    return Event(1, "2026-01-01T00:00:00Z", "job", "crawl", kind, data)


def test_a_bare_selector_takes_everything():
    assert Selector().wants(event(kind="tree.entry", path="p"))


def test_kinds_selects_a_family_by_prefix():
    selector = Selector.from_query({"kinds": ["crawl"]})
    assert selector.wants(event("crawl.file", url="u"))
    assert selector.wants(event("crawl.dir", url="u"))
    assert not selector.wants(event("tree.entry", path="p"))


def test_kinds_selects_one_kind_exactly():
    selector = Selector.from_query({"kinds": ["crawl.file,tree.entry"]})
    assert selector.wants(event("crawl.file", url="u"))
    assert not selector.wants(event("crawl.dir", url="u"))


def test_match_is_applied_to_the_whole_line_not_only_the_url():
    selector = Selector.from_query({"match": ["(?i)acme"]})
    assert selector.wants(event("crawl.dir", url="u", title="Acme Ltd backups"))
    assert not selector.wants(event("crawl.dir", url="u", title="holiday photos"))


def test_control_events_are_never_filtered_out():
    """A consumer that asked for files still has to hear that it lost some."""
    selector = Selector.from_query({"kinds": ["crawl.file"], "match": ["nothing-matches"]})
    assert selector.wants(event("stream.gap", lost=4))


def test_shape_line_renders_only_the_locator():
    selector = Selector.from_query({"shape": ["line"]})
    assert selector.render(event("crawl.file", url="http://x.onion/a")) == "http://x.onion/a"
    assert selector.render(event("crawl.progress", queued=2)) is None


def test_shape_ndjson_is_the_default_and_renders_the_line():
    assert Selector().shape == NDJSON
    assert json.loads(Selector().render(event(url="u")))["kind"] == "crawl.file"


@pytest.mark.parametrize("query,message", [
    ({"kind": ["crawl"]}, "unknown parameter"),
    ({"match": ["("]}, "not a valid regex"),
    ({"since": ["soon"]}, "sequence number"),
    ({"since": ["-1"]}, "cannot be negative"),
    ({"shape": ["yaml"]}, "shape must be one of"),
    ({"heartbeat": ["often"]}, "must be seconds"),
    ({"kinds": [""]}, "omit it"),
    ({"since": ["1", "2"]}, "give it once"),
])
def test_a_bad_query_names_what_is_wrong_with_it(query, message):
    """The reader of this message is writing a client against a live run, mid-incident."""
    with pytest.raises(SelectorError, match=message):
        Selector.from_query(query)


def test_a_silly_heartbeat_is_clamped_rather_than_refused():
    assert Selector.from_query({"heartbeat": ["0.001"]}).heartbeat == 1.0
    assert Selector.from_query({"heartbeat": ["0"]}).heartbeat == 0


def test_the_filter_describes_itself_for_the_hello_line():
    described = Selector.from_query({"kinds": ["crawl"], "match": ["x"]}).describe()
    assert described["kinds"] == ["crawl"] and described["match"] == "x"


# ----------------------------------------------------------------- introspection


def test_status_reports_the_cursor_the_kinds_and_the_consumers(bus):
    bus.subscribe()
    bus("crawl.file", {"url": "u"})
    bus("crawl.file", {"url": "v"})
    bus("crawl.dir", {"url": "d"})
    stats = bus.stats()
    assert stats["seq"] == 3
    assert stats["kinds"] == {"crawl.file": 2, "crawl.dir": 1}
    assert len(stats["consumers"]) == 1


def test_drained_says_whether_everyone_has_been_handed_everything(bus):
    subscriber = bus.subscribe()
    bus("crawl.file", {"url": "u"})
    assert not bus.drained()
    subscriber.take(0)
    assert bus.drained()
