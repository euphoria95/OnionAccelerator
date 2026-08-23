"""A local spool standing in for random access the server will not give.

rvtree's whole premise is that an archive's metadata lives somewhere a few Range
requests can reach. When a server refuses ranges there is no version of that premise
left: the only thing it will do is stream the entity from byte zero, once, and the
metadata of every format worth listing (7z, zip, xz, and the last header of any tar)
sits at the far end of that stream.

So this does the only thing that works. It takes the one stream on offer, writes it to
disk, and hands back a file object with the same surface as ``HttpRangeFile`` — after
which every walker in the tree reads a local file at local speed and none of them needs
to know anything changed. It is a download, and the caller is expected to have said out
loud that it wants one; ``archive.open_archive`` will not reach for this on its own.

Two properties of a rangeless server make the details matter more than they look:

* **There is no resume.** A transfer that dies at 90% starts again at zero. That is why
  ``--spool PATH`` exists: name the file and a second attempt costs nothing if the first
  one finished, and the bytes are still there afterwards if the archive was what you
  wanted all along.
* **Short is not an error the server reports.** A truncated body arrives looking exactly
  like a complete one, so the byte count is checked against Content-Length here rather
  than left to a parser to discover as a confusing "unexpected end of data".
"""

from __future__ import annotations

import io
import logging
import os
import tempfile
from typing import Callable, Optional

from ..util import human_bytes
from .tor import Transport

log = logging.getLogger(__name__)

WRITE_CHUNK = 256 * 1024


class SpoolError(RuntimeError):
    pass


class SpooledFile(io.RawIOBase):
    """A downloaded copy of a remote entity, shaped like ``HttpRangeFile``.

    Same attributes and same two convenience reads, so ``detect``, the format walkers
    and the extractors take it without a word of special-casing.
    """

    def __init__(
        self,
        fh: io.BufferedReader,
        url: str,
        size: int,
        headers: dict[str, str],
        transport: Transport,
        path: str,
        keep: bool,
    ):
        super().__init__()
        self._fh = fh
        self.url = url
        self.size = size
        self.headers = headers
        self.transport = transport
        self.path = path
        self.keep = keep

    # -- io.RawIOBase contract -------------------------------------------------
    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    def tell(self) -> int:
        return self._fh.tell()

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        return self._fh.seek(offset, whence)

    def read(self, n: int = -1) -> bytes:
        return self._fh.read(None if n is None or n < 0 else n)

    def readinto(self, b) -> int:  # type: ignore[override]
        return self._fh.readinto(b)

    def close(self) -> None:
        if self.closed:
            return
        try:
            self._fh.close()
        finally:
            super().close()
            if not self.keep:
                try:
                    os.unlink(self.path)
                except OSError:
                    log.debug("could not remove spool file %s", self.path)

    # -- convenience, matching HttpRangeFile ------------------------------------
    def pread(self, offset: int, length: int) -> bytes:
        if length <= 0:
            return b""
        here = self._fh.tell()
        try:
            self._fh.seek(offset)
            return self._fh.read(min(length, max(0, self.size - offset)))
        finally:
            self._fh.seek(here)

    def read_tail(self, length: int) -> bytes:
        start = max(0, self.size - length)
        return self.pread(start, self.size - start)


def spool(
    transport: Transport,
    url: str,
    path: Optional[str] = None,
    expected_size: Optional[int] = None,
    headers: Optional[dict[str, str]] = None,
    on_progress: Optional[Callable[[int], None]] = None,
) -> SpooledFile:
    """Stream ``url`` to disk in one pass and open the result for random access.

    ``path`` names a file to keep and to reuse: an existing one of exactly the expected
    size is taken as the archive and nothing is transferred. Without it the spool goes to
    a temporary file that is deleted when the returned object is closed.
    """
    keep = path is not None
    if path is not None and expected_size is not None and _usable(path, expected_size):
        log.info("reusing the spool already at %s (%s)", path, human_bytes(expected_size))
        return SpooledFile(
            open(path, "rb"), url, expected_size, headers or {}, transport, path, keep
        )

    stream = transport.open_stream(url)
    try:
        if expected_size is not None and stream.size != expected_size:
            # Worth saying rather than silently trusting the second number: on a gated
            # host the probe and the transfer are two separate grants, and a size that
            # moved between them means the thing behind the URL moved too.
            log.warning(
                "size changed between probe and transfer: %s then %s",
                human_bytes(expected_size),
                human_bytes(stream.size),
            )
        target, fh = _open_target(path)
        written = 0
        try:
            for block in stream.chunks(WRITE_CHUNK):
                fh.write(block)
                written += len(block)
                if on_progress is not None:
                    on_progress(written)
            fh.flush()
        finally:
            fh.close()

        if written != stream.size:
            _discard(target, keep)
            raise SpoolError(
                f"transfer ended after {human_bytes(written)} of {human_bytes(stream.size)}. "
                f"This server serves no partial content, so there is nothing to resume "
                f"from — the whole file has to come again."
            )
        log.info("spooled %s to %s", human_bytes(written), target)
        return SpooledFile(
            open(target, "rb"),
            url,
            written,
            headers or stream.headers,
            transport,
            target,
            keep,
        )
    finally:
        stream.close()


def _usable(path: str, expected_size: int) -> bool:
    """Is the file already on disk the whole archive, rather than half of one?"""
    try:
        return os.path.getsize(path) == expected_size
    except OSError:
        return False


def _open_target(path: Optional[str]):
    if path is not None:
        return path, open(path, "wb")
    fd, temp = tempfile.mkstemp(prefix="rvtree-", suffix=".spool")
    return temp, os.fdopen(fd, "wb")


def _discard(path: str, keep: bool) -> None:
    """Remove a spool that came up short, so it is never mistaken for a complete one."""
    if keep:
        # Named by the user, and a partial file under a name they chose would be taken
        # for the archive by the next run. The reuse check compares sizes, but only the
        # empty-handed case is safe to leave lying around.
        try:
            os.truncate(path, 0)
        except OSError:
            pass
        return
    try:
        os.unlink(path)
    except OSError:
        pass
