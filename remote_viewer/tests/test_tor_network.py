"""Live Tor tests. Opt in with ``pytest -m network``; excluded by default.

These run against a real, stable, publicly mirrored file so the assertions can be
concrete. The whole point of the tool is that this stays cheap: the assertions below
are satisfied by roughly 3.5 MB of transfer, not by the 140 MB archive.
"""

from __future__ import annotations

import pytest

from rvtree import archive
from rvtree.formats import detect, tarwalk, xz
from rvtree.transport import DEFAULT_PROXY, Transport, probe

KERNEL_URL = "https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.6.tar.xz"
KERNEL_SIZE = 140_064_536
KERNEL_BLOCKS = 57

pytestmark = pytest.mark.network


@pytest.fixture
def tor():
    t = Transport(proxy=DEFAULT_PROXY, circuits=4, timeout=(60, 180))
    yield t
    t.close()


def test_probe_reports_range_support(tor):
    caps = probe(tor, KERNEL_URL)
    assert caps.size == KERNEL_SIZE
    assert caps.accepts_ranges is True


def test_verification_still_works_against_a_real_certificate():
    """Only a live CA-issued certificate can prove --verify-tls is not merely inert.

    The offline suite shows a self-signed one being rejected; this shows a valid one
    being accepted, which together say the flag does what it claims.
    """
    t = Transport(proxy=DEFAULT_PROXY, circuits=1, timeout=(60, 180), verify=True)
    try:
        assert probe(t, KERNEL_URL).size == KERNEL_SIZE
    finally:
        t.close()


def test_multirange_is_rejected_by_this_cdn(tor):
    """Recorded because it is the reason nothing may depend on multi-range requests."""
    caps = probe(tor, KERNEL_URL, check_multirange=True)
    assert caps.multirange is False


def test_index_reveals_block_map_for_a_few_hundred_bytes(tor):
    arc = archive.open_archive(tor, KERNEL_URL)
    assert arc.fmt == detect.TAR_XZ
    index = arc.xz_index
    assert len(index.blocks) == KERNEL_BLOCKS
    assert index.seekable is True
    assert index.uncompressed_size == 1_419_089_920
    # Footer, index and the bounded sniff prefix only.
    assert tor.bytes_fetched < 128 * 1024


def test_first_block_yields_thousands_of_headers(tor):
    """One 3.5 MB range request out of a 140 MB archive should surface a real subtree.

    Block 0 covers the first 24 MiB of the tar, which in this archive runs from the
    top-level files into ``Documentation/devicetree`` — so the assertions below stay
    inside that range deliberately.
    """
    arc = archive.open_archive(tor, KERNEL_URL)
    entries = list(archive.list_archive(arc, circuits=1, limit_blocks=1))
    paths = {e.path for e in entries}
    assert len(entries) > 5000
    assert "linux-6.6/COPYING" in paths
    assert "linux-6.6/CREDITS" in paths
    assert any(p.startswith("linux-6.6/Documentation/") for p in paths)
    assert tor.bytes_fetched < 8 * 1024 * 1024


def test_speculative_scan_finds_headers_in_an_isolated_block(tor):
    """Blocks are independently decodable, so a block can be scanned on its own."""
    arc = archive.open_archive(tor, KERNEL_URL)
    block = arc.xz_index.blocks[0]
    raw = arc.source.pread(block.comp_offset, block.unpadded_size)
    plain = xz.decode_block(raw, block)
    assert len(plain) == block.uncomp_size
    found = tarwalk.scan_headers(plain)
    assert len(found) > 5000
    assert tarwalk.header_density(plain) > 100


def test_extract_a_single_file_from_the_remote_tarball(tor):
    arc = archive.open_archive(tor, KERNEL_URL)
    data = archive.extract(arc, "linux-6.6/README")
    assert data.startswith(b"Linux kernel")
    assert tor.bytes_fetched < 12 * 1024 * 1024
