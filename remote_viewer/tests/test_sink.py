"""The embedder's event sink: what ``main(sink=...)`` publishes while it lists.

rvtree's listing of a remote archive is minutes of work over Tor and the entries come
out one at a time, so an embedder -- OnionAccelerator's ``--mode tree`` -- can serve
them to something watching while the walk is still going. The contract those tests hold
to is narrow and worth keeping narrow: a sink sees every member exactly once, sees
exactly what ``-f ndjson`` writes, and cannot break the listing by failing.
"""

from __future__ import annotations

import io
import json

import pytest

from rvtree import cli, render


class Recorder:
    """A sink that keeps what it was given."""

    def __init__(self, explode: bool = False):
        self.events: list[tuple[str, dict]] = []
        self.explode = explode

    def __call__(self, kind: str, data: dict) -> None:
        if self.explode:
            raise RuntimeError("this consumer is broken")
        self.events.append((kind, data))

    def of(self, kind: str) -> list[dict]:
        return [data for name, data in self.events if name == kind]


def run(transport, server, name, sink, *extra):
    argv = ["list", f"{server.base}/{name}", "-f", "ndjson", "--quiet", *extra]
    return cli.main(argv, transport=transport, sink=sink)


# ----------------------------------------------------------------- entries


def test_every_member_reaches_the_sink_exactly_once(transport, server, capsys):
    sink = Recorder()
    assert run(transport, server, "test.tar", sink) == 0

    streamed = sink.of(cli.EVENT_ENTRY)
    printed = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    assert streamed == printed
    assert len(streamed) == len({e["path"] for e in streamed})


@pytest.mark.parametrize("name", ["test.zip", "test.tar", "test.7z", "test.rar5.rar"])
def test_the_sink_sees_the_same_object_ndjson_does(transport, server, name, capsys):
    """One wire format for members, whichever way they leave the process.

    Both call render.as_dict, and this is the test that stops somebody 'improving' one
    of the two paths: a consumer indexing the live stream and one indexing a saved
    tree.ndjson have to be able to use the same code.
    """
    sink = Recorder()
    assert run(transport, server, name, sink) == 0
    printed = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    assert sink.of(cli.EVENT_ENTRY) == printed


def test_entries_are_published_as_they_are_walked_not_at_the_end(transport, server):
    """A generator, not a list: the point of the stream is that it arrives early.

    Consuming half the walk and asserting the sink has seen only that half is the only
    way to tell "streamed" from "collected and handed over".
    """
    from rvtree import archive

    sink = Recorder()
    arc = archive.open_archive(transport, f"{server.base}/test.tar")
    walk = cli._counted(archive.list_archive(arc), _NoBar(), arc, sink)

    first = next(walk)
    assert sink.of(cli.EVENT_ENTRY) == [render.as_dict(first)]
    second = next(walk)
    assert sink.of(cli.EVENT_ENTRY) == [render.as_dict(first), render.as_dict(second)]


# ----------------------------------------------------------------- run facts


def test_the_archive_is_announced_before_the_walk_and_summarised_after(transport, server):
    sink = Recorder()
    assert run(transport, server, "test.zip", sink) == 0

    kinds = [kind for kind, _ in sink.events]
    assert kinds[0] == cli.EVENT_OPEN
    assert kinds[-1] == cli.EVENT_DONE

    opened = sink.of(cli.EVENT_OPEN)[0]
    assert opened["url"].endswith("test.zip")
    assert opened["size"] > 0 and opened["format"]

    done = sink.of(cli.EVENT_DONE)[0]
    assert done["entries"] == len(sink.of(cli.EVENT_ENTRY))
    assert done["requests"] >= 1


def test_a_listing_without_a_sink_behaves_exactly_as_before(transport, server, capsys):
    assert cli.main(["list", f"{server.base}/test.tar", "-f", "ndjson", "--quiet"],
                    transport=transport) == 0
    assert capsys.readouterr().out.splitlines()


# ----------------------------------------------------------------- robustness


def test_a_broken_consumer_does_not_break_the_listing(transport, server, capsys):
    """The archive is the job; the stream is a courtesy. It cannot cost us the job."""
    assert run(transport, server, "test.tar", Recorder(explode=True)) == 0
    assert capsys.readouterr().out.splitlines()


class _NoBar:
    """Enough of progress.Reporter for _counted, without a display."""

    def stage(self, *a, **k):
        pass

    def track(self, *a, **k):
        pass

    def entry(self, n: int = 1):
        pass
