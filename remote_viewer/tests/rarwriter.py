"""Build genuine stored-mode RAR archives in pure Python.

There is no freely installable ``rar`` binary to generate fixtures with — ``unrar`` and
``7z`` both only ever read RAR — so the corpus for the RAR tests is written here instead.
Everything is stored (method 0), which is the one RAR method needing no compressor.

These are real archives rather than stand-ins: ``unrar l`` and ``unrar x`` accept them,
and ``tests/test_listing.py`` asserts that agreement instead of assuming it. That matters
because ``rvtree/formats/rar.py`` is hand-rolled, and a fixture written by the same
understanding that reads it would only ever confirm its own mistakes.

Run directly to (re)build the fixtures::

    python3 tests/rarwriter.py tests/fixtures
"""

from __future__ import annotations

import dataclasses
import os
import struct
import sys
import time
import zlib
from typing import Iterator, Optional

SIG4 = b"Rar!\x1a\x07\x00"
SIG5 = b"Rar!\x1a\x07\x01\x00"

FILE, DIR, LINK = "file", "dir", "link"


@dataclasses.dataclass
class Item:
    """One member to write. ``data`` is the content, or the target for a link."""

    name: str
    kind: str = FILE
    data: bytes = b""
    mode: int = 0
    mtime: float = 0.0
    solid: bool = False
    split_after: bool = False

    def __post_init__(self) -> None:
        if not self.mode:
            self.mode = {DIR: 0o040755, LINK: 0o120777}.get(self.kind, 0o100644)
        if not self.mtime:
            self.mtime = time.time()


def from_tree(base: str, arcbase: Optional[str] = None) -> list[Item]:
    """Read a directory into items, directories ahead of their contents."""
    arcbase = arcbase or os.path.basename(base.rstrip("/"))
    st = os.lstat(base)
    items = [Item(arcbase, DIR, mode=st.st_mode, mtime=st.st_mtime)]
    for name in sorted(os.listdir(base)):
        path = os.path.join(base, name)
        arc = f"{arcbase}/{name}"
        st = os.lstat(path)
        if os.path.islink(path):
            target = os.readlink(path).encode()
            items.append(Item(arc, LINK, target, st.st_mode, st.st_mtime))
        elif os.path.isdir(path):
            items.extend(from_tree(path, arc))
        else:
            with open(path, "rb") as fh:
                items.append(Item(arc, FILE, fh.read(), st.st_mode, st.st_mtime))
    return items


# ------------------------------------------------------------------------ RAR 5.0


def vint(n: int) -> bytes:
    """RAR5's variable-length integer: 7 bits per byte, high bit continues."""
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        out.append(byte | 0x80 if n else byte)
        if not n:
            return bytes(out)


def _block5(htype: int, flags: int, fields: bytes, extra: bytes = b"", data_size: int = 0) -> bytes:
    """Assemble one RAR5 block.

    The CRC covers the size field and the header, so it must be computed after the
    header is complete — including the extra area, which counts towards Header size.
    """
    head = vint(htype) + vint(flags)
    if flags & 0x0001:
        head += vint(len(extra))
    if flags & 0x0002:
        head += vint(data_size)
    head += fields + extra
    size = vint(len(head))
    return struct.pack("<I", zlib.crc32(size + head)) + size + head


def _file5(item: Item) -> bytes:
    data = b"" if item.kind == DIR else item.data
    is_link = item.kind == LINK
    payload = b"" if is_link else data  # a link's target lives in an extra record

    file_flags = 0x0002  # mtime present
    if item.kind == DIR:
        file_flags |= 0x0001
    if payload:
        file_flags |= 0x0004  # data CRC32 present

    # Compression info: bits 0-5 algorithm version (0 is RAR 5.0), bit 6 solid,
    # bits 7-9 method (0 is store), bits 10-13 dictionary size.
    comp_info = 0x0040 if item.solid else 0

    fields = vint(file_flags) + vint(0 if is_link else len(payload)) + vint(item.mode)
    fields += struct.pack("<I", int(item.mtime))
    if payload:
        fields += struct.pack("<I", zlib.crc32(payload))
    fields += vint(comp_info)
    fields += vint(1)  # host OS: Unix, so Attributes is a st_mode
    name = item.name.encode("utf-8")
    fields += vint(len(name)) + name

    extra = b""
    header_flags = 0x0002 if payload else 0
    if is_link:
        # FHEXTRA_REDIR: type 5, redirection type 1 (Unix symlink).
        record = vint(5) + vint(1) + vint(0) + vint(len(data)) + data
        extra = vint(len(record)) + record
        header_flags |= 0x0001
    if item.split_after:
        header_flags |= 0x0010

    return _block5(2, header_flags, fields, extra, len(payload)) + payload


def write_rar5(dest: str, items: list[Item], volume: bool = False) -> str:
    solid = any(i.solid for i in items)
    with open(dest, "wb") as fh:
        fh.write(SIG5)
        arc_flags = (0x0004 if solid else 0) | (0x0001 if volume else 0)
        fh.write(_block5(1, 0, vint(arc_flags)))
        for item in items:
            fh.write(_file5(item))
        fh.write(_block5(5, 0x0004, vint(0)))
    return dest


# ------------------------------------------------------------------------ RAR 4.x


def _block4(htype: int, flags: int, tail: bytes) -> bytes:
    """Assemble one RAR4 block. HEAD_SIZE spans the base header and the tail."""
    body = struct.pack("<BHH", htype, flags, 7 + len(tail)) + tail
    return struct.pack("<H", zlib.crc32(body) & 0xFFFF) + body


def _dos_time(unix: float) -> int:
    t = time.localtime(unix)
    return (
        ((max(t.tm_year, 1980) - 1980) << 25)
        | (t.tm_mon << 21)
        | (t.tm_mday << 16)
        | (t.tm_hour << 11)
        | (t.tm_min << 5)
        | (t.tm_sec // 2)
    )


def encode_name4(name: str) -> tuple[bytes, bool]:
    """Return the FILE_NAME field and whether LHD_UNICODE must be set.

    Non-ASCII names use RAR's 2-bit-opcode scheme. Only opcode 2 (a raw little-endian
    UTF-16 unit) is emitted: it is always valid, and it saves implementing the
    compression side of a scheme rvtree only ever needs to decode.
    """
    try:
        return name.encode("ascii"), False
    except UnicodeEncodeError:
        pass

    chars = [ord(c) for c in name]
    if any(c > 0xFFFF for c in chars):
        raise ValueError(f"{name!r} is outside the BMP; RAR4 names are UTF-16 units")

    encoded = bytearray([0])  # high byte, unused while every opcode is 2
    for i in range(0, len(chars), 4):
        group = chars[i : i + 4]
        encoded.append(sum(2 << (6 - 2 * j) for j in range(len(group))))
        for char in group:
            encoded += struct.pack("<H", char)
    return name.encode("ascii", "replace") + b"\0" + bytes(encoded), True


def _file4(item: Item) -> bytes:
    # A RAR4 symlink stores its target as the member's data, unlike RAR5.
    data = b"" if item.kind == DIR else item.data
    name, unicode_name = encode_name4(item.name)

    flags = 0x8000  # LONG_BLOCK: PACK_SIZE doubles as the block's ADD_SIZE
    if unicode_name:
        flags |= 0x0200
    if item.kind == DIR:
        flags |= 0x00E0  # the window-size bits all set means "directory"
    if item.solid:
        flags |= 0x0010
    if item.split_after:
        flags |= 0x0002

    tail = struct.pack(
        "<IIBIIBBHI",
        len(data),          # PACK_SIZE, doubling as ADD_SIZE
        len(data),          # UNP_SIZE
        3,                  # HOST_OS: Unix, so ATTR is a st_mode
        zlib.crc32(data),
        _dos_time(item.mtime),
        20,                 # UNP_VER
        0x30,               # METHOD: store
        len(name),
        item.mode,
    )
    return _block4(0x74, flags, tail + name) + data


def write_rar4(dest: str, items: list[Item], volume: bool = False) -> str:
    solid = any(i.solid for i in items)
    with open(dest, "wb") as fh:
        fh.write(SIG4)
        main_flags = (0x0008 if solid else 0) | (0x0001 if volume else 0)
        fh.write(_block4(0x73, main_flags, struct.pack("<HI", 0, 0)))
        for item in items:
            fh.write(_file4(item))
        fh.write(_block4(0x7B, 0, b""))
    return dest


# ------------------------------------------------------------------------ the corpus


def _small() -> list[Item]:
    """A handful of tiny members, for the fixtures that only exercise a refusal."""
    return [
        Item("small", DIR),
        Item("small/a.txt", FILE, b"alpha\n"),
        Item("small/b.txt", FILE, b"bravo\n"),
    ]


def build(dest: str) -> Iterator[str]:
    payload = os.path.join(dest, "payload")
    items = from_tree(payload)

    yield write_rar5(os.path.join(dest, "test.rar5.rar"), items)
    yield write_rar4(os.path.join(dest, "test.rar4.rar"), items)

    # Solid: every file after the first shares the dictionary of the ones before it.
    solid, seen = [], False
    for item in items:
        solid.append(dataclasses.replace(item, solid=item.kind == FILE and seen))
        seen = seen or item.kind == FILE
    yield write_rar5(os.path.join(dest, "test.solid.rar5.rar"), solid)

    # Multi-volume: the last member is flagged as continuing into the next volume.
    spanning = _small()
    spanning[-1] = dataclasses.replace(spanning[-1], split_after=True)
    yield write_rar5(os.path.join(dest, "test.multivol.rar5.rar"), spanning, volume=True)
    yield write_rar4(os.path.join(dest, "test.multivol.rar4.rar"), spanning, volume=True)

    # A non-ASCII name, so the RAR4 unicode decoder is exercised against real bytes.
    yield write_rar4(
        os.path.join(dest, "test.unicode.rar4.rar"),
        [Item("ünïcode/naïve — ★.txt", FILE, b"unicode\n")],
    )


def main(argv: list[str]) -> int:
    dest = argv[1] if len(argv) > 1 else os.path.join(os.path.dirname(__file__), "fixtures")
    for path in build(dest):
        print(f"{os.path.basename(path)}  {os.path.getsize(path):,} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
