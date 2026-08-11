"""RAR listing and extraction, hand-rolled, because nothing in the stdlib reads RAR.

RAR keeps no central index at all: every file header sits immediately after the previous
member's packed data, so enumerating an archive means walking that chain. That is the
same shape as tar, with one advantage — the headers are self-describing and this module
drives its own reads, so it pulls a small window per header instead of paying
``HttpRangeFile``'s 16 KiB readahead floor. On a 100 GB archive with ten thousand members
that is the difference between roughly 10 MB fetched and 160 MB.

Two generations are handled, sharing nothing but their first four bytes. RAR4
(``Rar!\\x1a\\x07\\x00``) is fixed-layout little-endian structs; RAR5
(``Rar!\\x1a\\x07\\x01\\x00``) is a vint-based redesign. They are parsed separately and
normalised onto the same ``Entry``.

Only stored members can be read here. RAR's compression is proprietary, has no stdlib
decoder and no credible pure-Python one, so a compressed member is spliced into a
synthetic archive built from this archive's own verbatim headers and handed to an
installed ``unrar``. See ``synthesize``.
"""

from __future__ import annotations

import calendar
import dataclasses
import logging
import os
import shutil
import struct
import subprocess
import tempfile
import zlib
from concurrent.futures import ThreadPoolExecutor
from typing import Iterator, Optional

from . import segment
from .. import progress
from ..model import DIR, FILE, HARDLINK, OTHER, SYMLINK, Entry
from ..util import human_bytes

log = logging.getLogger(__name__)

SIGNATURE_V4 = b"Rar!\x1a\x07\x00"
SIGNATURE_V5 = b"Rar!\x1a\x07\x01\x00"

# unrar itself searches about a megabyte for the mark block, because a self-extracting
# archive carries an executable stub in front of it. We match that, but only ever after
# offset 0 has been ruled out — see ``find_signature``.
SFX_SCAN_LIMIT = 1 << 20

WINDOW = 4096          # one header read; covers the fixed fields plus a long name
MAX_HEADER_V4 = 0x10000  # HEAD_SIZE is a uint16, so this is the format's own ceiling
MAX_HEADER_V5 = 1 << 20  # HeaderSize is a vint; this bound is ours, not the format's
MAX_NAME = 64 * 1024
MAX_BLOCKS = 5_000_000   # a header chain longer than this is corruption, not an archive
MAX_INLINE_LINK = 4096   # a RAR4 link target is member data; read it only when tiny

# RAR4 block types.
BLOCK_MAIN_V4 = 0x73
BLOCK_FILE_V4 = 0x74
BLOCK_NEWSUB_V4 = 0x7A   # identical layout to FILE, but a service record, not a member
BLOCK_END_V4 = 0x7B

# RAR5 block types.
BLOCK_MAIN_V5 = 1
BLOCK_FILE_V5 = 2
BLOCK_SERVICE_V5 = 3
BLOCK_CRYPT_V5 = 4
BLOCK_END_V5 = 5

# The helpers that can decompress a RAR, in preference order. ``unrar`` is the reference
# implementation; 7-Zip carries its own RAR reader and is a common substitute.
_HELPERS = (("unrar", "x", "-inul", "-p-", "-y"), ("7z", "x", "-bso0", "-bsp0", "-bse0", "-y"))


class RarError(RuntimeError):
    pass


class EncryptedArchive(RarError):
    """Headers or member data need a password we do not have."""


class MultiVolumeArchive(RarError):
    """The member continues into another volume, which is a different URL."""


class HelperUnavailable(RarError):
    """The member is compressed and no external RAR decompressor is installed."""


class HelperFailed(RarError):
    """The helper ran but did not hand back the bytes the header promised."""


@dataclasses.dataclass
class RarInfo:
    """Everything learned from the signature and the main archive header."""

    version: int          # 4 or 5
    base_offset: int      # where the signature starts; non-zero for a self-extracting archive
    body_offset: int      # the first block after the main header
    main_offset: int      # the main header itself, for copying into a synthetic archive
    main_size: int
    volume: bool = False
    solid: bool = False
    recovery: bool = False
    volume_number: Optional[int] = None

    @property
    def signature(self) -> bytes:
        return SIGNATURE_V5 if self.version == 5 else SIGNATURE_V4


# ------------------------------------------------------------------- reading


def _pread(fp, offset: int, length: int) -> bytes:
    if hasattr(fp, "pread"):
        return fp.pread(offset, length)
    fp.seek(offset)
    return fp.read(length)


class _Window:
    """One cached positional read, sized for headers rather than for member data.

    The headers of small members fall inside a single window, so one request covers many
    entries; headers separated by megabytes of data cost one small request each. Going
    through the file object's own buffer instead would charge the 16 KiB readahead floor
    for every one of those hops.
    """

    def __init__(self, fp, size: int = WINDOW):
        self.fp = fp
        self.size = size
        self.total = getattr(fp, "size", 0) or 0
        self._start = 0
        self._buf = b""

    def read(self, offset: int, length: int) -> bytes:
        if length <= 0 or (self.total and offset >= self.total):
            return b""
        if not (self._start <= offset and offset + length <= self._start + len(self._buf)):
            want = max(length, self.size)
            if self.total:
                want = min(want, self.total - offset)
            self._buf = _pread(self.fp, offset, want)
            self._start = offset
        lo = offset - self._start
        return self._buf[lo : lo + length]


def _vint(buf: bytes, pos: int) -> tuple[int, int]:
    """Decode a RAR5 variable-length integer, returning (value, next position).

    Ten bytes is the ceiling: 64 bits at seven bits a byte. A longer run is corruption
    rather than a very large number, and must not be allowed to walk off the buffer.
    """
    value = shift = 0
    for _ in range(10):
        if pos >= len(buf):
            raise RarError("truncated variable-length integer in a RAR5 header")
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7
    raise RarError("over-long variable-length integer in a RAR5 header")


def _vint_bytes(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | 0x80 if value else byte)
        if not value:
            return bytes(out)


def find_signature(fp) -> tuple[int, int]:
    """Locate the RAR mark block, returning (offset, version).

    Offset zero is checked first and costs nothing. Only when that fails do we scan, and
    then only because a self-extracting archive puts an executable stub in front of the
    signature — the same situation ``detect._has_eocd`` handles for self-extracting zips,
    and bounded for the same reason: this runs over a link where a megabyte is not free.
    """
    head = _pread(fp, 0, len(SIGNATURE_V5))
    if head.startswith(SIGNATURE_V5):
        return 0, 5
    if head.startswith(SIGNATURE_V4):
        return 0, 4

    size = getattr(fp, "size", 0) or 0
    window = min(size, SFX_SCAN_LIMIT)
    # Worth announcing: this is one large read over a slow link, and from the outside it
    # is indistinguishable from a hang.
    log.info("no signature at offset 0; scanning the first %s for an SFX stub", human_bytes(window))
    progress.get().stage("sfx scan", human_bytes(window))
    blob = _pread(fp, 0, window)
    for signature, version in ((SIGNATURE_V5, 5), (SIGNATURE_V4, 4)):
        at = blob.find(signature)
        if at >= 0:
            return at, version
    raise RarError(
        f"no RAR signature in the first {human_bytes(window)}; this is not a RAR archive"
    )


# ------------------------------------------------------------------- RAR 4.x blocks


@dataclasses.dataclass
class _BlockV4:
    offset: int
    htype: int
    flags: int
    head_size: int
    add_size: int
    body: bytes

    @property
    def data_offset(self) -> int:
        return self.offset + self.head_size

    @property
    def next(self) -> int:
        return self.offset + self.head_size + self.add_size


def _read_block4(win: _Window, offset: int) -> Optional[_BlockV4]:
    raw = win.read(offset, WINDOW)
    if len(raw) < 11:
        return None  # not even a base header left: a clean end, as tarwalk treats EOF

    htype = raw[2]
    flags, head_size = struct.unpack_from("<HH", raw, 3)
    if head_size < 7 or head_size > MAX_HEADER_V4:
        raise RarError(f"RAR4 block at {offset} declares a {head_size}-byte header")
    if head_size > len(raw):
        raw = win.read(offset, head_size)
        if len(raw) < head_size:
            raise RarError(f"RAR4 header at {offset} runs past the end of the archive")

    body = raw[:head_size]
    add_size = 0
    if flags & 0x8000:  # LONG_BLOCK: a data area follows the header
        if head_size < 11:
            raise RarError(f"RAR4 block at {offset} promises a data area but has no room for it")
        add_size = struct.unpack_from("<I", body, 7)[0]
    return _BlockV4(offset, htype, flags, head_size, add_size, body)


def _decode_oem(raw: bytes) -> str:
    """A RAR4 name with no unicode flag. Linux ``rar`` writes UTF-8 here; DOS wrote OEM."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp437", "replace")


def _decode_name4(raw: bytes, unicode_flag: bool) -> str:
    """Decode a RAR4 FILE_NAME field.

    With LHD_UNICODE the field is ``ascii_name \\0 encoded``, where ``encoded`` is RAR's
    own scheme: a shared high byte, then two-bit opcodes packed four to a byte, most
    significant pair first. Opcode 3 back-references the *whole* raw field from index
    zero, which is the detail every reimplementation of this gets wrong.

    A malformed name must never abort a listing, so anything unexpected degrades to the
    ASCII half rather than raising.
    """
    if not unicode_flag:
        return _decode_oem(raw)
    split = raw.find(b"\0")
    if split < 0:
        return raw.decode("utf-8", "replace")

    encoded = raw[split + 1 :]
    if not encoded:
        return _decode_oem(raw[:split])

    try:
        high = encoded[0] << 8
        out: list[int] = []
        pos, bits, flags = 1, 0, 0
        while pos < len(encoded) and len(out) < MAX_NAME:
            if bits == 0:
                flags = encoded[pos]
                pos += 1
                bits = 8
                if pos >= len(encoded):
                    break
            bits -= 2
            op = (flags >> bits) & 3
            if op == 0:
                out.append(encoded[pos])
                pos += 1
            elif op == 1:
                out.append(encoded[pos] + high)
                pos += 1
            elif op == 2:
                out.append(encoded[pos] | (encoded[pos + 1] << 8))
                pos += 2
            else:
                length = encoded[pos]
                pos += 1
                if length & 0x80:
                    correction = encoded[pos]
                    pos += 1
                    for _ in range((length & 0x7F) + 2):
                        if len(out) >= len(raw):
                            break
                        out.append(((raw[len(out)] + correction) & 0xFF) + high)
                else:
                    for _ in range(length + 2):
                        if len(out) >= len(raw):
                            break
                        out.append(raw[len(out)])
        return "".join(chr(c) for c in out if c < 0x110000)
    except (IndexError, ValueError):
        return _decode_oem(raw[:split])


def _dos_to_unix(value: int) -> Optional[float]:
    """MS-DOS packed datetime. Read as UTC, the same convention ``zipfmt`` uses."""
    if not value:
        return None
    try:
        return float(
            calendar.timegm(
                (
                    ((value >> 25) & 0x7F) + 1980,
                    (value >> 21) & 0x0F,
                    (value >> 16) & 0x1F,
                    (value >> 11) & 0x1F,
                    (value >> 5) & 0x3F,
                    (value & 0x1F) * 2,
                    0,
                    0,
                    -1,
                )
            )
        )
    except (ValueError, OverflowError):
        return None


def _entry4(win: _Window, blk: _BlockV4, links: bool) -> Optional[Entry]:
    body = blk.body
    if blk.head_size < 32:
        raise RarError(f"RAR4 file header at {blk.offset} is too short to hold its fields")

    pack, unp, host_os, crc, ftime, _ver, method, name_size, attr = struct.unpack_from(
        "<IIBIIBBHI", body, 7
    )
    pos = 32
    if blk.flags & 0x0100:  # LHD_LARGE: the sizes carry a second 32 bits
        high_pack, high_unp = struct.unpack_from("<II", body, pos)
        pos += 8
        pack |= high_pack << 32
        unp |= high_unp << 32
    if name_size > MAX_NAME or pos + name_size > blk.head_size:
        raise RarError(f"RAR4 file header at {blk.offset} declares a {name_size}-byte name")
    name = _decode_name4(body[pos : pos + name_size], bool(blk.flags & 0x0200))

    unix = host_os == 3
    kind = FILE
    if unix and (attr & 0xF000) == 0xA000:
        kind = SYMLINK
    elif unix and (attr & 0xF000) == 0x4000:
        kind = DIR
    elif (blk.flags & 0x00E0) == 0x00E0:
        # The window-size bits all set is RAR4's directory marker. Attributes are
        # consulted first because those same bits also mean "4 MiB dictionary".
        kind = DIR
    elif not unix and attr & 0x10:
        kind = DIR

    linkname = ""
    if kind == SYMLINK and links and method == 0x30 and 0 < pack <= MAX_INLINE_LINK:
        # RAR4 keeps a link target in the member's data rather than its header. This is
        # the one place the walk touches data, and it is bounded for that reason.
        linkname = win.read(blk.data_offset, pack).decode("utf-8", "replace")

    return Entry(
        path=_normalise(name),
        size=unp,
        csize=pack,
        mtime=_dos_to_unix(ftime),
        mode=attr & 0xFFFF if unix else None,
        kind=kind,
        linkname=linkname,
        locator={
            "offset": blk.data_offset,
            "csize": pack,
            "size": unp,
            "method": max(0, method - 0x30),
            "solid": bool(blk.flags & 0x0010),
            "split": bool(blk.flags & 0x0003),
            "crypt": bool(blk.flags & 0x0004),
            "crc": crc,
            "hdr": (blk.offset, blk.head_size),
        },
    )


# ------------------------------------------------------------------- RAR 5.0 blocks


@dataclasses.dataclass
class _BlockV5:
    offset: int
    hdr_size: int    # bytes from ``offset`` to the start of the data area
    data_size: int
    htype: int
    flags: int
    extra_size: int
    body: bytes
    pos: int         # cursor into ``body``, just past the fields common to every block

    @property
    def data_offset(self) -> int:
        return self.offset + self.hdr_size

    @property
    def next(self) -> int:
        return self.data_offset + self.data_size


def _read_block5(win: _Window, offset: int) -> Optional[_BlockV5]:
    raw = win.read(offset, WINDOW)
    if len(raw) < 6:
        return None

    size, start = _vint(raw, 4)  # header size, counted from the header type onwards
    if size < 2 or size > MAX_HEADER_V5:
        raise RarError(f"RAR5 block at {offset} declares a {size}-byte header")
    if start + size > len(raw):
        raw = win.read(offset, start + size)
        if len(raw) < start + size:
            raise RarError(f"RAR5 header at {offset} runs past the end of the archive")

    body = raw[start : start + size]
    htype, pos = _vint(body, 0)
    flags, pos = _vint(body, pos)
    extra_size = data_size = 0
    if flags & 0x0001:
        extra_size, pos = _vint(body, pos)
    if flags & 0x0002:
        data_size, pos = _vint(body, pos)
    if extra_size > size:
        raise RarError(f"RAR5 block at {offset} has an extra area larger than its header")
    return _BlockV5(offset, start + size, data_size, htype, flags, extra_size, body, pos)


def _read_extra5(extra: bytes) -> tuple[int, str, bool]:
    """Scan a RAR5 extra area for the records that change how a member is presented.

    Returns the redirection kind, its target, and whether the member is encrypted.
    Unknown record types are skipped by their own length, which is what lets a RAR 7
    archive parse here.
    """
    redir_kind, target, encrypted = 0, "", False
    pos = 0
    while pos < len(extra):
        try:
            size, start = _vint(extra, pos)
        except RarError:
            break
        if size <= 0 or start + size > len(extra):
            break
        record = extra[start : start + size]
        pos = start + size
        try:
            rtype, at = _vint(record, 0)
            if rtype == 0x01:
                encrypted = True
            elif rtype == 0x05:  # FHEXTRA_REDIR
                redir_kind, at = _vint(record, at)
                _flags, at = _vint(record, at)
                length, at = _vint(record, at)
                target = record[at : at + length].decode("utf-8", "replace")
        except RarError:
            break
    return redir_kind, target, encrypted


def _entry5(blk: _BlockV5) -> Entry:
    body, pos = blk.body, blk.pos
    file_flags, pos = _vint(body, pos)
    unp_size, pos = _vint(body, pos)
    attrs, pos = _vint(body, pos)

    mtime = None
    if file_flags & 0x0002:
        mtime = float(struct.unpack_from("<I", body, pos)[0])
        pos += 4
    crc = None
    if file_flags & 0x0004:
        crc = struct.unpack_from("<I", body, pos)[0]
        pos += 4

    comp_info, pos = _vint(body, pos)
    host_os, pos = _vint(body, pos)
    name_size, pos = _vint(body, pos)
    if name_size > MAX_NAME or pos + name_size > len(body):
        raise RarError(f"RAR5 file header at {blk.offset} declares a {name_size}-byte name")
    name = body[pos : pos + name_size].decode("utf-8", "replace")

    redir_kind, target, encrypted = 0, "", False
    if blk.extra_size:
        redir_kind, target, encrypted = _read_extra5(body[len(body) - blk.extra_size :])

    unix = host_os == 1
    kind = FILE
    if redir_kind in (1, 2, 3):
        kind = SYMLINK
    elif redir_kind == 4:
        kind = HARDLINK
    elif redir_kind == 5:
        kind = OTHER
    elif file_flags & 0x0001:
        kind = DIR
    elif not unix and attrs & 0x10:
        kind = DIR

    return Entry(
        # The size field is present but meaningless when the encoder did not know it.
        path=_normalise(name),
        size=0 if file_flags & 0x0008 else unp_size,
        csize=blk.data_size,
        mtime=mtime,
        mode=attrs & 0xFFFF if unix else None,
        kind=kind,
        linkname=target,
        locator={
            "offset": blk.data_offset,
            "csize": blk.data_size,
            "size": unp_size,
            "method": (comp_info >> 7) & 7,
            "solid": bool(comp_info & 0x40),
            "split": bool(blk.flags & 0x0018),
            "crypt": encrypted,
            "crc": crc,
            "hdr": (blk.offset, blk.hdr_size),
        },
    )


def _normalise(name: str) -> str:
    """RAR4 stores Windows paths with backslashes; the rest of rvtree speaks slashes."""
    return name.replace("\\", "/").lstrip("/")


# ------------------------------------------------------------------- the walk


def read_info(fp) -> RarInfo:
    """Read the signature and main header. Both listing and extraction need them."""
    info = _read_info(fp)
    log.info(
        "RAR %s, solid %s, volume %s%s",
        "5.0" if info.version == 5 else "4.x",
        "yes" if info.solid else "no",
        "yes" if info.volume else "no",
        f", sfx stub {human_bytes(info.base_offset)}" if info.base_offset else "",
    )
    return info


def _read_info(fp) -> RarInfo:
    base, version = find_signature(fp)
    win = _Window(fp)
    offset = base + len(SIGNATURE_V5 if version == 5 else SIGNATURE_V4)

    if version == 5:
        blk5 = _read_block5(win, offset)
        if blk5 is None:
            raise RarError("this RAR5 archive ends immediately after its signature")
        if blk5.htype == BLOCK_CRYPT_V5:
            raise EncryptedArchive(
                "this archive's headers are encrypted (rar -hp), so not even the file "
                "names can be read without the password"
            )
        if blk5.htype != BLOCK_MAIN_V5:
            raise RarError(f"expected a RAR5 main header, found block type {blk5.htype}")
        flags, pos = _vint(blk5.body, blk5.pos)
        number = None
        if flags & 0x0002:
            number, pos = _vint(blk5.body, pos)
        return RarInfo(
            version=5,
            base_offset=base,
            body_offset=blk5.next,
            main_offset=blk5.offset,
            main_size=blk5.hdr_size,
            volume=bool(flags & 0x0001),
            solid=bool(flags & 0x0004),
            recovery=bool(flags & 0x0008),
            volume_number=number,
        )

    blk4 = _read_block4(win, offset)
    if blk4 is None or blk4.htype != BLOCK_MAIN_V4:
        raise RarError("expected a RAR4 main header after the signature")
    if blk4.flags & 0x0080:
        raise EncryptedArchive(
            "this archive's headers are encrypted (rar -hp), so not even the file names "
            "can be read without the password"
        )
    return RarInfo(
        version=4,
        base_offset=base,
        body_offset=blk4.next,
        main_offset=blk4.offset,
        main_size=blk4.head_size,
        volume=bool(blk4.flags & 0x0001),
        solid=bool(blk4.flags & 0x0008),
        recovery=bool(blk4.flags & 0x0040),
    )


class Cursor:
    """Where a bounded walk stopped.

    A segmented walk has to know whether the walker before it *arrived* at the offset it
    started from, since that is the only thing standing between a speculatively located
    boundary and a wrong listing. A generator cannot hand back a value, so it writes here.
    """

    __slots__ = ("offset",)

    def __init__(self, offset: int = 0):
        self.offset = offset


def walk(
    fp,
    info: Optional[RarInfo] = None,
    limit: Optional[int] = None,
    links: bool = True,
    start: Optional[int] = None,
    stop: Optional[int] = None,
    cursor: Optional[Cursor] = None,
    publish: bool = True,
) -> Iterator[Entry]:
    """Yield entries lazily by following the header chain.

    Member data is never read, so the cost is set by how many headers there are rather
    than by how big the archive is. The one exception is a RAR4 symlink, whose target
    lives in the data area; that read is bounded and can be switched off.

    ``start`` and ``stop`` bound the walk to one stretch of the chain, which is what lets
    several walkers cover an archive at once. ``publish`` is switched off for those,
    because K walkers pushing their own position would fight over one progress display.
    """
    info = info or read_info(fp)
    win = _Window(fp)
    offset = info.body_offset if start is None else start
    yielded = 0

    for _ in range(MAX_BLOCKS):
        if cursor is not None:
            cursor.offset = offset
        if win.total and offset >= win.total:
            return
        if stop is not None and offset >= stop:
            return

        if info.version == 5:
            blk5 = _read_block5(win, offset)
            if blk5 is None:
                return
            if blk5.htype == BLOCK_CRYPT_V5:
                raise EncryptedArchive("this archive's headers are encrypted from here on")
            if blk5.htype == BLOCK_END_V5:
                return
            # Type 3 is a service record — CMT, QO, ACL, STM, RR. It shares the file
            # header's layout exactly, so yielding it would invent members that the
            # archive does not contain.
            entry = _entry5(blk5) if blk5.htype == BLOCK_FILE_V5 else None
            nxt = blk5.next
        else:
            blk4 = _read_block4(win, offset)
            if blk4 is None or blk4.htype == BLOCK_END_V4:
                return
            if not 0x72 <= blk4.htype <= 0x7B:
                return  # unknown block type: stop cleanly, keeping what we already have
            # 0x7a NEWSUB is RAR4's service record and has FILE's exact layout.
            entry = _entry4(win, blk4, links) if blk4.htype == BLOCK_FILE_V4 else None
            nxt = blk4.next

        if entry is not None:
            yield entry
            yielded += 1
            if limit and yielded >= limit:
                return

        if nxt <= offset or (win.total and nxt > win.total):
            raise RarError(
                f"corrupt RAR header chain: the block at {offset} points to {nxt}"
            )
        offset = nxt
        # The walker reads through its own window rather than the file object, so its
        # position is not observable from outside; publish it. The offset is monotonic,
        # which the check just above is what guarantees.
        if publish:
            progress.get().position(offset, win.total)

    raise RarError(f"gave up after {MAX_BLOCKS} RAR blocks; the header chain does not end")


def walk_segment(
    fp, info: RarInfo, start: int, stop: Optional[int], links: bool = True
) -> tuple[list[Entry], int]:
    """One bounded stretch of the chain, plus the offset the walk stopped at."""
    cursor = Cursor(start)
    entries = list(
        walk(fp, info=info, links=links, start=start, stop=stop, cursor=cursor, publish=False)
    )
    return entries, cursor.offset


def _speculative_segment(
    fp, info: RarInfo, start: int, stop: Optional[int], links: bool
) -> tuple[list[Entry], int]:
    """A walk from a *guessed* boundary, where failing to parse is an answer.

    A boundary that landed inside a member's data will make the parser reject what it
    finds there, and that must not end the listing — it is the expected outcome of a
    guess being wrong, and the stretch is about to be re-walked from a known-good offset.
    Anything raised here is therefore recorded as "unusable" rather than propagated;
    segment 0 is walked non-speculatively, so a genuinely corrupt archive still reports
    itself exactly as it always did.
    """
    try:
        return walk_segment(fp, info, start, stop, links)
    except Exception as exc:  # noqa: BLE001 - a wrong guess can fail in any parser
        log.debug("segment at %d did not parse (%s); it will be re-walked", start, exc)
        return [], -1


def walk_parallel(
    fp,
    info: Optional[RarInfo] = None,
    limit: Optional[int] = None,
    links: bool = True,
    segments: int = 4,
) -> Iterator[Entry]:
    """Walk the header chain with several walkers at once.

    The chain is serial, so one walker can only ever go one round trip at a time. Several
    walkers, each entering at a header that verified on its own, cover the archive in
    roughly a K'th of the round trips — and over Tor round trips are the entire cost.

    Correctness is anchored rather than assumed. Walker 0 starts where the main header
    says the chain starts, so it is right by construction. Every later walker is trusted
    only once the walker before it has *arrived* at the offset it started from; a boundary
    that turns out to have been inside a member's data is detected exactly there, and that
    stretch is re-walked serially. A wrong guess costs time, never entries.
    """
    info = info or read_info(fp)
    size = getattr(fp, "size", 0) or 0
    if segments < 2 or size < segment.MIN_SEGMENTED_SIZE:
        yield from walk(fp, info=info, limit=limit, links=links)
        return

    kind = segment.RAR5 if info.version == 5 else segment.RAR4
    starts = segment.locate(fp, kind, info.body_offset, size, segments)
    if len(starts) < 2:
        log.debug("no usable segment boundaries; walking serially")
        yield from walk(fp, info=info, limit=limit, links=links)
        return

    bounds = [(starts[i], starts[i + 1] if i + 1 < len(starts) else None)
              for i in range(len(starts))]
    yielded = 0
    reporter = progress.get()

    with ThreadPoolExecutor(max_workers=len(bounds), thread_name_prefix="rv-walk") as pool:
        futures = [
            pool.submit(walk_segment if i == 0 else _speculative_segment,
                        fp, info, start, stop, links)
            for i, (start, stop) in enumerate(bounds)
        ]
        try:
            frontier = starts[0]
            for i, fut in enumerate(futures):
                entries, exit_at = fut.result()
                if i > 0:
                    if frontier < starts[i]:
                        # The chain ended before this segment — an end block, or a block
                        # type the walker stopped cleanly on. Everything past here is not
                        # part of the archive.
                        log.debug("chain ended at %d, before segment %d", frontier, i)
                        break
                    if frontier != starts[i] or exit_at < 0:
                        log.warning(
                            "segment %d started at %d but the chain arrives at %d; "
                            "that boundary was not a real header, re-walking serially",
                            i,
                            starts[i],
                            frontier,
                        )
                        entries, exit_at = walk_segment(fp, info, frontier, bounds[i][1], links)
                for entry in entries:
                    yield entry
                    yielded += 1
                    if limit and yielded >= limit:
                        return
                frontier = exit_at
                reporter.position(frontier, size)
        finally:
            for fut in futures:
                fut.cancel()


def find_member(
    fp, path: str, info: Optional[RarInfo] = None
) -> tuple[Optional[Entry], list[Entry]]:
    """The named member, plus the solid run whose dictionary it depends on.

    For a non-solid member the run is just the member. For a solid one it reaches back to
    the last member that started a fresh dictionary, because that is exactly the set of
    packed bytes a decompressor needs to reach this one.
    """
    info = info or read_info(fp)
    run: list[Entry] = []
    for entry in walk(fp, info=info):
        if entry.kind == DIR:
            if entry.path == path:
                return entry, [entry]
            continue
        loc = entry.locator or {}
        if not loc.get("solid"):
            run = []
        run.append(entry)
        if entry.path == path:
            return entry, list(run)
    return None, []


# ------------------------------------------------------------------- extraction


def helper_argv() -> Optional[tuple[str, ...]]:
    """The first installed RAR decompressor, or None. Split out so tests can stub it."""
    for helper in _HELPERS:
        if shutil.which(helper[0]):
            return helper
    return None


def _check_member_path(name: str) -> None:
    """A remote archive is untrusted input and the helper writes real paths."""
    parts = name.replace("\\", "/").split("/")
    if name.startswith("/") or ".." in parts or (len(name) > 1 and name[1] == ":"):
        raise RarError(f"refusing to extract {name!r}: the path escapes the archive")


def _end_block4() -> bytes:
    body = struct.pack("<BHH", BLOCK_END_V4, 0, 7)
    return struct.pack("<H", zlib.crc32(body) & 0xFFFF) + body


def _block5_bytes(htype: int, flags: int, fields: bytes) -> bytes:
    head = _vint_bytes(htype) + _vint_bytes(flags) + fields
    size = _vint_bytes(len(head))
    return struct.pack("<I", zlib.crc32(size + head)) + size + head


def synthesize(fp, info: RarInfo, run: list[Entry]) -> bytes:
    """Build a small archive holding just ``run``, for handing to an external helper.

    Every file header is copied byte for byte out of the source archive, so nothing has
    to be re-CRCed and no mistake here can corrupt a name, a size or a method. Only the
    main and end headers are synthesized, and only so the volume and solid archive flags
    can be cleared: copying a volume's main header would send the helper looking for the
    next volume, which is a different URL we deliberately do not guess.
    """
    for entry in run:
        _check_member_path(entry.path)

    if info.version == 5:
        out = bytearray(SIGNATURE_V5)
        out += _block5_bytes(BLOCK_MAIN_V5, 0, _vint_bytes(0))
    else:
        out = bytearray(SIGNATURE_V4)
        out += _block4_bytes(BLOCK_MAIN_V4, 0, struct.pack("<HI", 0, 0))

    for entry in run:
        loc = entry.locator or {}
        offset, length = loc["hdr"]
        # The header is immediately followed by its data, so this is one range request.
        out += _pread(fp, offset, length + loc["csize"])

    if info.version == 5:
        out += _block5_bytes(BLOCK_END_V5, 0x0004, _vint_bytes(0))
    else:
        out += _end_block4()
    return bytes(out)


def _block4_bytes(htype: int, flags: int, tail: bytes) -> bytes:
    body = struct.pack("<BHH", htype, flags, 7 + len(tail)) + tail
    return struct.pack("<H", zlib.crc32(body) & 0xFFFF) + body


def run_helper(blob: bytes, entry: Entry) -> bytes:
    """Decompress one member by handing a synthetic archive to ``unrar`` or ``7z``.

    The whole synthetic archive is extracted rather than naming the member on the command
    line: RAR treats a member name as a wildcard mask, and the archive holds only what we
    put in it anyway. The result is then checked against the size and CRC the real header
    promised, so a helper that writes anything unexpected to disk fails loudly instead of
    handing back quietly wrong bytes.
    """
    helper = helper_argv()
    if helper is None:
        raise HelperUnavailable(
            f"{entry.path!r} is compressed, and decompressing RAR needs the unrar binary "
            f"(or 7z built with its RAR plugin) — there is no pure-Python RAR decoder. "
            f"Listing, and extracting stored members, work without it."
        )

    loc = entry.locator or {}
    with tempfile.TemporaryDirectory(prefix="rvtree-rar-") as tmp:
        archive_path = os.path.join(tmp, "member.rar")
        out_dir = os.path.join(tmp, "out")
        os.mkdir(out_dir)
        with open(archive_path, "wb") as fh:
            fh.write(blob)

        if helper[0] == "unrar":
            argv = list(helper) + [archive_path, out_dir + os.sep]
        else:
            argv = list(helper) + [f"-o{out_dir}", archive_path]
        proc = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True)

        target = os.path.join(out_dir, entry.path)
        if not os.path.isfile(target):
            tail = (proc.stderr or proc.stdout or b"")[-200:].decode("utf-8", "replace").strip()
            raise HelperFailed(
                f"{helper[0]} exited {proc.returncode} without producing {entry.path!r}"
                + (f": {tail}" if tail else "")
            )
        with open(target, "rb") as fh:
            data = fh.read()

    _verify(data, loc, entry.path, helper[0])
    return data


def _verify(data: bytes, loc: dict, path: str, source: str) -> None:
    expected = loc.get("size")
    if expected and len(data) != expected:
        raise HelperFailed(
            f"{source} produced {len(data)} bytes for {path!r}, not the {expected} its "
            f"header promises"
        )
    crc = loc.get("crc")
    if crc is not None and zlib.crc32(data) != crc:
        raise HelperFailed(f"{source} produced {path!r} with the wrong CRC32")


def extract_member(
    fp,
    entry: Entry,
    info: RarInfo,
    run: Optional[list[Entry]] = None,
    force_helper: bool = False,
) -> bytes:
    """Fetch one member's bytes, touching as little of the archive as possible.

    ``force_helper`` sends a stored member through the synthesis path anyway. That has no
    use in production and every use in testing: it exercises the exact byte layout handed
    to ``unrar`` against an archive whose correct contents are already known.
    """
    loc = entry.locator or {}
    if loc.get("split"):
        raise MultiVolumeArchive(
            f"{entry.path!r} is split across volumes: this file holds only part of it, and "
            f"rvtree will not guess the URLs of the others. Point it at each volume in turn."
        )
    if loc.get("crypt"):
        raise EncryptedArchive(f"{entry.path!r} is encrypted and needs a password")

    if loc.get("method") == 0 and not force_helper:
        data = _pread(fp, loc["offset"], loc["csize"])
        _verify(data, loc, entry.path, "the archive")
        return data

    return run_helper(synthesize(fp, info, run or [entry]), entry)
