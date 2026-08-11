"""Finding header boundaries in an archive without walking to them.

A RAR or tar header chain is serially *dependent*: entry ``i+1``'s offset is only known
once entry ``i`` has been parsed. Over Tor that is the whole cost of a listing — a real
two-hour run in ``rv.log`` spent 97.7% of its wall clock waiting on 7,540 round trips of
4 KiB each, one per member, because there was nothing else to do but ask for the next one.

The chain cannot be pipelined. It can, however, be *entered in several places at once*,
because both formats make a header verifiable on its own:

* RAR5 prefixes each block with a CRC32 over the header. Thirty-two bits is enough that a
  candidate offset either is a header or is not; false positives do not happen in
  practice.
* RAR4 carries only a 16-bit header CRC, so a candidate is corroborated by following the
  chain forward and requiring the next block to verify too.
* tar headers carry the ustar magic *and* a checksum, and sit on 512-byte boundaries.
  ``tarwalk.scan_headers`` already validates exactly that.

None of this has to be right, which is what makes it safe to use. The walkers are anchored
at the archive's real first header, and each one must *arrive* at the offset the next one
started from; a boundary that turns out to be wrong is detected there and the seam is
re-walked serially. Verification only has to be good enough to be usually right.
"""

from __future__ import annotations

import logging
import re
import struct
import zlib
from typing import Optional

from .tarwalk import BLOCK, scan_headers

log = logging.getLogger(__name__)

# One probe per segment. Sized against the header stride measured in rv.log — mean 331 KiB,
# median 124 KiB, 88% under 512 KiB — so a probe of this size usually contains a header.
PROBE = 512 * 1024

# A segment whose first probe found nothing gets one wider retry before being given up on
# and folded into its predecessor.
PROBE_RETRY = 4
MAX_PROBE = 2 * 1024 * 1024

# Below this there is no point: the probes would cost more than the serial walk they save.
MIN_SEGMENTED_SIZE = 8 * 1024 * 1024

RAR5, RAR4, TAR = "rar5", "rar4", "tar"


def _vint(buf: bytes, pos: int) -> Optional[tuple[int, int]]:
    """RAR5 variable-length integer, or ``None`` if these bytes are not one.

    A local copy rather than ``rar._vint`` because this runs at every byte offset of a
    probe window and must answer "no" by returning, not by raising.
    """
    value = shift = 0
    for _ in range(10):
        if pos >= len(buf):
            return None
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7
    return None


# Every candidate is found by a C-speed scan for a byte the header must contain, and only
# then checked properly. Testing a megabyte of probe one Python offset at a time is
# thousands of times slower and finds exactly the same headers.
_TYPE_BYTES_V5 = re.compile(rb"[\x01-\x05]")  # RAR5 block type, first vint of the body
_TYPE_BYTES_V4 = re.compile(rb"[\x72-\x7b]")  # RAR4 block type, third byte of the block

# How many bytes the header-size vint may occupy. MAX_HEADER_V5 is 1 MiB, which is three
# groups of seven bits; real headers use one or two.
_V5_SIZE_WIDTHS = (1, 2, 3)

# Largest header a *scan* will consider. The formats allow far more (64 KiB for RAR4, a
# megabyte for RAR5), but a candidate's declared size is checksummed before it is
# believed, so an unbounded limit means CRCing tens of kilobytes at every plausible byte
# of the probe — which turned a 2 MiB window into twenty seconds of work. Real headers are
# a few hundred bytes; a member whose header exceeds this is simply not used as a boundary
# and its segment folds into the one before it.
MAX_SCAN_HEADER = 8192


def find_rar5_boundary(blob: bytes, base: int = 0) -> Optional[int]:
    """First offset in ``blob`` holding a CRC-valid RAR5 block, as an absolute offset.

    A RAR5 block has no magic, but it does begin with a CRC32 over its own header — so a
    candidate either verifies or it does not, and thirty-two bits means a false positive
    is not something that happens.
    """
    n = len(blob)
    for match in _TYPE_BYTES_V5.finditer(blob):
        body_at = match.start()
        for width in _V5_SIZE_WIDTHS:
            off = body_at - 4 - width
            if off < 0:
                continue
            parsed = _vint(blob, off + 4)
            if parsed is None:
                continue
            size, start = parsed
            # The vint must be exactly this wide, or this candidate belongs to a
            # different alignment and will be considered on its own iteration.
            if start != body_at or size < 2 or size > MAX_SCAN_HEADER or start + size > n:
                continue
            if struct.unpack_from("<I", blob, off)[0] == zlib.crc32(blob[off + 4 : start + size]):
                return base + off
    return None


def find_rar4_boundary(blob: bytes, base: int = 0, confirm: int = 1) -> Optional[int]:
    """First offset holding a RAR4 block whose 16-bit CRC is corroborated by its successor.

    Sixteen bits alone would fire about once every 64 KiB of random data, which over a
    512 KiB probe is not a rare event. Requiring the block the candidate points at to
    verify as well makes it 32 bits' worth of evidence, and the arrival check downstream
    catches whatever still slips through.
    """
    for match in _TYPE_BYTES_V4.finditer(blob):
        off = match.start() - 2
        if off >= 0 and _verify_block4(blob, off, confirm):
            return base + off
    return None


def _verify_block4(blob: bytes, off: int, confirm: int) -> bool:
    n = len(blob)
    if off + 11 > n:
        return False
    head_size = struct.unpack_from("<H", blob, off + 5)[0]
    if head_size < 7 or head_size > MAX_SCAN_HEADER or off + head_size > n:
        return False
    if struct.unpack_from("<H", blob, off)[0] != (
        zlib.crc32(blob[off + 2 : off + head_size]) & 0xFFFF
    ):
        return False
    htype = blob[off + 2]
    if not 0x72 <= htype <= 0x7B:
        return False
    if confirm <= 0 or htype == 0x7B:  # the end block has no successor to corroborate it
        return True
    flags = struct.unpack_from("<H", blob, off + 3)[0]
    add_size = struct.unpack_from("<I", blob, off + 7)[0] if flags & 0x8000 else 0
    nxt = off + head_size + add_size
    if nxt <= off:
        return False
    if nxt + 11 > n:
        # The successor lies outside the probe, so it cannot be checked. Accept on the
        # strength of the CRC alone and let the arrival check be the backstop.
        return True
    return _verify_block4(blob, nxt, confirm - 1)


def find_tar_boundary(blob: bytes, base: int = 0) -> Optional[int]:
    """First 512-aligned tar header in ``blob``, validated by magic and checksum.

    Alignment is relative to the archive, not to the probe, so the scan starts at the
    first offset in the probe that is itself 512-aligned in the file.
    """
    skip = (-base) % BLOCK
    found = scan_headers(blob[skip:], base=base + skip)
    return found[0][0] if found else None


_FINDERS = {RAR5: find_rar5_boundary, RAR4: find_rar4_boundary, TAR: find_tar_boundary}


def plan_probes(first: int, size: int, k: int, probe: int) -> list[tuple[int, int]]:
    """Where to look for the ``k-1`` boundaries after the archive's known first header.

    Segment starts are spread evenly over what is left of the archive, and each probe is
    *centred* on its split point rather than starting there. A split point is a target,
    not a floor — nothing says the boundary has to be after it, and an archive whose
    members happen to be evenly sized puts a header a few bytes on the wrong side of every
    one of them. Looking only forwards misses all of them and looks like a format problem.

    Walker 0 needs no probe: it begins at the header the format itself points to, which is
    the anchor the whole scheme's correctness hangs off.
    """
    span = size - first
    if k < 2 or span <= 0:
        return []
    step = span // k
    if step <= probe:
        return []  # segments no bigger than a probe: the probes would be the whole walk
    out = []
    for i in range(1, k):
        target = first + i * step
        lo = max(first + 1, target - probe // 2)
        hi = min(lo + probe, size) - 1
        if hi > lo:
            out.append((lo, hi))
    return out


def locate(fp, kind: str, first: int, size: int, k: int, probe: int = PROBE) -> list[int]:
    """Boundaries for ``k`` segments, as absolute offsets, starting with ``first``.

    One batched round trip finds them all: this is what ``get_ranges`` exists for. A
    segment whose probe found nothing is dropped, which simply hands its bytes to the
    walker before it.
    """
    finder = _FINDERS[kind]
    starts = [first]
    windows = plan_probes(first, size, k, probe)
    if not windows:
        return starts

    blobs = _read_many(fp, windows)
    missed: list[int] = []
    for i, (blob, (lo, _hi)) in enumerate(zip(blobs, windows)):
        at = finder(blob, lo)
        if at is None:
            missed.append(i)
        elif at > starts[-1]:
            starts.append(at)

    if missed and probe < MAX_PROBE:
        # A miss means the members here are further apart than the probe is wide, not that
        # there is nothing to find. One wider look, still in parallel, before giving up.
        wider = min(probe * PROBE_RETRY, MAX_PROBE)
        wide_windows = plan_probes(first, size, k, wider)
        retry = [wide_windows[i] for i in missed if i < len(wide_windows)]
        if retry:
            log.debug("%d segment probe(s) found nothing; retrying at %d KiB", len(retry), wider // 1024)
            for blob, (lo, _hi) in zip(_read_many(fp, retry), retry):
                at = finder(blob, lo)
                if at is not None and at > starts[-1]:
                    starts.append(at)
            starts.sort()

    log.info("segmented walk: %d of %d segment(s) found a verified header", len(starts), k)
    return starts


def _read_many(fp, spans: list[tuple[int, int]]) -> list[bytes]:
    """Fetch probe windows, in parallel when the transport can do it.

    The fallback is the same positional-read duck type every format parser here uses, so
    a plain ``BytesIO`` works and the tests need no transport at all.
    """
    transport = getattr(fp, "transport", None)
    url = getattr(fp, "url", None)
    if transport is not None and url and hasattr(transport, "get_ranges"):
        return transport.get_ranges(url, spans)
    out = []
    for lo, hi in spans:
        if hasattr(fp, "pread"):
            out.append(fp.pread(lo, hi - lo + 1))
        else:
            fp.seek(lo)
            out.append(fp.read(hi - lo + 1))
    return out
