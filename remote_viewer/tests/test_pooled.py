"""The pooled transport: lane rotation, range splitting, hedging and failover.

The test that matters most here is ``test_serial_load_rotates_across_lanes``. A real
two-hour production run (``rv.log``) issued 7,540 range requests and sent every single one
of them over the same circuit, because ``tor.Transport`` hands circuits out of a
``LifoQueue`` and a sequential caller always gets the top of the stack back. Nothing in
the suite could see it: the shared ``transport`` fixture is single-circuit, and the one
test that builds four acquires them without ever releasing one. That shape is pinned here.

Lanes are direct connections rather than SOCKS proxies, which changes nothing about the
pooling, splitting or retry logic and lets all of it run offline against
``tests/rangeserver.py``.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from rvtree import archive
from rvtree.transport import HttpRangeFile, PooledTransport, split_spans
from rvtree.transport.pooled import (
    HEDGE_MAX_BYTES,
    MIN_SPLIT,
    Endpoint,
    Lane,
    LanePool,
    parse_endpoint,
)
from rvtree.transport.tor import RangeNotHonoured, TransportError


def _serve(fixtures, **kwargs):
    from rangeserver import RangeServer

    return RangeServer(fixtures, **kwargs)


def _ranges(server):
    """The absolute ranges the server was actually asked for."""
    return [r for r in server.requests if r.startswith("bytes=") and not r.startswith("bytes=-")]


# ----------------------------------------------------------------- lane rotation


def test_serial_load_rotates_across_lanes(pooled, server):
    """The rv.log regression: a sequential caller must not be pinned to one lane.

    Forty small reads, one at a time, over eight lanes. A LIFO pool answers all forty on
    the same lane; a round-robin cursor spreads them.
    """
    url = f"{server.base}/test.tar"
    for i in range(40):
        pooled.get_range(url, i * 4096, i * 4096 + 4095)

    counts = {lane: n for lane, n in pooled._pool.counts().items() if n}
    assert len(counts) >= 6, f"only {len(counts)} of 8 lanes were ever used: {counts}"
    busiest = max(counts.values())
    assert busiest <= sum(counts.values()) * 0.5, f"one lane served most of the work: {counts}"


def test_all_lanes_dead_still_hands_one_back():
    """OnionAccelerator's contract: a stale retry beats giving up, so never return None."""
    endpoints = [Endpoint("a", None, "ua"), Endpoint("b", None, "ua")]
    pool = LanePool([Lane(i, e, None) for i, e in enumerate(endpoints)])
    for endpoint in endpoints:
        pool.mark_dead(endpoint)
    lane = pool.acquire()
    assert lane is not None


def test_acquire_avoids_the_endpoint_that_just_failed():
    endpoints = [Endpoint("a", None, "ua"), Endpoint("b", None, "ua")]
    pool = LanePool([Lane(i, e, None) for i, e in enumerate(endpoints)])
    first = pool.acquire()
    pool.release(first)
    assert pool.acquire(exclude=first.endpoint).endpoint is not first.endpoint


def test_try_acquire_returns_none_when_every_lane_is_busy():
    """The property hedging and splitting lean on to stay opportunistic."""
    pool = LanePool([Lane(0, Endpoint("a", None, "ua"), None)])
    held = pool.acquire()
    assert pool.try_acquire() is None
    pool.release(held)
    assert pool.try_acquire() is not None


# ----------------------------------------------------------------- splitting


@pytest.mark.parametrize(
    "want,lanes,expected",
    [
        (4095, 8, 1),           # below the floor: never split
        (MIN_SPLIT, 8, 1),      # exactly one chunk's worth
        (MIN_SPLIT * 3 + 512, 8, 3),
        (MIN_SPLIT * 8, 8, 8),
        (MIN_SPLIT * 32, 8, 8),  # capped by lanes, not by size
        (MIN_SPLIT * 4, 1, 1),   # capped by lanes when there is only one
    ],
)
def test_split_uses_onionaccelerators_formula(want, lanes, expected):
    """``n = min(lanes, max(1, want // MIN_SPLIT))``, straight out of partial_download_file."""
    spans = split_spans(0, want - 1, lanes)
    assert len(spans) == expected


def test_split_spans_are_contiguous_and_cover_the_range():
    spans = split_spans(100, 100 + MIN_SPLIT * 8 - 1, 8)
    assert spans[0][0] == 100
    assert spans[-1][1] == 100 + MIN_SPLIT * 8 - 1
    assert all(spans[i][1] + 1 == spans[i + 1][0] for i in range(len(spans) - 1))


def test_a_large_range_is_split_across_lanes(pooled, server):
    url = f"{server.base}/test.multiblock.tar.xz"
    server.requests.clear()
    blob = pooled.get_range(url, 0, 4 * MIN_SPLIT - 1)
    assert len(blob) == 4 * MIN_SPLIT
    assert len(_ranges(server)) == 4, _ranges(server)


def test_a_split_range_is_byte_identical_to_a_serial_one(pooled, transport, server):
    url = f"{server.base}/test.multiblock.tar.xz"
    want = (0, 6 * MIN_SPLIT + 4321)
    assert pooled.get_range(url, *want) == transport.get_range(url, *want)


def test_small_ranges_are_never_split(pooled, server):
    """The rv.log workload is 4 KiB reads; splitting them would only add round trips."""
    server.requests.clear()
    pooled.get_range(f"{server.base}/test.tar", 0, 4095)
    assert len({r for r in _ranges(server)}) == 1


# ----------------------------------------------------------------- hedging


def test_small_ranges_are_hedged_across_endpoints(server):
    """A small range enlists several endpoints, and the first answer is taken.

    Asserted on lanes enlisted rather than on requests observed. The copies are checked
    out synchronously before any of them is submitted, whereas how many actually reach the
    server is a race: against a loopback server the winner often lands before a sibling is
    scheduled, and the executor then cancels it. That cancellation is exactly what should
    happen in production — it just makes request counting a coin toss here.
    """
    t = PooledTransport(endpoints=["direct"] * 4, hedge=3)
    try:
        before = sum(t._pool.counts().values())
        server.requests.clear()
        blob = t.get_range(f"{server.base}/test.tar", 0, 4095)
        assert len(blob) == 4096
        assert sum(t._pool.counts().values()) - before == 3
        assert set(_ranges(server)) == {"bytes=0-4095"}
    finally:
        t.close()


def test_hedging_needs_more_than_one_daemon(server):
    """Lanes on one daemon queue behind one process, so racing them races nothing.

    Measured against a real Tor client: eight lanes on a single daemon made a listing
    *slower* than one, because every duplicate paid a fresh circuit setup to reach the
    same bottleneck.
    """
    t = PooledTransport(endpoints=None, circuits_per_endpoint=8, hedge=3)
    try:
        assert t.lanes == 8 and t.daemons == 1
        server.requests.clear()
        t.get_range(f"{server.base}/test.tar", 0, 4095)
        assert len(_ranges(server)) == 1
    finally:
        t.close()


def test_hedged_bytes_are_the_right_bytes(server, fixtures):
    t = PooledTransport(endpoints=["direct"] * 4, hedge=3)
    try:
        blob = t.get_range(f"{server.base}/test.tar", 512, 512 + 2047)
        with open(f"{fixtures}/test.tar", "rb") as fh:
            fh.seek(512)
            assert blob == fh.read(2048)
    finally:
        t.close()


def test_bulk_ranges_are_never_hedged(pooled, server):
    """A duplicated 4 KiB header is free; a duplicated multi-megabyte block is not."""
    server.requests.clear()
    pooled.get_range(f"{server.base}/test.multiblock.tar.xz", 0, 2 * MIN_SPLIT - 1)
    asked = _ranges(server)
    assert len(asked) == len(set(asked)), f"a bulk read was duplicated: {asked}"


def test_hedging_disables_itself_when_the_pool_is_busy(server, fixtures):
    """Spare capacity only. Under parallel walkers every lane is taken, and that is the
    right place for the budget to go: a walker multiplies throughput, a hedge trims a tail.
    """
    t = PooledTransport(endpoints=["direct"] * 4, hedge=3)
    try:
        held = [t._pool.acquire() for _ in range(3)]
        server.requests.clear()
        t.get_range(f"{server.base}/test.tar", 0, 4095)
        assert len(_ranges(server)) == 1
        for lane in held:
            t._pool.release(lane)
    finally:
        t.close()


def test_hedge_ceiling_is_below_the_split_floor():
    """The three regimes must not overlap, or a read could be hedged *and* split."""
    assert HEDGE_MAX_BYTES < MIN_SPLIT


# ----------------------------------------------------------------- batch fetch


def test_get_ranges_returns_spans_in_the_order_asked(pooled, server, fixtures):
    url = f"{server.base}/test.tar"
    spans = [(i * 100_000, i * 100_000 + 999) for i in range(12)]
    blobs = pooled.get_ranges(url, spans)
    assert len(blobs) == 12
    with open(f"{fixtures}/test.tar", "rb") as fh:
        for blob, (lo, hi) in zip(blobs, spans):
            fh.seek(lo)
            assert blob == fh.read(hi - lo + 1)


# ----------------------------------------------------------------- range discipline


@pytest.mark.parametrize(
    "name", ["test.zip", "test.7z", "test.multiblock.tar.xz", "test.tar", "test.rar5.rar"]
)
def test_never_sends_a_suffix_range(pooled, server, name):
    arc = archive.open_archive(pooled, f"{server.base}/{name}")
    list(archive.list_archive(arc, segments=4))
    suffixes = [r for r in server.requests if r.startswith("bytes=-")]
    assert suffixes == [], f"emitted suffix ranges: {suffixes}"


def test_a_200_to_a_sub_range_aborts_before_the_body(fixtures):
    """A server ignoring Range means the whole entity is coming — on every span at once."""
    with _serve(fixtures, ignore_ranges=True) as srv:
        t = PooledTransport(endpoints=None, circuits_per_endpoint=8, retries=1)
        try:
            with pytest.raises(RangeNotHonoured):
                fp = HttpRangeFile(t, f"{srv.base}/test.zip")
                fp.pread(0, 16)
        finally:
            t.close()


def test_a_wrong_content_range_is_rejected(pooled, server, monkeypatch):
    from rvtree.transport import pooled as pooled_mod

    def liar(value, start, end):
        raise TransportError("Content-Range did not match")

    monkeypatch.setattr(pooled_mod, "_validate_content_range", liar)
    with pytest.raises(TransportError):
        pooled.get_range(f"{server.base}/test.tar", 0, 4 * MIN_SPLIT - 1)


# ----------------------------------------------------------------- endpoints


def test_endpoint_specs_are_parsed_the_way_onionaccelerator_writes_them():
    assert parse_endpoint("127.0.0.1:5000") == "socks5h://127.0.0.1:5000"
    assert parse_endpoint("[::1]:9050") == "socks5h://[::1]:9050"
    assert parse_endpoint("socks5h://host:9050") == "socks5h://host:9050"
    for direct in (None, "", "none", "direct"):
        assert parse_endpoint(direct) is None


def test_socks5_is_refused_because_it_leaks_dns():
    with pytest.raises(TransportError, match="socks5h"):
        parse_endpoint("socks5://127.0.0.1:9150")
    with pytest.raises(TransportError, match="socks5h"):
        PooledTransport(endpoints=["socks5://127.0.0.1:9150"])


def test_a_malformed_endpoint_says_so():
    with pytest.raises(TransportError, match="host:port"):
        parse_endpoint("not-an-endpoint")


def test_each_endpoint_keeps_one_user_agent_for_the_whole_run():
    """One daemon is one identity to the far end; rotating mid-circuit is a fingerprint."""
    t = PooledTransport(endpoints=["direct", "direct"], user_agents=["UA-A", "UA-B"])
    try:
        assert t.daemons == 2
        agents = {lane.endpoint.user_agent for lane in t._pool._lanes}
        assert agents == {"UA-A", "UA-B"}
        for lane in t._pool._lanes:
            assert lane.circuit.session.headers["User-Agent"] == lane.endpoint.user_agent
    finally:
        t.close()


def test_ipv6_endpoints_keep_their_brackets_through_circuit_isolation():
    """urlsplit drops them, and 'rv0:abcd@::1:9050' is an address no SOCKS client parses."""
    t = PooledTransport(endpoints=["[::1]:9050"])
    try:
        assert "[::1]:9050" in t._pool._lanes[0].circuit.session.proxies["http"]
    finally:
        t.close()


# ----------------------------------------------------------------- failover


def test_a_dead_endpoint_is_parked_and_the_retry_lands_elsewhere(server):
    """One endpoint points at a closed port; the fetch must still succeed on the other.

    Hedging is off here so the request takes the plain path and the failover is the thing
    under test rather than a race between two copies.
    """
    t = PooledTransport(endpoints=["127.0.0.1:1", "direct"], retries=3, hedge=1)
    try:
        assert len(t.get_range(f"{server.base}/test.tar", 0, 511)) == 512
        assert "socks5h://127.0.0.1:1" in t._pool._dead
    finally:
        t.close()


def test_hedging_still_answers_when_one_endpoint_is_dead(server):
    t = PooledTransport(endpoints=["127.0.0.1:1", "direct"], retries=3, hedge=2)
    try:
        assert len(t.get_range(f"{server.base}/test.tar", 0, 511)) == 512
    finally:
        t.close()


# ----------------------------------------------------------------- accounting


def test_throughput_is_wall_clock_not_the_sum_of_each_request(pooled, server):
    """``SingleBlockWarning`` turns this number into "about N days".

    Summing per-request seconds across parallel lanes reports several times more transfer
    time than actually elapsed, so the estimate that decides whether a caller starts a
    multi-hour job would be badly wrong. Busy-time accounting cannot exceed the wall clock,
    which is the invariant asserted here — and the one the old formula fails.
    """
    url = f"{server.base}/test.multiblock.tar.xz"
    start = time.monotonic()
    pooled.get_range(url, 0, 8 * MIN_SPLIT - 1)
    elapsed = time.monotonic() - start
    assert pooled.seconds_transferring <= elapsed + 0.05
    assert pooled.throughput > 0


def test_a_capability_probe_is_not_counted_as_a_range_request(pooled, server):
    """``requests_made`` is what the listing summary is stated in; keep it to real ranges."""
    pooled.head_like(f"{server.base}/test.zip")
    assert pooled.requests_made == 0


# ----------------------------------------------------------------- concurrency


def test_prefetch_over_the_fetch_pool_does_not_deadlock(pooled, server):
    """The xz prefetch executor submits work that submits work to the transport's pool.

    Two distinct executors and one direction, so there is no cycle — but this is the
    arrangement that would hang if a lane were ever held across a wait, so it is pinned.
    """
    arc = archive.open_archive(pooled, f"{server.base}/test.multiblock.tar.xz")
    with ThreadPoolExecutor(max_workers=1) as runner:
        future = runner.submit(lambda: list(archive.list_archive(arc, circuits=4)))
        entries = future.result(timeout=120)
    assert len(entries) == 14


def test_many_threads_can_share_one_pool(pooled, server, fixtures):
    url = f"{server.base}/test.tar"
    errors: list[Exception] = []

    def read(i):
        try:
            assert len(pooled.get_range(url, i * 8192, i * 8192 + 8191)) == 8192
        except Exception as exc:  # noqa: BLE001 - the assertion is that there are none
            errors.append(exc)

    threads = [threading.Thread(target=read, args=(i,)) for i in range(24)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
