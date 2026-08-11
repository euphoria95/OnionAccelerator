"""Progress and diagnostics: what they show, and what they must never disturb.

The hard rule the integration half exists to enforce is that stdout carries payload and
nothing else. A bar or a log line that leaked into an ndjson stream, or into extracted
member bytes, would corrupt the output of a tool whose whole job is to be piped.
"""

from __future__ import annotations

import io
import json
import logging
import re
import threading
import time

import pytest

from rvtree import archive, progress
from rvtree.cli import main

ANSI = re.compile(r"\x1b\[")


class _Fake:
    """A stand-in for Transport, holding only what the display reads."""

    def __init__(self, fetched=0, requests=0):
        self.bytes_fetched = fetched
        self.requests_made = requests
        self.bytes_inflight = 0
        self.throughput = 512 * 1024.0


def _reporter(**kwargs) -> progress.Reporter:
    stream = kwargs.pop("stream", io.StringIO())
    kwargs.setdefault("ansi", True)
    kwargs.setdefault("color", False)
    return progress.Reporter(stream=stream, **kwargs)


# ----------------------------------------------------------------- rendering


def test_a_known_total_renders_a_bar_a_percentage_and_the_counters():
    r = _reporter()
    r.attach(_Fake(fetched=3 * 1024 * 1024, requests=18))
    r.stage("walk", "tar inside an XZ stream")
    r.position(50, 200)
    r.entry(1204)
    head, tail = r._compose(100)
    assert "walk" in head
    assert "25%" in head
    assert "[" in head and "]" in head
    assert "3.0 MiB fetched" in tail
    assert "18 req" in tail
    assert "1,204 entries" in tail


def test_an_unknown_total_still_shows_movement():
    """A 7z header read has no denominator, but silence still has to look like work."""
    r = _reporter()
    r.attach(_Fake())
    r.stage("open", "reading the header")
    first = r._compose(100)[0]
    r._started -= 0.5  # advance the marquee without sleeping
    second = r._compose(100)[0]
    assert "%" not in first
    assert first != second


def test_the_block_counter_is_shown_when_the_walker_reports_one():
    r = _reporter()
    r.attach(_Fake())
    r.stage("walk")
    r.block(11, 57)
    assert "block 12/57" in r._compose(100)[0]


def test_progress_never_goes_backwards():
    """tarfile seeks back for pax records; a bar that reversed would look like a bug."""
    r = _reporter()
    r.attach(_Fake())
    r.stage("walk")
    r.position(80, 100)
    r.position(20, 100)
    assert "80%" in r._compose(100)[0]


@pytest.mark.parametrize("width", [120, 100, 72, 58, 30, 12])
def test_no_rendered_line_overflows_the_terminal(width):
    r = _reporter()
    r.attach(_Fake(fetched=12345678, requests=99))
    r.stage("walk", "tar inside an XZ stream")
    r.position(3, 7)
    r.entry(4242)
    lines = r._compose(width)
    assert lines, "something must always be drawn"
    for line in lines:
        assert len(line) <= width
    if width < progress.MIN_TWO_LINE_WIDTH:
        assert len(lines) == 1


class _AsciiStream(io.StringIO):
    """A terminal under LANG=C: it cannot encode a block-drawing character."""

    encoding = "ascii"


def test_a_terminal_that_cannot_encode_blocks_gets_ascii():
    r = _reporter(stream=_AsciiStream())
    r.attach(_Fake())
    r.stage("walk")
    r.position(1, 2)
    head = r._compose(100)[0]
    head.encode("ascii")  # would raise if a block-drawing glyph got through


def test_a_poll_that_raises_does_not_take_the_run_with_it():
    r = _reporter()
    r.attach(_Fake())
    r.stage("walk")

    def broken() -> int:
        raise RuntimeError("the stream closed underneath us")

    r.track_bytes(broken, 100)
    r._draw()  # must not raise
    assert isinstance(r._compose(100)[0], str)


# ----------------------------------------------------------------- the terminal


def test_painting_then_erasing_leaves_nothing_behind():
    stream = io.StringIO()
    r = _reporter(stream=stream)
    r.attach(_Fake())
    r.stage("walk")
    r._draw()
    assert "walk" in stream.getvalue()
    r._erase()
    # Everything painted is followed by a cursor-up and a clear, so the last thing the
    # terminal is told is to blank those rows.
    assert stream.getvalue().endswith("\x1b[2A")


def test_a_log_record_suspends_the_bar():
    stream = io.StringIO()
    r = _reporter(stream=stream)
    r.attach(_Fake())
    r.stage("walk")
    r._draw()
    with r.suspended():
        mark = len(stream.getvalue())
        stream.write("rvtree: something happened\n")
    after = stream.getvalue()[mark:]
    assert not ANSI.search(after), "the log line must land on an already-cleared row"


def test_close_is_idempotent_and_leaves_no_thread_behind():
    before = threading.active_count()
    r = _reporter()
    r.attach(_Fake())
    r.start()
    r.close()
    r.close()
    for _ in range(50):
        if threading.active_count() <= before:
            break
        time.sleep(0.02)
    assert threading.active_count() <= before


def test_counting_from_many_threads_is_exact():
    r = _reporter(enabled=False)
    threads = [
        threading.Thread(target=lambda: [r.entry() for _ in range(2000)]) for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert r.entries == 16000


# ----------------------------------------------------------------- enablement


@pytest.mark.parametrize(
    "when,quiet,tty,expected",
    [
        ("auto", False, True, True),
        ("auto", False, False, False),  # redirected stderr: cursor moves would be noise
        ("auto", True, True, False),  # quiet means quiet
        ("never", False, True, False),
        ("always", False, False, True),  # how the tests drive it
        ("always", True, True, False),  # -q still wins
    ],
)
def test_when_a_bar_is_drawn(when, quiet, tty, expected):
    stream = io.StringIO()
    stream.isatty = lambda: tty  # type: ignore[method-assign]
    assert progress.make(when, quiet=quiet, stream=stream).enabled is expected


# ----------------------------------------------------------------- end to end


def test_stderr_stays_clean_when_it_is_not_a_terminal(server, capsys):
    """The default has to be invisible to every existing script and CI job."""
    assert main(["list", "--proxy", "none", f"{server.base}/test.zip"]) == 0
    captured = capsys.readouterr()
    assert not ANSI.search(captured.err)
    assert "\r" not in captured.err
    assert captured.err.startswith("\nZIP archive:")  # just the summary, as before


def test_ndjson_on_stdout_survives_a_forced_bar(server, capsys):
    """Every line must still parse: this is the output people pipe into jq."""
    code = main(
        ["list", "--proxy", "none", "--progress", "always", "-f", "ndjson", f"{server.base}/test.zip"]
    )
    out = capsys.readouterr().out
    assert code == 0
    entries = [json.loads(line) for line in out.splitlines() if line]
    assert len(entries) == 14


def test_extraction_to_stdout_is_byte_identical_with_a_bar(server, capsys):
    args = ["extract", "--proxy", "none", f"{server.base}/test.zip", "payload/sub/deep/note.md"]
    assert main(args) == 0
    plain = capsys.readouterr().out
    assert main(args + ["--progress", "always"]) == 0
    assert capsys.readouterr().out == plain == "hello\n"


def test_quiet_silences_the_summary_and_the_bar(server, capsys):
    assert main(["list", "--proxy", "none", "-q", "--progress", "always", f"{server.base}/test.zip"]) == 0
    assert capsys.readouterr().err == ""


# ----------------------------------------------------------------- verbosity


def test_default_output_says_nothing_new(server, capsys):
    """Guard against INFO leaking into the stderr other tests assert on."""
    assert main(["list", "--proxy", "none", f"{server.base}/test.tar"]) == 0
    assert capsys.readouterr().err.count("\n") == 2  # blank line + summary


def test_v_names_the_stages_of_the_run(server, capsys):
    assert main(["list", "--proxy", "none", "-v", f"{server.base}/test.multiblock.tar.xz"]) == 0
    err = capsys.readouterr().err
    for stage in ("probe", "detect", "index", "walk"):
        assert stage in err
    assert "identified as" in err
    assert "xz index:" in err


def test_vv_logs_one_line_per_range_request(server, capsys):
    """The count is the point: a request the log does not mention is one you cannot debug."""
    from rvtree.transport import Transport

    seen = {}
    real_close = Transport.close

    def close(self):
        seen["requests"] = self.requests_made
        real_close(self)

    Transport.close = close
    try:
        assert main(["list", "--proxy", "none", "-vv", f"{server.base}/test.tar"]) == 0
    finally:
        Transport.close = real_close
    err = capsys.readouterr().err
    assert err.count("GET bytes=") == seen["requests"]


def test_debug_shows_a_traceback_and_the_default_does_not(server, capsys, monkeypatch):
    def explode(*args, **kwargs):
        raise archive.ArchiveError("something went wrong deep down")

    monkeypatch.setattr(archive, "list_archive", explode)
    url = f"{server.base}/test.zip"

    assert main(["list", "--proxy", "none", url]) == 1
    plain = capsys.readouterr().err
    assert plain.startswith("rvtree: something went wrong")
    assert "Traceback" not in plain

    assert main(["list", "--proxy", "none", "--debug", url]) == 1
    assert "Traceback" in capsys.readouterr().err


def test_repeated_runs_do_not_multiply_log_lines(server, capsys):
    """main() is called many times in one pytest process; handlers must not accumulate."""
    url = f"{server.base}/test.zip"
    for _ in range(3):
        assert main(["list", "--proxy", "none", "-v", url]) == 0
    err = capsys.readouterr().err
    assert len([h for h in logging.getLogger("rvtree").handlers if not isinstance(h, logging.NullHandler)]) == 1
    assert err.count("identified as zip") == 3


def test_log_file_captures_the_full_story_whatever_the_console_was_told(server, tmp_path):
    path = tmp_path / "rv.log"
    assert main(
        ["list", "--proxy", "none", "--log-file", str(path), f"{server.base}/test.zip"]
    ) == 0
    logged = path.read_text()
    assert "GET bytes=" in logged  # DEBUG, even though the console was at WARNING
    assert "identified as zip" in logged


# ----------------------------------------------------------------- the walkers


class _Recorder(progress.Reporter):
    """Captures what a walk publishes, instead of drawing it."""

    def __init__(self):
        super().__init__(stream=io.StringIO(), enabled=False, ansi=False)
        self.positions: list[int] = []
        self.blocks: list[int] = []

    def position(self, pos, total=None):
        self.positions.append(pos)
        super().position(pos, total)

    def block(self, index, count):
        self.blocks.append(index)
        super().block(index, count)


def test_rar_publishes_its_own_monotonic_offsets(transport, server):
    """RAR drives positional reads, so the file object's position never moves: without
    this the bar would sit at 0% for the whole walk."""
    recorder = _Recorder()
    with progress.activate(recorder):
        arc = archive.open_archive(transport, f"{server.base}/test.rar5.rar")
        assert len(list(archive.list_archive(arc))) > 0
    assert recorder.positions
    assert all(b > a for a, b in zip(recorder.positions, recorder.positions[1:]))
    assert recorder.positions[-1] <= arc.size


def test_the_xz_walk_reports_the_blocks_it_decodes(transport, server):
    recorder = _Recorder()
    with progress.activate(recorder):
        arc = archive.open_archive(transport, f"{server.base}/test.multiblock.tar.xz")
        entries = list(archive.list_archive(arc))
    assert len(entries) == 14
    assert recorder.blocks == sorted(recorder.blocks)
    assert max(recorder.blocks) < len(arc.xz_index.blocks)


def test_a_tar_walk_can_be_followed_by_its_stream_position(transport, server):
    arc = archive.open_archive(transport, f"{server.base}/test.tar")
    entries = list(archive.list_archive(arc))
    assert entries
    assert arc.walk_source is arc.source
    assert arc.walk_source.tell() > 0


def test_progress_costs_no_extra_traffic(transport, server):
    """The poll functions must never touch the network — the cost budgets depend on it."""
    arc = archive.open_archive(transport, f"{server.base}/test.multiblock.tar.xz")
    baseline_entries = list(archive.list_archive(arc))
    quiet_bytes = transport.bytes_fetched

    recorder = _Recorder()
    recorder.attach(transport)
    with progress.activate(recorder):
        arc2 = archive.open_archive(transport, f"{server.base}/test.multiblock.tar.xz")
        loud_entries = list(archive.list_archive(arc2))
        for _ in range(20):
            recorder._draw()
    assert len(loud_entries) == len(baseline_entries)
    assert transport.bytes_fetched <= 2 * quiet_bytes + 1024
