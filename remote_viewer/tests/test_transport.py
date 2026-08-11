"""Transport behaviour, encoding the two rejections observed against a real CDN.

Probing ``cdn.kernel.org`` during design turned up two things that a naive
implementation gets wrong, both answered with HTTP 501:

* ``Range: bytes=-12`` (a suffix range) is refused, so archive footers must be read
  via an absolute range computed from Content-Length.
* Multiple disjoint ranges in one request are refused, so nothing may depend on them.

These tests pin both down.
"""

from __future__ import annotations

import pytest

from rvtree import archive
from rvtree.transport import HttpRangeFile, Transport
from rvtree.transport.tor import RangeNotHonoured, TransportError, _validate_content_range


def _serve(fixtures, **kwargs):
    from rangeserver import RangeServer

    return RangeServer(fixtures, **kwargs)


# ----------------------------------------------------------------- range discipline


@pytest.mark.parametrize(
    "name",
    ["test.zip", "test.7z", "test.multiblock.tar.xz", "test.tar", "test.rar5.rar"],
)
def test_never_sends_a_suffix_range(transport, server, name):
    arc = archive.open_archive(transport, f"{server.base}/{name}")
    list(archive.list_archive(arc))
    suffixes = [r for r in server.requests if r.startswith("bytes=-")]
    assert suffixes == [], f"emitted suffix ranges: {suffixes}"


@pytest.mark.parametrize("name", ["test.zip", "test.multiblock.tar.xz"])
def test_works_against_a_server_that_501s_on_suffix_ranges(fixtures, name):
    """The end-to-end proof of the above: a hostile-to-suffix server changes nothing."""
    with _serve(fixtures, reject_suffix=True) as srv:
        t = Transport(proxy=None, circuits=1)
        try:
            arc = archive.open_archive(t, f"{srv.base}/{name}")
            entries = list(archive.list_archive(arc))
            assert len(entries) == 14
        finally:
            t.close()


def test_aborts_when_server_ignores_range(fixtures):
    """A 200 to a Range request means the whole entity is coming; stop before the body."""
    with _serve(fixtures, ignore_ranges=True) as srv:
        t = Transport(proxy=None, circuits=1, retries=1)
        try:
            with pytest.raises(RangeNotHonoured):
                fp = HttpRangeFile(t, f"{srv.base}/test.zip")
                fp.pread(0, 16)
        finally:
            t.close()


def test_multirange_probe_reports_unsupported(transport, server):
    from rvtree.transport import probe

    caps = probe(transport, f"{server.base}/test.zip", check_multirange=True)
    assert caps.accepts_ranges is True
    assert caps.multirange is False


# ----------------------------------------------------------------- proxy safety


def test_refuses_socks5_because_it_leaks_dns():
    with pytest.raises(TransportError, match="socks5h"):
        Transport(proxy="socks5://127.0.0.1:9150")


def test_accepts_socks5h():
    t = Transport(proxy="socks5h://127.0.0.1:9150", circuits=1)
    assert t.proxy == "socks5h://127.0.0.1:9150"
    t.close()


def test_isolated_circuits_get_distinct_credentials():
    t = Transport(proxy="socks5h://127.0.0.1:9150", circuits=4)
    try:
        creds = set()
        for _ in range(4):
            c = t._acquire()
            creds.add(c.session.proxies["http"])
        assert len(creds) == 4, "circuits must not share a SOCKS credential"
    finally:
        t.close()


# ----------------------------------------------------------------- response checks


def test_content_range_mismatch_is_rejected():
    with pytest.raises(TransportError):
        _validate_content_range("bytes 0-99/1000", 0, 199)


def test_content_range_accepted_when_it_matches():
    _validate_content_range("bytes 10-19/1000", 10, 19)


def test_seek_from_end_becomes_an_absolute_range(transport, server):
    fp = HttpRangeFile(transport, f"{server.base}/test.zip")
    fp.seek(-12, 2)
    assert fp.tell() == fp.size - 12
    fp.read(12)
    assert not any(r.startswith("bytes=-") for r in server.requests)


# ----------------------------------------------------------------- readahead


def test_readahead_shrinks_when_buffer_goes_unused(transport, server):
    """Skipping through a tar of large members should not drag whole buffers along."""
    from rvtree.transport.httpfile import MIN_READAHEAD

    fp = HttpRangeFile(transport, f"{server.base}/test.tar")
    for i in range(8):
        fp.seek(i * 4_000_000)
        fp.read(512)
    assert fp._readahead <= MIN_READAHEAD * 2
