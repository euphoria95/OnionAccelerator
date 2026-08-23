"""The proof-of-work gate, and what a rangeless server leaves rvtree able to do.

Every test here runs against ``gateserver``, which reproduces a live gate: an HTML
challenge served as 200 with no Content-Length, a single-use token behind it, and a
download endpoint that refuses ranges. Before this existed, that host produced the
message "server ignored Range and gave no Content-Length" — technically true of the
challenge page, and wrong about everything that mattered.
"""

from __future__ import annotations

import hashlib
import os

import pytest
from gateserver import GateServer

from rvtree import archive
from rvtree.cli import main
from rvtree.transport import GATE_HEADER, RangeNotHonoured, SpoolError, gate, probe, spool

ARCHIVE = "test.tar"


@pytest.fixture
def gated(fixtures):
    with GateServer(fixtures, ARCHIVE) as srv:
        yield srv


@pytest.fixture
def gated_with_ranges(fixtures):
    with GateServer(fixtures, ARCHIVE, verified_ranges=True) as srv:
        yield srv


# -- parsing and solving ------------------------------------------------------


def test_a_challenge_page_is_recognised_and_solved():
    page = """
      <form method="POST" action="/verify">
        <input type="hidden" name="challenge" value="abc123">
        <input type="hidden" name="nonce" id="nonce">
      </form>
      <script>const difficulty = 3; crypto.subtle.digest("SHA-256", x);</script>
    """
    challenge = gate.detect(page, "http://host/dir/file.7z")
    assert challenge is not None
    assert (challenge.seed, challenge.difficulty) == ("abc123", 3)
    assert challenge.action == "http://host/verify"

    nonce = gate.solve(challenge)
    assert hashlib.sha256(b"abc123" + nonce.encode()).hexdigest().startswith("000")


@pytest.mark.parametrize(
    "page",
    [
        "",
        "<html>plain page</html>",
        # A login form is HTML with a form in it, and must not be mistaken for a gate.
        '<form action="/login"><input name="user" value=""></form>',
        # No stated difficulty means no way to know when the work is done.
        '<form action="/verify"><input name="challenge" value="a">'
        '<input name="nonce"></form><script>SHA-256</script>',
    ],
)
def test_other_html_is_not_mistaken_for_a_gate(page):
    assert gate.detect(page, "http://host/file.7z") is None


def test_an_unaffordable_difficulty_is_refused_rather_than_attempted():
    page = (
        '<form action="/verify"><input name="challenge" value="a">'
        '<input name="nonce"></form><script>const difficulty = 12; "SHA-256"</script>'
    )
    with pytest.raises(gate.GateError, match="difficulty"):
        gate.detect(page, "http://host/file.7z")


# -- the transport through a gate ---------------------------------------------


def test_the_probe_reports_the_entity_behind_the_gate(gated, transport, fixtures):
    """The size that comes back is the archive's, not the challenge page's."""
    caps = probe(transport, gated.url)

    assert caps.size == os.path.getsize(os.path.join(fixtures, ARCHIVE))
    assert caps.gated is True
    assert GATE_HEADER in caps.headers
    assert caps.accepts_ranges is False
    assert gated.solved == 1


def test_a_gate_alone_does_not_cost_random_access(gated_with_ranges, transport):
    """It is the token endpoint that refuses ranges, not the gate. Keep them apart."""
    caps = probe(transport, gated_with_ranges.url)

    assert caps.gated is True
    assert caps.accepts_ranges is True


def test_a_range_request_through_a_gate_says_which_url_refused_it(gated, transport):
    with pytest.raises(RangeNotHonoured) as exc:
        transport.get_range(gated.url, 0, 511)

    message = str(exc.value)
    assert "proof-of-work" in message
    assert "/download" in message


def test_each_grant_is_spent_once(gated, transport):
    """Two probes mean two challenges: a token cannot be cached and replayed."""
    probe(transport, gated.url)
    probe(transport, gated.url)

    assert gated.solved == 2


# -- the way through: one stream to disk --------------------------------------


def test_spooling_produces_a_readable_local_archive(gated, transport, fixtures, tmp_path):
    caps = probe(transport, gated.url)
    target = str(tmp_path / "copy.tar")

    source = spool(transport, gated.url, path=target, expected_size=caps.size)
    try:
        with open(os.path.join(fixtures, ARCHIVE), "rb") as fh:
            expected = fh.read()
        assert source.size == len(expected)
        assert source.read() == expected
        assert source.read_tail(512) == expected[-512:]
        assert source.pread(0, 8) == expected[:8]
    finally:
        source.close()
    assert os.path.getsize(target) == caps.size


def test_a_finished_spool_is_reused_instead_of_fetched_again(gated, transport, tmp_path):
    caps = probe(transport, gated.url)
    target = str(tmp_path / "copy.tar")

    spool(transport, gated.url, path=target, expected_size=caps.size).close()
    after_first = gated.downloads

    # The point of naming the file: a rangeless server has no resume, so a second run
    # that had to fetch again would pay the whole transfer twice.
    reused = spool(transport, gated.url, path=target, expected_size=caps.size)
    reused.close()

    assert gated.downloads == after_first
    assert reused.size == caps.size


def test_a_temporary_spool_is_removed_when_it_is_closed(gated, transport):
    source = spool(transport, gated.url)
    path = source.path
    assert os.path.isfile(path)

    source.close()
    assert not os.path.exists(path)


def test_a_short_transfer_is_reported_and_not_left_behind(gated, transport, monkeypatch):
    """No resume exists here, so a truncated body must never reach a parser."""
    real = transport.open_stream

    def truncating(url):
        stream = real(url)
        stream.size += 4096  # claim more than the body will deliver
        return stream

    monkeypatch.setattr(transport, "open_stream", truncating)
    with pytest.raises(SpoolError, match="nothing to resume"):
        spool(transport, gated.url)


# -- what the CLI does with all of it -----------------------------------------


def test_listing_a_rangeless_archive_refuses_until_asked_twice(gated, transport, capsys):
    """Exit 3 is "your decision", and the message has to say what the decision costs."""
    with pytest.raises(archive.RangelessWarning) as exc:
        archive.open_archive(transport, gated.url)

    message = str(exc.value)
    assert "--spool" in message
    assert "no resume" in message

    assert main(["list", gated.url, "--proxy", "none"], transport=transport) == 3


def test_spooling_lists_the_archive_it_could_not_range_over(gated, transport, tmp_path, capsys):
    target = str(tmp_path / "copy.tar")

    code = main(
        ["list", gated.url, "--proxy", "none", "--spool", target, "-f", "ndjson"],
        transport=transport,
    )

    assert code == 0
    out = capsys.readouterr().out
    assert '"path"' in out
    assert os.path.isfile(target)
