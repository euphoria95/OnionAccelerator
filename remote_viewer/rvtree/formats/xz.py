"""XZ block index parsing and block-level random access.

The XZ container ends with an Index listing every block's compressed and uncompressed
extent. Reading it costs two small range requests and turns a multi-block ``.xz`` into
a randomly-accessible stream: for the real ``linux-6.6.tar.xz`` a 12-byte footer plus a
444-byte index reveals all 57 blocks of 24 MiB each.

A stream written by single-threaded ``xz`` has exactly one block and no random access
at all; that case is detected here and reported rather than silently attempted.
"""

from __future__ import annotations

import binascii
import dataclasses
import io
import logging
import lzma
import struct
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Iterator, Optional

from .. import progress
from ..util import human_bytes

log = logging.getLogger(__name__)

XZ_MAGIC = b"\xfd7zXZ\x00"
FOOTER_MAGIC = b"YZ"
STREAM_HEADER_SIZE = 12
STREAM_FOOTER_SIZE = 12

# Check size in bytes, indexed by the check-type id in the stream flags.
CHECK_SIZES = [0, 4, 4, 4, 8, 8, 8, 16, 16, 16, 32, 32, 32, 64, 64, 64]

# xz filter id -> python lzma filter constant. Members absent from a given Python
# build (ARM64/RISC-V arrived later) are simply skipped.
_FILTER_IDS = {
    0x21: "FILTER_LZMA2",
    0x03: "FILTER_DELTA",
    0x04: "FILTER_X86",
    0x05: "FILTER_POWERPC",
    0x06: "FILTER_IA64",
    0x07: "FILTER_ARM",
    0x08: "FILTER_ARMTHUMB",
    0x09: "FILTER_SPARC",
    0x0A: "FILTER_ARM64",
    0x0B: "FILTER_RISCV",
}
_BCJ_IDS = {0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0A, 0x0B}


class XzError(RuntimeError):
    pass


class NotSeekable(XzError):
    """The stream is a single block, so no part of it can be reached without the rest."""


def read_uvarint(buf: bytes, pos: int) -> tuple[int, int]:
    """Decode a multibyte integer as defined by the .xz spec (7 bits per byte, LE)."""
    value = 0
    for i in range(9):
        if pos >= len(buf):
            raise XzError("truncated varint")
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << (7 * i)
        if not byte & 0x80:
            return value, pos
    raise XzError("overlong varint")


@dataclasses.dataclass
class Block:
    index: int
    comp_offset: int  # absolute offset of the block header in the file
    unpadded_size: int  # block header + compressed data + check, without padding
    uncomp_offset: int  # offset of this block's output in the decoded stream
    uncomp_size: int
    check_size: int

    @property
    def comp_end(self) -> int:
        return self.comp_offset + self.unpadded_size

    def contains(self, offset: int) -> bool:
        return self.uncomp_offset <= offset < self.uncomp_offset + self.uncomp_size


class XzIndex:
    """The block map of an ``.xz`` file, read from its trailing Index record(s)."""

    def __init__(self, blocks: list[Block], streams: int, check_type: int):
        self.blocks = blocks
        self.streams = streams
        self.check_type = check_type

    @property
    def uncompressed_size(self) -> int:
        return sum(b.uncomp_size for b in self.blocks)

    @property
    def compressed_size(self) -> int:
        return sum(_pad4(b.unpadded_size) for b in self.blocks)

    @property
    def seekable(self) -> bool:
        return len(self.blocks) > 1

    def find(self, offset: int) -> Optional[Block]:
        lo, hi = 0, len(self.blocks) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            b = self.blocks[mid]
            if offset < b.uncomp_offset:
                hi = mid - 1
            elif offset >= b.uncomp_offset + b.uncomp_size:
                lo = mid + 1
            else:
                return b
        return None


def _pad4(n: int) -> int:
    return (n + 3) & ~3


def read_index(fp) -> XzIndex:
    """Walk the file backwards collecting every stream's Index.

    Concatenated ``.xz`` streams are legal and common (``cat a.xz b.xz > c.xz``), so
    this keeps stepping back over stream padding until it reaches the start of file.
    """
    size = fp.size if hasattr(fp, "size") else _seek_size(fp)
    head = _pread(fp, 0, 6)
    if head != XZ_MAGIC:
        raise XzError("not an xz stream")

    blocks: list[Block] = []
    streams = 0
    check_type = 0
    end = size

    while end > 0:
        # Stream padding is zero bytes in multiples of four, between streams.
        while end >= 4:
            tail = _pread(fp, end - 4, 4)
            if tail == b"\x00\x00\x00\x00":
                end -= 4
            else:
                break
        if end <= 0:
            break

        footer = _pread(fp, end - STREAM_FOOTER_SIZE, STREAM_FOOTER_SIZE)
        if footer[10:12] != FOOTER_MAGIC:
            raise XzError("xz stream footer not found (truncated or not an xz file?)")
        stored_crc, backward = struct.unpack("<II", footer[0:8])
        flags = footer[8:10]
        if binascii.crc32(footer[4:10]) != stored_crc:
            raise XzError("corrupt xz stream footer (CRC mismatch)")
        check_type = flags[1] & 0x0F
        check_size = CHECK_SIZES[check_type]

        index_size = (backward + 1) * 4
        index_start = end - STREAM_FOOTER_SIZE - index_size
        if index_start < 0:
            raise XzError("xz index extends before start of file")
        raw = _pread(fp, index_start, index_size)
        records = _parse_index_record(raw)

        # The stream is: header | blocks | index | footer.
        payload = sum(_pad4(u) for u, _ in records)
        stream_start = index_start - payload - STREAM_HEADER_SIZE
        if stream_start < 0:
            raise XzError("xz stream extends before start of file")

        comp = stream_start + STREAM_HEADER_SIZE
        uncomp = 0
        local: list[Block] = []
        for unpadded, uncomp_size in records:
            local.append(
                Block(
                    index=0,
                    comp_offset=comp,
                    unpadded_size=unpadded,
                    uncomp_offset=uncomp,
                    uncomp_size=uncomp_size,
                    check_size=check_size,
                )
            )
            comp += _pad4(unpadded)
            uncomp += uncomp_size

        blocks = local + blocks
        streams += 1
        end = stream_start

    # Renumber and re-base uncompressed offsets across the concatenated streams.
    running = 0
    for i, b in enumerate(blocks):
        b.index = i
        b.uncomp_offset = running
        running += b.uncomp_size
    index = XzIndex(blocks, streams, check_type)
    log.info(
        "xz index: %d block(s) in %d stream(s), %s uncompressed, random access %s",
        len(blocks),
        streams,
        human_bytes(index.uncompressed_size),
        "yes" if index.seekable else "NO (single block)",
    )
    return index


def _parse_index_record(raw: bytes) -> list[tuple[int, int]]:
    if not raw or raw[0] != 0x00:
        raise XzError("xz index indicator missing")
    stored = struct.unpack("<I", raw[-4:])[0]
    if binascii.crc32(raw[:-4]) != stored:
        raise XzError("corrupt xz index (CRC mismatch)")
    pos = 1
    count, pos = read_uvarint(raw, pos)
    records: list[tuple[int, int]] = []
    for _ in range(count):
        unpadded, pos = read_uvarint(raw, pos)
        uncomp, pos = read_uvarint(raw, pos)
        records.append((unpadded, uncomp))
    return records


def parse_block_header(raw: bytes) -> tuple[int, list[dict]]:
    """Return ``(header_size, lzma filter chain)`` for a block.

    Filters are returned in the order the block header lists them, which is the order
    ``lzma`` expects for ``FORMAT_RAW`` decoding: the compression filter comes last.
    """
    if not raw or raw[0] == 0x00:
        raise XzError("expected a block header, found an index indicator")
    header_size = (raw[0] + 1) * 4
    header = raw[:header_size]
    if len(header) < header_size:
        raise XzError("truncated block header")
    stored = struct.unpack("<I", header[-4:])[0]
    if binascii.crc32(header[:-4]) != stored:
        raise XzError("corrupt block header (CRC mismatch)")

    flags = header[1]
    n_filters = (flags & 0x03) + 1
    pos = 2
    if flags & 0x40:  # compressed size present
        _, pos = read_uvarint(header, pos)
    if flags & 0x80:  # uncompressed size present
        _, pos = read_uvarint(header, pos)

    filters: list[dict] = []
    for _ in range(n_filters):
        fid, pos = read_uvarint(header, pos)
        prop_len, pos = read_uvarint(header, pos)
        props = header[pos : pos + prop_len]
        pos += prop_len
        filters.append(_filter_spec(fid, props))
    return header_size, filters


def _filter_spec(fid: int, props: bytes) -> dict:
    name = _FILTER_IDS.get(fid)
    if name is None or not hasattr(lzma, name):
        raise XzError(f"unsupported xz filter id 0x{fid:02x}")
    const = getattr(lzma, name)
    if fid == 0x21:
        if len(props) != 1:
            raise XzError("bad LZMA2 properties")
        p = props[0]
        return {"id": const, "dict_size": (2 | (p & 1)) << ((p >> 1) + 11)}
    if fid == 0x03:
        if len(props) != 1:
            raise XzError("bad delta properties")
        return {"id": const, "dist": props[0] + 1}
    if fid in _BCJ_IDS:
        spec: dict = {"id": const}
        if len(props) == 4:
            spec["start_offset"] = struct.unpack("<I", props)[0]
        return spec
    raise XzError(f"unhandled xz filter id 0x{fid:02x}")


def decode_block_prefix(raw_prefix: bytes, max_length: int) -> bytes:
    """Decode the leading ``max_length`` plaintext bytes from a truncated block.

    Used to identify what a stream wraps without pulling the whole block down. That
    distinction is the difference between a 64 KiB probe and a 100 GB one when the
    archive happens to be a single-block ``.xz``.
    """
    header_size, filters = parse_block_header(raw_prefix)
    dec = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=filters)
    return dec.decompress(raw_prefix[header_size:], max_length)


def decode_block(raw: bytes, block: Block, max_length: int = -1) -> bytes:
    """Decode one block from its raw bytes (header + payload + check).

    Uses ``FORMAT_RAW`` with the block's own filter chain. An earlier approach that
    rewrapped each block into a synthetic single-block stream for ``lzma.decompress``
    failed on 25 of 31 blocks; this one decodes every block byte-identically.
    """
    header_size, filters = parse_block_header(raw)
    payload = raw[header_size : block.unpadded_size - block.check_size]
    dec = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=filters)
    out = dec.decompress(payload, max_length if max_length > 0 else -1)
    return out


class XzBlockFile(io.RawIOBase):
    """Seekable, read-only view of the *decompressed* contents of an ``.xz`` file.

    Only the blocks actually touched are fetched. Decoded plaintext is kept in a small
    LRU so that a forward walk (which is what listing a tar is) stays cheap without
    holding a 100 GB stream in memory.
    """

    def __init__(
        self,
        source,
        index: Optional[XzIndex] = None,
        cache_blocks: int = 3,
        prefetch: int = 0,
        executor: Optional[ThreadPoolExecutor] = None,
        owns_executor: bool = False,
        raw_cache: Optional[dict[int, bytes]] = None,
    ):
        super().__init__()
        self.source = source
        self.index = index or read_index(source)
        # Raw compressed blocks already pulled down elsewhere, e.g. by format sniffing,
        # so that identifying the archive does not cost a second fetch of block 0.
        self._raw_cache = raw_cache if raw_cache is not None else {}
        self.size = self.index.uncompressed_size
        self._pos = 0
        self._cache: dict[int, bytes] = {}
        self._order: list[int] = []
        self._cache_blocks = max(1, cache_blocks)
        self._prefetch = prefetch
        self._executor = executor
        self._owns_executor = owns_executor
        self._pending: dict[int, object] = {}
        self.blocks_decoded = 0

    # -- io contract -----------------------------------------------------------
    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            new = offset
        elif whence == io.SEEK_CUR:
            new = self._pos + offset
        elif whence == io.SEEK_END:
            new = self.size + offset
        else:
            raise ValueError(f"invalid whence {whence!r}")
        self._pos = max(0, new)
        return self._pos

    def read(self, n: int = -1) -> bytes:
        if self._pos >= self.size:
            return b""
        if n is None or n < 0:
            n = self.size - self._pos
        n = min(n, self.size - self._pos)
        chunks: list[bytes] = []
        remaining = n
        while remaining > 0:
            block = self.index.find(self._pos)
            if block is None:
                break
            plain = self._plaintext(block)
            off = self._pos - block.uncomp_offset
            take = plain[off : off + remaining]
            if not take:
                break
            chunks.append(take)
            self._pos += len(take)
            remaining -= len(take)
        return b"".join(chunks)

    def readinto(self, b) -> int:  # type: ignore[override]
        data = self.read(len(b))
        b[: len(data)] = data
        return len(data)

    # -- block access ----------------------------------------------------------
    def raw_block(self, block: Block) -> bytes:
        cached = self._raw_cache.get(block.index)
        if cached is not None:
            return cached
        return self.source.pread(block.comp_offset, block.unpadded_size)

    def _plaintext(self, block: Block) -> bytes:
        cached = self._cache.get(block.index)
        if cached is not None:
            self._touch(block.index)
            return cached

        prefetched = self._take_pending(block)
        started = time.monotonic()
        raw = prefetched if prefetched is not None else self.raw_block(block)
        fetched = time.monotonic()
        plain = decode_block(raw, block)
        self.blocks_decoded += 1
        progress.get().block(block.index, len(self.index.blocks))
        log.debug(
            "block %d/%d: %s %s in %.2fs, decoded to %s in %.2fs",
            block.index + 1,
            len(self.index.blocks),
            human_bytes(len(raw)),
            "prefetched" if prefetched is not None else "fetched",
            fetched - started,
            human_bytes(len(plain)),
            time.monotonic() - fetched,
        )
        self._store(block.index, plain)
        self._schedule_prefetch(block)
        return plain

    def _store(self, idx: int, plain: bytes) -> None:
        self._cache[idx] = plain
        self._touch(idx)
        while len(self._order) > self._cache_blocks:
            self._cache.pop(self._order.pop(0), None)

    def _touch(self, idx: int) -> None:
        if idx in self._order:
            self._order.remove(idx)
        self._order.append(idx)

    # -- prefetch --------------------------------------------------------------
    def _schedule_prefetch(self, block: Block) -> None:
        """Pull the next few blocks' compressed bytes in parallel.

        Network is the bottleneck, not decompression, so only the fetch is farmed out.
        This is aimed squarely at the dense case (a source tarball, where the walk
        crosses every block) which is exactly where the extra circuits pay off.
        """
        if not self._prefetch or self._executor is None:
            return
        queued = []
        for i in range(block.index + 1, min(block.index + 1 + self._prefetch, len(self.index.blocks))):
            if i in self._cache or i in self._pending:
                continue
            nxt = self.index.blocks[i]
            self._pending[i] = self._executor.submit(self.raw_block, nxt)
            queued.append(i)
        if queued:
            log.debug("prefetching block(s) %s", ", ".join(str(i + 1) for i in queued))

    def _take_pending(self, block: Block) -> Optional[bytes]:
        fut = self._pending.pop(block.index, None)
        if fut is None:
            return None
        try:
            return fut.result()  # type: ignore[attr-defined]
        except Exception:
            return None

    def release(self) -> None:
        """Drop the prefetch machinery while leaving the stream readable.

        Extraction walks the tar to find a member and then seeks back to read it, so the
        end of a walk cannot be the end of the file object. Clearing the executor first
        is what makes that safe: no later read can submit to a pool being shut down.
        """
        executor, self._executor = self._executor, None
        for fut in self._pending.values():
            try:
                fut.cancel()  # type: ignore[attr-defined]
            except Exception:
                pass
        self._pending.clear()
        if executor is not None and self._owns_executor:
            executor.shutdown(wait=False)

    def close(self) -> None:
        self.release()
        self._cache.clear()
        super().close()


def _pread(fp, offset: int, length: int) -> bytes:
    if hasattr(fp, "pread"):
        return fp.pread(offset, length)
    fp.seek(offset)
    return fp.read(length)


def _seek_size(fp) -> int:
    cur = fp.tell()
    fp.seek(0, io.SEEK_END)
    size = fp.tell()
    fp.seek(cur)
    return size
