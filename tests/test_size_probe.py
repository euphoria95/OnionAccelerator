"""Tests for the remote file size probe.

The ladder exists because a single HEAD + Content-Length is not enough against real
servers, so every rung is exercised in isolation here: the only way to know rung 3 works
is to face it with a server that refuses HEAD, and the only way to know rung 4 works is
to face it with one that refuses HEAD *and* rejects any Range.

Nothing here touches Tor. The ``oa`` fixture stubs make_proxies() out, which is the
single point every probe routes through.
"""

import socket

import pytest
import requests

from conftest import PAYLOAD_SIZE
from probeserver import ProbeServer
from rangeserver import RangeServer

UA = "Mozilla/5.0 (probe test)"
PROXY = "127.0.0.1:9050"  # never dialled: the oa fixture stubs make_proxies out
LARGE = 16 * 1024 * 1024


def _dead_port() -> int:
    """A port that nothing is listening on."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------------------------------------------------------------- header parsers


@pytest.mark.parametrize(
    "value,expected",
    [
        ("1048576", 1048576),
        (" 42 ", 42),
        (1024, 1024),
        ("0", None),  # a HEAD-hostile server, not an empty file
        ("", None),
        ("-5", None),
        ("1.5", None),
        ("many", None),
        (None, None),
    ],
)
def test_positive_int(oa, value, expected):
    assert oa._positive_int(value) == expected


@pytest.mark.parametrize(
    "header,expected",
    [
        ('attachment; filename="file.zip"; size=1048576', 1048576),
        ('attachment; size="2048"; filename=a.bin', 2048),
        ('attachment; filename="a;b.zip"; SIZE=77', 77),
        ('attachment; filename="file.zip"', None),
        ("inline", None),
        # The reason this uses the stdlib parser rather than a size=(digits) regex:
        # the digits here belong to the filename, not to a size parameter.
        ('attachment; filename="report-size=99.pdf"', None),
    ],
)
def test_size_from_content_disposition(oa, header, expected):
    assert oa._size_from_content_disposition({"content-disposition": header}) == expected


def test_size_from_content_disposition_ignores_a_missing_header(oa):
    assert oa._size_from_content_disposition({}) is None


@pytest.mark.parametrize(
    "header,expected",
    [
        ("bytes 0-0/1048576", 1048576),
        ("bytes 10-19/1000", 1000),
        ("bytes */0", None),  # unsatisfied range
        ("bytes 0-0/*", None),  # length unknown to the server
        ("nonsense", None),
    ],
)
def test_size_from_content_range(oa, header, expected):
    assert oa._size_from_content_range({"content-range": header}) == expected


def test_a_206_takes_its_size_from_content_range_not_content_length(oa):
    """Content-Length on a 206 is the length of the range -- one byte for our probe."""
    headers = {"content-length": "1", "content-range": "bytes 0-0/1048576"}
    assert oa._size_from_response(206, headers) == (1048576, "Content-Range")


def test_content_length_outranks_content_disposition(oa):
    headers = {
        "content-length": "1048576",
        "content-disposition": 'attachment; filename="f.zip"; size=999',
    }
    assert oa._size_from_response(200, headers) == (1048576, "Content-Length")


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "br", "GZIP"])
def test_an_encoded_response_advertises_no_usable_size(oa, encoding):
    """Under a non-identity encoding every number describes the compressed transfer."""
    headers = {"content-length": "5000", "content-encoding": encoding}
    assert oa._size_from_response(200, headers) == (None, "")


@pytest.mark.parametrize("encoding", ["", "identity", " identity "])
def test_an_unencoded_response_is_read_normally(oa, encoding):
    headers = {"content-length": "5000", "content-encoding": encoding}
    assert oa._size_from_response(200, headers) == (5000, "Content-Length")


# ------------------------------------------------------- the ladder, rung by rung


def test_content_length_on_a_head_is_the_first_rung(oa, payload_dir):
    with RangeServer(payload_dir) as srv:
        probe = oa.probe_remote_size(f"{srv.base}/file.zip", PROXY, UA)
        assert srv.requests == ["-"], "one HEAD should have settled it"
    assert probe == oa.SizeProbe(size=PAYLOAD_SIZE, reachable=True, source="Content-Length")


def test_content_disposition_is_the_second_rung_and_shares_the_round_trip(oa):
    with ProbeServer(
        body=b"y" * 4096,
        omit_content_length=True,
        extra_headers={"Content-Disposition": 'attachment; filename="file.zip"; size=1048576'},
    ) as srv:
        probe = oa.probe_remote_size(f"{srv.base}/file.zip", PROXY, UA)
        assert srv.methods == ["HEAD"], "rungs 1 and 2 read the same response"
    assert probe.size == 1048576
    assert probe.source == "Content-Disposition size="


def test_content_range_is_the_third_rung_when_head_is_refused(oa):
    with ProbeServer(body=b"z" * PAYLOAD_SIZE, head_status=405) as srv:
        probe = oa.probe_remote_size(f"{srv.base}/file.bin", PROXY, UA)
        assert srv.methods == ["HEAD", "GET"]
        assert srv.requests[1] == ("GET", "bytes=0-0")
    assert probe.size == PAYLOAD_SIZE
    assert probe.source == "Content-Range"


def test_a_server_that_ignores_the_range_still_answers_on_the_third_rung(oa):
    """A Range the server ignores comes back 200, and then Content-Length is the entity."""
    with ProbeServer(body=b"w" * PAYLOAD_SIZE, head_status=405, ignore_ranges=True) as srv:
        probe = oa.probe_remote_size(f"{srv.base}/file.bin", PROXY, UA)
        assert srv.methods == ["HEAD", "GET"]
    assert probe.size == PAYLOAD_SIZE
    assert probe.source == "Content-Length"


def test_streamed_get_is_the_last_rung_and_aborts_before_the_body(oa):
    """The rung that only a server rejecting every Range header can reach.

    The body is large enough that finishing it would be unmistakable, so asserting the
    server got nowhere near the end is the proof that closing the unread response really
    does abort the transfer rather than quietly draining it.
    """
    with ProbeServer(body=b"q" * LARGE, head_status=405, range_status=501) as srv:
        probe = oa.probe_remote_size(f"{srv.base}/big.iso", PROXY, UA)
        assert srv.methods == ["HEAD", "GET", "GET"]
        assert srv.wait_for_body(), "the server never finished or aborted its write"
        assert srv.bytes_written < LARGE // 4
    assert probe.size == LARGE
    assert probe.source == "Content-Length"


def test_a_redirected_head_still_yields_the_size(oa):
    """requests.head() defaults allow_redirects to False, and a 302 is not a 4xx.

    Unguarded, the HEAD reports the redirect's empty body as the file size. The
    two-HEAD request log is what shows the redirect was followed on the first rung
    rather than papered over by the GET probes further down the ladder.
    """
    with ProbeServer(body=b"r" * PAYLOAD_SIZE, redirect="/real.bin") as srv:
        probe = oa.probe_remote_size(f"{srv.base}/download", PROXY, UA)
        assert srv.methods == ["HEAD", "HEAD"]
    assert probe.size == PAYLOAD_SIZE
    assert probe.source == "Content-Length"


def test_a_chunked_server_that_says_nothing_yields_no_size(oa):
    with ProbeServer(body=b"c" * 4096, head_status=405, range_status=501, chunked=True) as srv:
        probe = oa.probe_remote_size(f"{srv.base}/file.bin", PROXY, UA)
        assert srv.methods == ["HEAD", "GET", "GET"], "every rung was tried"
    assert probe.size is None
    assert probe.reachable is True, "the server answered, it just would not say how big"


def test_a_content_encoded_response_reports_no_size(oa, payload_dir):
    with RangeServer(payload_dir, extra_headers={"Content-Encoding": "gzip"}) as srv:
        probe = oa.probe_remote_size(f"{srv.base}/file.zip", PROXY, UA)
    assert probe.size is None
    assert probe.reachable is True


def test_a_missing_file_yields_no_size(oa, payload_dir):
    with RangeServer(payload_dir) as srv:
        probe = oa.probe_remote_size(f"{srv.base}/absent.zip", PROXY, UA)
    assert probe.size is None
    assert probe.reachable is False
    assert probe.error == "HTTP 404"


# ------------------------------------------------------------------ error handling


def test_an_unreachable_endpoint_yields_no_size(oa):
    probe = oa.probe_remote_size(f"http://127.0.0.1:{_dead_port()}/f.bin", PROXY, UA)
    assert probe.size is None
    assert probe.reachable is False
    assert probe.error


def test_a_connection_error_stops_the_ladder_after_one_probe(oa, monkeypatch):
    """Three probes through a dead proxy would cost 3 x REQUEST_TIMEOUT to learn nothing."""
    calls = []

    def dead(*args, **kwargs):
        calls.append(args)
        raise requests.exceptions.ConnectionError("no route to proxy")

    monkeypatch.setattr(oa, "_probe", dead)
    probe = oa.probe_remote_size("http://example.invalid/f.bin", PROXY, UA)
    assert len(calls) == 1
    assert probe.size is None
    assert probe.reachable is False


def test_a_non_connection_failure_falls_through_to_the_next_probe(oa, monkeypatch):
    answers = [requests.exceptions.ReadTimeout("HEAD hung"), (200, {"content-length": "4096"})]

    def flaky(*args, **kwargs):
        item = answers.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(oa, "_probe", flaky)
    assert oa.probe_remote_size("http://example.invalid/f.bin", PROXY, UA).size == 4096


def test_reachability_survives_a_connection_error_later_in_the_ladder(oa, monkeypatch):
    answers = [(200, {}), requests.exceptions.ConnectionError("proxy died mid-ladder")]

    def flaky(*args, **kwargs):
        item = answers.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(oa, "_probe", flaky)
    probe = oa.probe_remote_size("http://example.invalid/f.bin", PROXY, UA)
    assert probe.size is None
    assert probe.reachable is True, "an earlier probe did get an answer"


# ---------------------------------------------------------------------- callers


def test_get_remote_file_size_returns_the_bare_size(oa, payload_dir):
    with RangeServer(payload_dir) as srv:
        assert oa.get_remote_file_size(f"{srv.base}/file.zip", PROXY, UA) == PAYLOAD_SIZE


def test_get_remote_file_size_returns_none_when_nothing_answers(oa):
    assert oa.get_remote_file_size(f"http://127.0.0.1:{_dead_port()}/f.bin", PROXY, UA) is None


def test_check_url_availability_sees_a_head_hostile_server_as_available(oa):
    """It used to report these unavailable: the HEAD raised and nothing else was tried."""
    with ProbeServer(body=b"z" * PAYLOAD_SIZE, head_status=405) as srv:
        is_ok, size, eta = oa.check_url_availability(f"{srv.base}/f.bin", PROXY, ua=UA,
                                                     speed_bps=PAYLOAD_SIZE / 2)
    assert is_ok is True
    assert size == PAYLOAD_SIZE
    assert eta == pytest.approx(2.0)


def test_check_url_availability_reports_availability_without_a_size(oa):
    with ProbeServer(body=b"c" * 4096, head_status=405, range_status=501, chunked=True) as srv:
        assert oa.check_url_availability(f"{srv.base}/f.bin", PROXY, ua=UA) == (True, None, None)


def test_check_url_availability_reports_an_unreachable_url(oa):
    url = f"http://127.0.0.1:{_dead_port()}/f.bin"
    assert oa.check_url_availability(url, PROXY, ua=UA) == (False, None, None)
