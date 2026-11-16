"""The piece-based download engine: resume, validators, slow lanes, and what it proves.

Everything here runs against tests/faultserver.py on loopback; the SOCKS tests add
tests/socks5server.py. Nothing touches a real network, and no address in this file is
anything but 127.0.0.1.

Written before the engine, from the Phase 1 acceptance list in docs/ROADMAP.md:

  * SIGKILL mid-transfer, rerun, and the file's SHA-256 equals the source's
  * one lane throttled to a tenth of the others completes under a tenth of the pieces
  * an ETag change mid-transfer never yields a mixed file
  * bounded memory (the 2 GiB, eight-lane test is marked `slow`)
and the five faults the roadmap names: a stall, a dropped connection, a changing ETag,
a 200 answering a Range, and an HTML error page served with 200.
"""

import hashlib
import json
import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest

from faultserver import (BytesSource, Drop, FaultServer, Html200, PatternSource, Stall,
                         Status200)
from oacore import engine
from socks5server import Socks5Server

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIECE = 256 * 1024
NAME = "dump.bin"


def sha(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def cfg(**overrides):
    base = dict(piece_size=PIECE, retries=2, timeout=(3, 3))
    base.update(overrides)
    return engine.EngineConfig(**base)


def fetch(server, out, *, lanes=4, endpoints=None, sink=None, user_agents=None, **config):
    """Download NAME from `server` to `out` over direct lanes; return the result."""
    downloader = engine.Downloader(endpoints if endpoints is not None else [None] * lanes,
                                   config=cfg(**config), sink=sink,
                                   user_agents=user_agents or ["test-agent"], job_id="job-1")
    try:
        return downloader.download(server.url(NAME), str(out))
    finally:
        downloader.close()


@pytest.fixture
def source():
    return PatternSource(12 * PIECE + 12345, seed=7)        # last piece is short


@pytest.fixture
def server(source):
    with FaultServer({NAME: source}) as srv:
        yield srv


def side_files(out):
    """Everything in the output directory, scratch files and all: the engine's temp files
    are named .oa-XXXX.part, so filtering by the target's name would hide exactly the
    litter these checks exist to find."""
    return sorted(os.listdir(os.path.dirname(str(out))))


# --------------------------------------------------------------------- planning


def test_pieces_cover_the_file_exactly_once_and_the_last_one_is_short():
    pieces = engine.plan_pieces(10 * 1024 + 5, 4096)
    assert pieces == [(0, 4095), (4096, 8191), (8192, 10244)]
    assert engine.plan_pieces(4096, 4096) == [(0, 4095)]
    assert engine.plan_pieces(1, 4096) == [(0, 0)]


# --------------------------------------------------------------------- happy path


def test_a_download_matches_the_source_and_leaves_only_the_file_and_its_record(
        tmp_path, server, source):
    out = tmp_path / "got.bin"
    result = fetch(server, out)

    assert result.ok and sha(out) == source.sha256()
    assert os.path.getsize(out) == source.size
    # The scratch file and the journal are gone; the custody record stays.
    assert side_files(out) == ["got.bin", "got.bin.oa.json"]

    record = json.load(open(str(out) + ".oa.json"))
    assert record["url"] == server.url(NAME)
    assert record["size"] == source.size
    assert record["sha256"] == source.sha256()
    assert record["etag"] == '"v1"' and record["last_modified"]
    assert record["job_id"] == "job-1"
    assert record["started_at"] <= record["finished_at"]
    assert sum(lane["pieces"] for lane in record["lanes"]) == 13


def test_every_piece_is_a_ranged_request_carrying_if_range(tmp_path, server):
    fetch(server, tmp_path / "got.bin")
    ranged = server.ranged()
    assert {r.start for r in ranged} >= {i * PIECE for i in range(13)}
    assert all(r.if_range == '"v1"' for r in ranged)


def test_a_file_that_fits_in_one_piece_is_one_request(tmp_path):
    with FaultServer({NAME: BytesSource(b"tiny" * 100)}) as server:
        out = tmp_path / "got.bin"
        assert fetch(server, out).ok
        assert open(out, "rb").read() == b"tiny" * 100


# --------------------------------------------------------------------- resume


KILL_SCRIPT = textwrap.dedent("""
    import sys
    sys.path.insert(0, {root!r})
    from oacore import engine
    config = engine.EngineConfig(piece_size={piece}, retries=2, timeout=(3, 3))
    d = engine.Downloader([None] * 4, config=config, user_agents=["killme"])
    d.download({url!r}, {out!r})
""")


def test_sigkill_mid_transfer_then_rerun_yields_the_sources_hash(tmp_path, source):
    """The acceptance check, with a real SIGKILL and a real second process."""
    out = tmp_path / "got.bin"
    with FaultServer({NAME: source}) as server:
        server.throttle = lambda ua: 400_000          # ~1.6 MB/s over four lanes
        script = KILL_SCRIPT.format(root=ROOT, piece=PIECE, url=server.url(NAME), out=str(out))
        proc = subprocess.Popen([sys.executable, "-c", script])
        journal = str(out) + ".oa-state"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if os.path.exists(journal) and open(journal).read().count('"done"') >= 4:
                break
            time.sleep(0.02)
        else:
            proc.kill()
            pytest.fail("the engine never journalled four pieces")
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)
        assert not os.path.exists(out), "a killed run must not leave a finished-looking file"

        done_before = open(journal).read().count('"done"')
        server.throttle = lambda ua: None
        mark = len(server.log)
        result = fetch(server, out)

    assert result.ok and sha(out) == source.sha256()
    assert result.pieces_resumed >= done_before - 1
    refetched = {r.start for r in server.log[mark:] if r.start is not None}
    assert len(refetched) == 13 - result.pieces_resumed, "a finished piece was fetched again"


def test_a_rerun_re_verifies_finished_pieces_by_hash(tmp_path, server, source):
    out = tmp_path / "got.bin"
    server.faults.append(lambda r: Drop(after=0) if r.start == 6 * PIECE else None)
    first = fetch(server, out)
    assert not first.ok
    done = first.pieces_done

    with open(str(out) + ".oa-part", "r+b") as part:      # rot one finished piece
        part.seek(0)
        part.write(b"\x00\xff" * 16)
    server.faults.clear()
    mark = len(server.log)
    second = fetch(server, out)

    assert second.ok and sha(out) == source.sha256()
    assert second.pieces_resumed == done - 1
    assert 0 in {r.start for r in server.log[mark:] if r.start is not None}


def test_fresh_discards_the_state_and_starts_over(tmp_path, server, source):
    out = tmp_path / "got.bin"
    server.faults.append(lambda r: Drop(after=0) if r.start == 6 * PIECE else None)
    assert not fetch(server, out).ok
    server.faults.clear()
    mark = len(server.log)
    result = fetch(server, out, fresh=True)
    assert result.ok and result.pieces_resumed == 0
    assert {r.start for r in server.log[mark:] if r.start is not None} == {
        i * PIECE for i in range(13)}


# --------------------------------------------------------------------- failure is partial


def test_a_piece_that_exhausts_its_retries_leaves_the_file_resumable(tmp_path, server, source):
    out = tmp_path / "got.bin"
    server.faults.append(lambda r: Drop(after=1000) if r.start == 3 * PIECE else None)
    result = fetch(server, out, retries=2)

    assert not result.ok
    assert result.reason
    assert os.path.exists(str(out) + ".oa-part") and os.path.exists(str(out) + ".oa-state")
    assert not os.path.exists(out)
    assert result.pieces_done > 0

    server.faults.clear()
    again = fetch(server, out)
    assert again.ok and sha(out) == source.sha256()
    assert again.pieces_resumed >= result.pieces_done


def test_a_failure_lets_the_pieces_already_in_flight_finish(tmp_path, source):
    """Giving up on one piece must not throw away the others' nearly finished work.

    Over Tor a piece takes tens of seconds, so aborting eight in-flight pieces the moment
    one runs out of retries would discard up to eight pieces on every failure.
    """
    with FaultServer({NAME: source}) as server:
        server.throttle = lambda ua: 600_000          # pieces 0-2 take ~0.4 s each
        server.faults.append(lambda r: Drop(after=0) if r.start == 3 * PIECE else None)
        out = tmp_path / "got.bin"
        started = time.monotonic()
        result = fetch(server, out, retries=2, drain_grace=10)
        elapsed = time.monotonic() - started

        assert not result.ok and "piece 3" in result.reason
        assert result.pieces_done >= 3, "in-flight pieces were abandoned"
        assert elapsed < 9, "the drain is bounded by what was in flight, not by the grace"
        # And what they finished is journalled, so the rerun does not fetch it again.
        server.faults.clear()
        server.throttle = lambda ua: None
        mark = len(server.log)
        again = fetch(server, out)
    assert again.ok and again.pieces_resumed >= 3
    assert sha(out) == source.sha256()
    assert {r.start for r in server.log[mark:] if r.start is not None}.isdisjoint(
        {i * PIECE for i in range(3)})


def test_the_drain_after_a_failure_gives_up_on_a_stalled_piece(tmp_path, source):
    """The grace is a ceiling: a lane that has stalled is abandoned, not waited for."""
    with FaultServer({NAME: source}) as server:
        server.faults.append(lambda r: Drop(after=0) if r.start == 3 * PIECE else (
            Stall(after=10, seconds=30) if r.start == 0 else None))
        out = tmp_path / "got.bin"
        started = time.monotonic()
        result = fetch(server, out, retries=2, drain_grace=1.5, timeout=(5, 20))
        elapsed = time.monotonic() - started
    assert not result.ok and elapsed < 8


def test_a_failed_run_never_touches_an_existing_finished_file(tmp_path, server):
    out = tmp_path / "got.bin"
    out.write_bytes(b"the good file from an earlier run")
    server.faults.append(lambda r: Drop(after=0) if r.start is not None else None)
    assert not fetch(server, out, retries=1).ok
    assert out.read_bytes() == b"the good file from an earlier run"


def test_a_stalled_piece_times_out_and_is_retried(tmp_path, server, source):
    out = tmp_path / "got.bin"
    stalled = []

    def stall_once(req):
        if req.start == 2 * PIECE and not stalled:
            stalled.append(req)
            return Stall(after=1000, seconds=6)

    server.faults.append(stall_once)
    result = fetch(server, out, timeout=(2, 1))
    assert result.ok and sha(out) == source.sha256()
    assert stalled


def test_an_html_error_page_served_with_200_is_never_written_into_the_file(
        tmp_path, server, source):
    out = tmp_path / "got.bin"
    served = []

    def busy_once(req):
        if req.start == 4 * PIECE and not served:
            served.append(req)
            return Html200()

    server.faults.append(busy_once)
    result = fetch(server, out)
    assert result.ok and sha(out) == source.sha256()
    assert served and b"<html" not in out.read_bytes()


# --------------------------------------------------------------------- validators


def test_an_etag_change_mid_transfer_never_produces_a_mixed_file(tmp_path, server, source):
    """If-Range makes a conforming server answer 200 with the new entity: restart once."""
    out = tmp_path / "got.bin"
    changed = PatternSource(source.size, seed=99)
    flipped = []

    def flip_after_four(req):
        if req.start is not None and len(server.ranged()) > 4 and not flipped:
            flipped.append(req)
            server.swap(NAME, changed, '"v2"')

    server.faults.append(flip_after_four)
    result = fetch(server, out)

    assert result.ok and result.restarts == 1
    assert sha(out) == changed.sha256()
    assert json.load(open(str(out) + ".oa.json"))["etag"] == '"v2"'


def test_a_server_that_ignores_if_range_is_caught_by_the_etag_on_the_response(
        tmp_path, source):
    changed = PatternSource(source.size, seed=99)
    with FaultServer({NAME: source}, honor_if_range=False) as server:
        flipped = []

        def flip_after_four(req):
            if req.start is not None and len(server.ranged()) > 4 and not flipped:
                flipped.append(req)
                server.swap(NAME, changed, '"v2"')

        server.faults.append(flip_after_four)
        out = tmp_path / "got.bin"
        result = fetch(server, out)

    assert result.ok and result.restarts == 1 and sha(out) == changed.sha256()


def test_a_file_that_changes_on_every_request_fails_instead_of_looping(tmp_path, source):
    with FaultServer({NAME: source}) as server:
        counter = []

        def churn(req):
            if req.start is not None:
                counter.append(1)
                server.swap(NAME, PatternSource(source.size, seed=1000 + len(counter)),
                            f'"v{len(counter) + 1}"')

        server.faults.append(churn)
        out = tmp_path / "got.bin"
        result = fetch(server, out)
    assert not result.ok and result.restarts == 1
    assert not os.path.exists(out)
    assert "changed" in result.reason


def test_a_server_that_answers_200_to_ranges_gets_one_restart_then_a_plain_download(
        tmp_path, server, source):
    out = tmp_path / "got.bin"
    server.faults.append(lambda r: Status200() if r.start is not None else None)
    result = fetch(server, out)

    assert result.ok and sha(out) == source.sha256()
    assert result.restarts == 1


def test_a_server_with_no_validators_still_downloads_without_if_range(tmp_path, source):
    with FaultServer({NAME: source}, validators=False) as server:
        out = tmp_path / "got.bin"
        result = fetch(server, out)
        assert result.ok and sha(out) == source.sha256()
        assert all(r.if_range is None for r in server.ranged())


# --------------------------------------------------------------------- lanes


def test_a_lane_at_a_tenth_of_the_speed_completes_under_a_tenth_of_the_pieces(tmp_path):
    source = PatternSource(40 * PIECE, seed=3)
    uas = [f"lane-{i}" for i in range(8)]
    with FaultServer({NAME: source}) as server:
        slow = uas[0]
        server.throttle = lambda ua: 200_000 if ua == slow else 2_000_000
        out = tmp_path / "got.bin"
        downloader = engine.Downloader([None] * 8, config=cfg(slow_ratio=0), user_agents=uas)
        assert sorted(downloader.lane_user_agents) == uas
        slow_lane = downloader.lane_user_agents.index(slow)
        result = downloader.download(server.url(NAME), str(out))
        downloader.close()

    assert result.ok and sha(out) == source.sha256()
    assert result.pieces_by_lane.get(slow_lane, 0) < 40 / 10


def test_a_lane_far_below_the_median_is_cancelled_requeued_and_rotated(tmp_path):
    source = PatternSource(30 * PIECE, seed=4)
    uas = [f"lane-{i}" for i in range(6)]
    with FaultServer({NAME: source}) as server:
        slow = uas[0]
        server.throttle = lambda ua: 20_000 if ua == slow else 1_000_000
        out = tmp_path / "got.bin"
        downloader = engine.Downloader(
            [None] * 6, user_agents=uas,
            config=cfg(slow_window=0.4, slow_ratio=0.25, slow_min_lanes=3, timeout=(5, 5)))
        slow_lane = downloader.lane_user_agents.index(slow)
        session_before = downloader.lanes[slow_lane].circuit.session
        started = time.monotonic()
        result = downloader.download(server.url(NAME), str(out))
        elapsed = time.monotonic() - started
        # The abandoned attempt unwinds at its next read, which at 20 KB/s is a moment
        # after download() has returned; the rotation is what it does on the way out.
        deadline = time.monotonic() + 5
        while (downloader.lanes[slow_lane].circuit.session is session_before
               and time.monotonic() < deadline):
            time.sleep(0.05)
        rotated = downloader.lanes[slow_lane].circuit.session is not session_before
        downloader.close()

    assert result.ok and sha(out) == source.sha256()
    assert result.slow_cancelled >= 1
    assert rotated, "the cancelled lane was handed back on the same circuit"
    assert result.pieces_by_lane.get(slow_lane, 0) == 0
    # At 20 KB/s one piece alone would take 13 s; the cancel is what makes this fast.
    assert elapsed < 8


def test_the_last_pieces_are_raced_and_the_first_to_finish_wins(tmp_path):
    source = PatternSource(6 * PIECE, seed=5)
    uas = [f"lane-{i}" for i in range(6)]
    with FaultServer({NAME: source}) as server:
        server.throttle = lambda ua: 30_000 if ua == "lane-0" else None
        out = tmp_path / "got.bin"
        downloader = engine.Downloader([None] * 6, config=cfg(slow_ratio=0, timeout=(30, 30)),
                                       user_agents=uas)
        slow_lane = downloader.lane_user_agents.index("lane-0")
        started = time.monotonic()
        result = downloader.download(server.url(NAME), str(out))
        elapsed = time.monotonic() - started
        downloader.close()

    assert result.ok and sha(out) == source.sha256()
    assert result.raced >= 1
    assert result.pieces_by_lane.get(slow_lane, 0) == 0
    assert elapsed < 6, "the slow lane's piece held the whole file hostage"


def test_pieces_that_are_all_on_schedule_are_never_fetched_twice(tmp_path):
    """The endgame races a *straggler*, not every piece that happens to be last.

    Found by the Phase 5 benchmark, not by a test: on a clean network a 16 MiB file over four
    equal lanes was served 24 MiB, because the last pieces were raced the instant a lane
    went idle, whether or not anything was late. Over Tor that is bandwidth spent on nothing.
    """
    source = PatternSource(4 * PIECE, seed=21)
    with FaultServer({NAME: source}) as server:
        server.throttle = lambda ua: 2_000_000            # every lane the same: ~0.13 s a piece
        out = tmp_path / "got.bin"
        result = fetch(server, out, lanes=4)
        starts = [r.start for r in server.ranged()]
        assert result.ok and sha(out) == source.sha256()
    assert sorted(starts) == [i * PIECE for i in range(4)], "a piece was requested twice"
    assert result.raced == 0


def test_a_late_piece_is_raced_only_once_it_is_clearly_late(tmp_path):
    """The other half: a real straggler still gets rescued, and not instantly."""
    source = PatternSource(6 * PIECE, seed=22)
    uas = [f"lane-{i}" for i in range(6)]
    with FaultServer({NAME: source}) as server:
        server.throttle = lambda ua: 40_000 if ua == "lane-0" else 4_000_000
        out = tmp_path / "got.bin"
        downloader = engine.Downloader([None] * 6, user_agents=uas,
                                       config=cfg(slow_ratio=0, race_floor=0.4, timeout=(30, 30)))
        slow_lane = downloader.lane_user_agents.index("lane-0")
        started = time.monotonic()
        result = downloader.download(server.url(NAME), str(out))
        elapsed = time.monotonic() - started
        downloader.close()
    assert result.ok and sha(out) == source.sha256()
    assert result.raced >= 1 and result.pieces_by_lane.get(slow_lane, 0) == 0
    assert elapsed < 5, "the straggler held the file hostage"


def test_each_lane_keeps_one_user_agent_for_its_whole_life(tmp_path, server, source,
                                                           monkeypatch):
    import OnionAccelerator as oa

    def forbidden(_):
        raise AssertionError("the download path picked a User-Agent per attempt")

    monkeypatch.setattr(oa, "random_user_agent", forbidden)
    server.faults.append(lambda r: Drop(after=10) if r.n % 5 == 0 else None)
    pool = [f"agent-{i}" for i in range(50)]
    downloader = engine.Downloader([None] * 4, config=cfg(retries=4), user_agents=pool,
                                   job_id="j")
    result = downloader.download(server.url(NAME), str(tmp_path / "got.bin"))
    downloader.close()

    assert result.ok
    assert len(set(downloader.lane_user_agents)) == 4
    assert {r.ua for r in server.log} <= set(downloader.lane_user_agents)


def test_lanes_on_one_daemon_present_different_socks_credentials(tmp_path):
    source = PatternSource(24 * PIECE, seed=6)
    with FaultServer({NAME: source}) as server, Socks5Server() as one, Socks5Server() as two:
        server.throttle = lambda ua: 1_000_000
        out = tmp_path / "got.bin"
        downloader = engine.Downloader([one.address, two.address], circuits_per_endpoint=2,
                                       config=cfg(timeout=(5, 5)), user_agents=["a", "b", "c", "d"])
        result = downloader.download(server.url(NAME), str(out))
        downloader.close()

    assert result.ok and sha(out) == source.sha256()
    credentials = set(one.credentials) | set(two.credentials)
    assert "<anonymous>" not in credentials
    assert len(credentials) >= 3, credentials
    assert one.distinct_credentials >= 1 and two.distinct_credentials >= 1


# --------------------------------------------------------------------- many files


def test_many_files_download_together_and_one_failing_does_not_stop_the_rest(tmp_path):
    """The multi-mode shape: small single-GET files and big piece-wise ones, sharing lanes."""
    files = {f"small{i}.bin": PatternSource(PIECE // 3 + i, seed=20 + i) for i in range(4)}
    files.update({f"big{i}.bin": PatternSource(5 * PIECE + 77 * i, seed=30 + i) for i in range(3)})
    files["broken.bin"] = PatternSource(5 * PIECE, seed=40)
    with FaultServer(files) as server:
        server.faults.append(lambda r: Drop(after=0) if r.name == "broken.bin" and r.start is not None
                             else None)
        items = [(server.url(name), str(tmp_path / name)) for name in files]
        downloader = engine.Downloader([None] * 4, config=cfg(retries=2),
                                       user_agents=["a", "b", "c", "d"])
        results = downloader.download_many(items, file_workers=4)
        downloader.close()

    assert [r.url for r in results] == [url for url, _ in items], "results come back in order"
    by_name = {os.path.basename(r.path): r for r in results}
    assert not by_name["broken.bin"].ok
    for name, source in files.items():
        if name == "broken.bin":
            assert not os.path.exists(tmp_path / name)
            assert os.path.exists(str(tmp_path / name) + ".oa-state"), "left resumable"
            continue
        assert by_name[name].ok and sha(tmp_path / name) == source.sha256(), name
        assert os.path.exists(str(tmp_path / name) + ".oa.json")
    assert by_name["big0.bin"].pieces_total == 5      # exactly five pieces
    assert by_name["big1.bin"].pieces_total == 6      # five and a sliver
    assert by_name["small0.bin"].pieces_total == 1    # fits in one: a single GET


# --------------------------------------------------------------------- events


def test_events_carry_exactly_what_was_written_to_disk(tmp_path, server, source):
    out = tmp_path / "got.bin"
    journal = str(out) + ".oa-state"
    events, mismatches = [], []

    def sink(kind, data):
        events.append((kind, data))
        if kind in ("download.start", "download.piece"):
            lines = [json.loads(line) for line in open(journal).read().splitlines() if line]
            if data not in lines:
                mismatches.append((kind, data))

    result = fetch(server, out, sink=sink)
    kinds = [kind for kind, _ in events]

    assert result.ok and not mismatches
    assert kinds[0] == "download.start" and kinds[-1] == "download.done"
    assert kinds.count("download.piece") == 13
    done = events[-1][1]
    assert done == json.load(open(str(out) + ".oa.json"))
    assert {data["i"] for kind, data in events if kind == "download.piece"} == set(range(13))


def test_a_failure_event_is_the_record_left_in_the_journal(tmp_path, server):
    out = tmp_path / "got.bin"
    server.faults.append(lambda r: Drop(after=0) if r.start == 2 * PIECE else None)
    events = []
    result = fetch(server, out, sink=lambda kind, data: events.append((kind, data)))

    assert not result.ok
    fails = [data for kind, data in events if kind == "download.fail"]
    assert len(fails) == 1
    lines = [json.loads(line) for line in open(str(out) + ".oa-state").read().splitlines()]
    assert fails[0] in lines and fails[0]["reason"] == result.reason


# --------------------------------------------------------------------- no size


def test_a_file_whose_size_cannot_be_found_is_one_sequential_piece(tmp_path):
    """No Content-Length anywhere: one GET, no state to resume, whole-file hash recorded."""
    data = PatternSource(PIECE * 3, seed=8)
    with FaultServer({NAME: data}, hide_length=True, validators=False) as server:
        out = tmp_path / "got.bin"
        result = fetch(server, out)
        assert result.ok and result.pieces_total == 1
        assert sha(out) == data.sha256()
        assert not any(r.if_range for r in server.log)
    assert side_files(out) == ["got.bin", "got.bin.oa.json"]
    assert json.load(open(str(out) + ".oa.json"))["size"] == data.size


def test_a_failed_sequential_download_leaves_nothing_behind(tmp_path):
    """There is no state to resume from, so there must be no litter either."""
    with FaultServer({NAME: PatternSource(PIECE * 3, seed=8)}, hide_length=True,
                     validators=False) as server:
        server.faults.append(lambda r: Drop(after=1000) if r.method == "GET" else None)
        out = tmp_path / "got.bin"
        assert not fetch(server, out, retries=2).ok
    assert side_files(out) == []


# --------------------------------------------------------------------- memory


MEMORY_SCRIPT = textwrap.dedent("""
    import resource, sys
    sys.path.insert(0, {root!r})
    from oacore import engine
    config = engine.EngineConfig(piece_size=4 * 1024 * 1024, retries=2, timeout=(10, 30))
    d = engine.Downloader([None] * 8, config=config, user_agents=["mem"])
    result = d.download({url!r}, {out!r})
    d.close()
    print(int(result.ok), resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
""")


@pytest.mark.slow
def test_peak_memory_stays_under_200_mib_on_a_2_gib_transfer_with_eight_lanes(tmp_path):
    size = 2 * 1024 ** 3
    source = PatternSource(size, seed=11)
    with FaultServer({NAME: source}) as server:
        out = tmp_path / "big.bin"
        script = MEMORY_SCRIPT.format(root=ROOT, url=server.url(NAME), out=str(out))
        done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                              timeout=900)
        assert done.returncode == 0, done.stderr
        ok, peak_kib = (int(x) for x in done.stdout.split())
        assert ok == 1
        assert os.path.getsize(out) == size
        assert sha(out) == source.sha256()
    assert peak_kib / 1024 < 200, f"peak resident memory {peak_kib / 1024:.0f} MiB"
