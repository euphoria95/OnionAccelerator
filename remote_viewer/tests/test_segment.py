"""Parallel segmented walking: entering a header chain in several places at once.

The chain is serially dependent, so the only way to spend fewer round trips on it is to
start several walkers at once. That is safe because a header can be verified where it is
found — a RAR5 block by its CRC32, a RAR4 block by its CRC plus its successor, a tar
header by its magic and checksum — and because nothing rests on that verification being
right. Walker 0 starts where the format says the chain starts, and every later walker is
believed only once the walker before it *arrives* at the offset it began from.

So the tests that matter are the ones that break the guesses on purpose: a boundary
shifted by a few bytes, by a block, or into the middle of a member's data must all still
produce exactly the serial listing.
"""

from __future__ import annotations

import io
import zlib

import pytest

from rvtree.formats import rar, segment, tarwalk

RAR_FIXTURES = [
    "test.rar5.rar",
    "test.rar4.rar",
    "test.solid.rar5.rar",
]


def _source(path):
    with open(path, "rb") as fh:
        data = fh.read()
    fp = io.BytesIO(data)
    fp.size = len(data)  # type: ignore[attr-defined]
    return fp


def _entries(iterable):
    return [(e.path, e.size, e.kind, e.linkname, e.locator) for e in iterable]


def _rar_header_offsets(fp, info):
    win = rar._Window(fp)
    offsets, offset = [], info.body_offset
    for _ in range(1000):
        block = (
            rar._read_block5(win, offset) if info.version == 5 else rar._read_block4(win, offset)
        )
        end = rar.BLOCK_END_V5 if info.version == 5 else rar.BLOCK_END_V4
        if block is None or block.htype == end:
            break
        offsets.append(offset)
        offset = block.next
    return offsets


# ----------------------------------------------------------------- boundary finders


def test_rar5_boundary_is_found_by_its_own_checksum(fixtures):
    fp = _source(f"{fixtures}/test.rar5.rar")
    info = rar.read_info(fp)
    target = _rar_header_offsets(fp, info)[4]
    blob = fp.getvalue()[target - 3000 : target + 20_000]
    assert segment.find_rar5_boundary(blob, target - 3000) == target


def test_rar5_boundary_rejects_a_corrupted_checksum(fixtures):
    fp = _source(f"{fixtures}/test.rar5.rar")
    info = rar.read_info(fp)
    target = _rar_header_offsets(fp, info)[4]
    data = bytearray(fp.getvalue())
    data[target] ^= 0xFF  # break the CRC of that block and nothing else
    window = bytes(data[target : target + 2000])
    assert segment.find_rar5_boundary(window, target) != target


def test_rar5_boundary_finds_nothing_in_payload(fixtures):
    """Member data must not read as a header, or a walker would start inside a file."""
    fp = _source(f"{fixtures}/test.rar5.rar")
    info = rar.read_info(fp)
    first = _rar_header_offsets(fp, info)[1]
    # Well clear of any header: the middle of the first member's packed data.
    blob = fp.getvalue()[first + 500_000 : first + 700_000]
    assert segment.find_rar5_boundary(blob, first + 500_000) is None


def test_rar4_boundary_is_found_and_corroborated(fixtures):
    fp = _source(f"{fixtures}/test.rar4.rar")
    info = rar.read_info(fp)
    target = _rar_header_offsets(fp, info)[3]
    blob = fp.getvalue()[target - 1000 : target + 20_000]
    assert segment.find_rar4_boundary(blob, target - 1000) == target


def test_tar_boundary_agrees_with_scan_headers(fixtures):
    fp = _source(f"{fixtures}/test.tar")
    blob = fp.getvalue()[0:200_000]
    found = tarwalk.scan_headers(blob)
    assert segment.find_tar_boundary(blob, 0) == found[0][0]


def test_tar_boundaries_respect_512_alignment(fixtures):
    """The scan is aligned to the archive, not to wherever the probe happened to start."""
    fp = _source(f"{fixtures}/test.tar")
    base = 1000  # deliberately not a multiple of 512
    blob = fp.getvalue()[base : base + 100_000]
    at = segment.find_tar_boundary(blob, base)
    assert at is None or at % 512 == 0


def test_the_scan_ignores_absurdly_large_declared_headers():
    """The cap is what keeps a probe from checksumming tens of kilobytes per candidate."""
    assert segment.MAX_SCAN_HEADER <= 64 * 1024


# ----------------------------------------------------------------- probe planning


def test_probes_are_centred_on_their_split_point():
    """An archive of evenly sized members puts a header just *behind* every split point.

    Looking only forwards misses all of them, which reads like a format problem and is
    really an arithmetic one.
    """
    windows = segment.plan_probes(first=0, size=32 * 1024 * 1024, k=8, probe=512 * 1024)
    step = 32 * 1024 * 1024 // 8
    for i, (lo, hi) in enumerate(windows, start=1):
        target = i * step
        assert lo < target < hi, f"probe {i} does not straddle its split point"


def test_no_probes_when_the_segments_would_be_smaller_than_a_probe():
    assert segment.plan_probes(first=0, size=1024, k=8, probe=512 * 1024) == []


# ----------------------------------------------------------------- listings match


@pytest.mark.parametrize("name", RAR_FIXTURES)
@pytest.mark.parametrize("segments", [2, 4, 8, 16])
def test_segmented_rar_listing_matches_serial_exactly(fixtures, name, segments):
    """The gate on the whole feature: parallel must be indistinguishable from serial."""
    info = rar.read_info(_source(f"{fixtures}/{name}"))
    serial = _entries(rar.walk(_source(f"{fixtures}/{name}"), info=info))
    parallel = _entries(
        rar.walk_parallel(_source(f"{fixtures}/{name}"), info=info, segments=segments)
    )
    assert parallel == serial
    assert len(serial) == 14


@pytest.mark.parametrize("segments", [2, 4, 8, 16])
def test_segmented_tar_listing_matches_serial_exactly(fixtures, segments):
    serial = _entries(tarwalk.walk(_source(f"{fixtures}/test.tar")))
    parallel = _entries(tarwalk.walk_parallel(_source(f"{fixtures}/test.tar"), segments=segments))
    assert parallel == serial
    assert len(serial) == 14


def test_one_walker_is_the_serial_walk(fixtures):
    info = rar.read_info(_source(f"{fixtures}/test.rar5.rar"))
    serial = _entries(rar.walk(_source(f"{fixtures}/test.rar5.rar"), info=info))
    assert _entries(rar.walk_parallel(_source(f"{fixtures}/test.rar5.rar"), info=info, segments=1)) == serial


def test_a_small_archive_is_not_segmented(monkeypatch):
    """Below the threshold the probes would cost more than the walk they are shortening.

    Asserted by making any attempt to locate a boundary fail loudly: a small archive must
    never get that far.
    """
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name in ("a.txt", "b.txt"):
            info = tarfile.TarInfo(name)
            info.size = 10
            tf.addfile(info, io.BytesIO(b"0123456789"))
    small = io.BytesIO(buf.getvalue())
    small.size = len(buf.getvalue())  # type: ignore[attr-defined]
    assert small.size < segment.MIN_SEGMENTED_SIZE

    def refuse(*args, **kwargs):
        raise AssertionError("a small archive must not be probed for boundaries")

    monkeypatch.setattr(segment, "locate", refuse)
    assert [e.path for e in tarwalk.walk_parallel(small, segments=8)] == ["a.txt", "b.txt"]


@pytest.mark.parametrize("name", ["test.rar5.rar", "test.rar4.rar"])
def test_segmentation_shortens_the_longest_serial_chain(fixtures, name):
    """The whole point, stated as a number: no walker walks the whole chain alone.

    Over Tor the wall clock of a listing is set by the longest run of requests that have
    to happen one after another, because each one is a round trip and the next offset is
    not known until the previous answer lands. Segmentation divides exactly that. It is
    measured per segment rather than per thread: a thread pool reuses idle workers, so on
    a local fixture one worker can pick up several segments and thread-level counting
    would report a chain that never existed.

    The division is by *bytes*, not by members, so the gain tracks how evenly members are
    spread. These fixtures are deliberately lopsided — eight 4 MiB files plus a handful of
    tiny ones packed together — and the cluster lands in a single segment, which is why
    eight walkers cut the chain by a bit over half rather than by eight. An archive like
    the one in rv.log, 7,540 members at a fairly even 331 KiB apart, divides far better.
    """
    source = _source(f"{fixtures}/{name}")
    info = rar.read_info(source)
    total = len(list(rar.walk(source, info=info)))

    kind = segment.RAR5 if info.version == 5 else segment.RAR4
    starts = segment.locate(source, kind, info.body_offset, source.size, 8)
    depths = [
        len(rar.walk_segment(source, info, start,
                             starts[i + 1] if i + 1 < len(starts) else None)[0])
        for i, start in enumerate(starts)
    ]
    assert sum(depths) == total
    assert max(depths) <= total / 2, f"{total} members, deepest segment {max(depths)}: {depths}"


@pytest.mark.parametrize("limit", [1, 4, 13])
def test_limit_still_stops_the_walk(fixtures, limit):
    info = rar.read_info(_source(f"{fixtures}/test.rar5.rar"))
    serial = _entries(rar.walk(_source(f"{fixtures}/test.rar5.rar"), info=info))
    got = _entries(
        rar.walk_parallel(_source(f"{fixtures}/test.rar5.rar"), info=info, segments=8, limit=limit)
    )
    assert got == serial[:limit]


# ----------------------------------------------------------------- wrong guesses


@pytest.mark.parametrize("shift", [1, 3, 100, 5000, 1_000_000])
@pytest.mark.parametrize("name", RAR_FIXTURES)
def test_a_wrong_rar_boundary_is_repaired(fixtures, monkeypatch, name, shift):
    """A boundary that is not a header must cost time, never entries."""
    info = rar.read_info(_source(f"{fixtures}/{name}"))
    serial = _entries(rar.walk(_source(f"{fixtures}/{name}"), info=info))

    real = segment.locate

    def crooked(fp, kind, first, size, k, probe=segment.PROBE):
        found = real(fp, kind, first, size, k, probe)
        return [found[0], found[1] + shift] if len(found) > 1 else found

    monkeypatch.setattr(segment, "locate", crooked)
    assert _entries(rar.walk_parallel(_source(f"{fixtures}/{name}"), info=info, segments=2)) == serial


@pytest.mark.parametrize("shift", [7, 512, 1024, 100_000])
def test_a_wrong_tar_boundary_is_repaired(fixtures, monkeypatch, shift):
    serial = _entries(tarwalk.walk(_source(f"{fixtures}/test.tar")))

    real = segment.locate

    def crooked(fp, kind, first, size, k, probe=segment.PROBE):
        found = real(fp, kind, first, size, k, probe)
        return [found[0], found[1] + shift] if len(found) > 1 else found

    monkeypatch.setattr(segment, "locate", crooked)
    assert _entries(tarwalk.walk_parallel(_source(f"{fixtures}/test.tar"), segments=2)) == serial


def test_a_segment_that_will_not_parse_is_re_walked_not_raised(fixtures, monkeypatch):
    """A guess landing in compressed data makes the parser object. That is not an error."""
    info = rar.read_info(_source(f"{fixtures}/test.rar5.rar"))
    serial = _entries(rar.walk(_source(f"{fixtures}/test.rar5.rar"), info=info))

    def nonsense(fp, kind, first, size, k, probe=segment.PROBE):
        return [first, size // 2 + 12345]

    monkeypatch.setattr(segment, "locate", nonsense)
    assert _entries(rar.walk_parallel(_source(f"{fixtures}/test.rar5.rar"), info=info, segments=2)) == serial


def test_walker_zero_still_reports_a_genuinely_corrupt_archive(fixtures):
    """Speculation fails soft; the anchored walker must not, or corruption goes unnoticed."""
    data = bytearray(_source(f"{fixtures}/test.rar5.rar").getvalue())
    info = rar.read_info(_source(f"{fixtures}/test.rar5.rar"))
    # Wreck the very first block of the chain, which segment 0 owns.
    for i in range(info.body_offset, info.body_offset + 64):
        data[i] = 0xFF
    fp = io.BytesIO(bytes(data))
    fp.size = len(data)  # type: ignore[attr-defined]
    with pytest.raises(rar.RarError):
        list(rar.walk_parallel(fp, info=info, segments=4))
