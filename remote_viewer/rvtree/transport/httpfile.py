"""A seekable file object backed by HTTP Range requests.

This is the substrate the whole tool stands on: hand one of these to stdlib
``zipfile`` or ``tarfile`` and they will parse a remote archive while touching only
the bytes they actually need.
"""

from __future__ import annotations

import io
import logging
from typing import Optional

from ..util import human_bytes
from .tor import Transport

log = logging.getLogger(__name__)

INITIAL_READAHEAD = 128 * 1024
MIN_READAHEAD = 16 * 1024
MAX_READAHEAD = 4 * 1024 * 1024

# How large a readahead may grow when the transport can fetch a range on several lanes at
# once. A fill is worth widening exactly to the point where every lane carries one
# split-sized sub-range; past that you are buying more rounds, not more parallelism. The
# absolute ceiling stops a very wide pool from turning a speculative read into a download.
SPLIT_HINT = 1024 * 1024
MAX_READAHEAD_CEILING = 32 * 1024 * 1024


class HttpRangeFile(io.RawIOBase):
    """``io.RawIOBase`` over a remote entity, using absolute byte ranges only.

    The total size is learned up front precisely so that ``seek(-n, SEEK_END)`` can be
    resolved locally into an absolute range. That matters more than it looks:
    ``zipfile._EndRecData`` seeks from the end to find the Central Directory, and a
    literal ``Range: bytes=-n`` is rejected outright (HTTP 501) by some CDN tiers.
    """

    def __init__(
        self,
        transport: Transport,
        url: str,
        size: Optional[int] = None,
        headers: Optional[dict[str, str]] = None,
        readahead: int = INITIAL_READAHEAD,
    ):
        super().__init__()
        self.transport = transport
        self.url = url
        if size is None:
            size, headers = transport.head_like(url)
        self.size = size
        self.headers = headers or {}
        self._pos = 0
        self._buf = b""
        self._buf_start = 0
        self._readahead = readahead
        self._served_from_buf = 0
        # Asked for rather than tested for, so this stays ignorant of which transport it
        # holds: one that cannot split has no ``lanes`` and keeps the single-lane ceiling.
        lanes = getattr(transport, "lanes", 1)
        split = getattr(transport, "min_split", SPLIT_HINT)
        self._max_readahead = min(max(MAX_READAHEAD, split * lanes), MAX_READAHEAD_CEILING)

    # -- io.RawIOBase contract -------------------------------------------------
    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

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
        if n == 0:
            return b""

        if not self._covers(self._pos, n):
            self._fill(self._pos, n)

        off = self._pos - self._buf_start
        data = self._buf[off : off + n]
        self._served_from_buf += len(data)
        self._pos += len(data)
        return data

    def readinto(self, b) -> int:  # type: ignore[override]
        data = self.read(len(b))
        b[: len(data)] = data
        return len(data)

    # -- buffering -------------------------------------------------------------
    def _covers(self, pos: int, n: int) -> bool:
        return self._buf_start <= pos and pos + n <= self._buf_start + len(self._buf)

    def _fill(self, pos: int, n: int) -> None:
        """Fetch at least ``n`` bytes at ``pos``, plus readahead.

        Readahead is tuned by how much of the *previous* buffer was actually consumed,
        which is what separates the two access patterns we care about. Walking a tar of
        large members reads 512 bytes and then skips megabytes, so readahead is pure
        waste and collapses toward the floor. Walking a dense tar, or reading a member's
        contents, consumes the buffer end to end and readahead grows to amortise the
        round trip — and over Tor a round trip costs about as much as 500 KiB of data.
        """
        contiguous = bool(self._buf) and pos == self._buf_start + len(self._buf)
        if self._buf:
            used = self._served_from_buf / len(self._buf)
            before = self._readahead
            if used < 0.25:
                # Small reads far apart: the extra bytes are being thrown away.
                self._readahead = max(self._readahead // 4, MIN_READAHEAD)
            elif contiguous:
                self._readahead = min(self._readahead * 2, self._max_readahead)
            else:
                # A seek says nothing about what follows, and a buffer cut short by
                # end-of-file looks fully used without being evidence of streaming.
                # Fall back to the neutral default rather than carrying a big window.
                self._readahead = INITIAL_READAHEAD
            if self._readahead != before:
                log.debug(
                    "readahead %s -> %s (%.0f%% of the last buffer was used)",
                    human_bytes(before),
                    human_bytes(self._readahead),
                    used * 100,
                )
        self._served_from_buf = 0

        span = max(n, self._readahead)
        end = min(pos + span, self.size) - 1
        self._buf = self.transport.get_range(self.url, pos, end)
        self._buf_start = pos

    # -- convenience -----------------------------------------------------------
    def pread(self, offset: int, length: int) -> bytes:
        """Positional read that bypasses the buffer, for one-shot metadata fetches."""
        if length <= 0:
            return b""
        end = min(offset + length, self.size) - 1
        return self.transport.get_range(self.url, offset, end)

    def read_tail(self, length: int) -> bytes:
        """Read the final ``length`` bytes using an absolute (never suffix) range."""
        start = max(0, self.size - length)
        return self.pread(start, self.size - start)
