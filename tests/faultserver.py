"""A range server that can be told to misbehave, for the download engine's tests.

remote_viewer/tests/rangeserver.py is the right server for rvtree, but the engine needs
things it cannot do: a second instance beside the first (its settings are class-level),
a validator (ETag, Last-Modified) that can change mid-transfer together with the bytes it
guards, `If-Range`, streamed bodies so a multi-GiB file never sits in memory, and a
throttle that singles out one client by its User-Agent -- which is how a test makes one
lane slow, because every lane pins its own.

Faults are callables `(Req) -> Optional[Fault]`, tried in order for every request; the
first answer wins, and a callable may also have side effects (call `swap()`), which is how
a test flips the file under a transfer after N requests. The five faults the engine has
to survive:

  Stall(after, seconds)   stop sending after `after` body bytes, for `seconds`
  Drop(after)             close the connection after `after` body bytes
  Status200()             answer a ranged request with 200 and the whole entity
  Html200()               answer with 200 and a small HTML error page
  (swap)                  change ETag and content under the client's feet
"""

from __future__ import annotations

import dataclasses
import functools
import hashlib
import re
import socket
import threading
import time
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

RANGE_RE = re.compile(r"^bytes=(\d+)-(\d*)$")
CHUNK = 64 * 1024


# ----------------------------------------------------------------------- content


class BytesSource:
    def __init__(self, data: bytes):
        self._data = data
        self.size = len(data)

    def read(self, start: int, length: int) -> bytes:
        return self._data[start:start + length]

    def sha256(self) -> str:
        return hashlib.sha256(self._data).hexdigest()


class PatternSource:
    """`size` bytes that are never repeated at any 1 MiB boundary, generated on demand.

    A repeating pattern would let a piece written to the wrong offset pass for the right
    one whenever the period divides the piece size. Each 1 MiB block here is the seed
    block rotated by a different odd multiple, so every block is distinct and a misplaced
    piece changes the file's hash.
    """

    BLOCK = 1024 * 1024

    def __init__(self, size: int, seed: int = 1):
        import random

        self.size = size
        self._base = random.Random(seed).randbytes(self.BLOCK)
        # Eight lanes read neighbouring blocks at once; without this every 64 KiB chunk
        # would re-rotate a whole MiB.
        self._block = functools.lru_cache(maxsize=64)(self._rotated)

    def _rotated(self, k: int) -> bytes:
        shift = (k * 7919) % self.BLOCK
        return self._base[shift:] + self._base[:shift]

    def read(self, start: int, length: int) -> bytes:
        end = min(start + length, self.size)
        out = bytearray()
        pos = start
        while pos < end:
            k, within = divmod(pos, self.BLOCK)
            block = self._block(k)
            out += block[within:within + (end - pos)]
            pos += min(self.BLOCK - within, end - pos)
        return bytes(out)

    def sha256(self) -> str:
        digest = hashlib.sha256()
        pos = 0
        while pos < self.size:
            piece = self.read(pos, self.BLOCK)
            digest.update(piece)
            pos += len(piece)
        return digest.hexdigest()


# ----------------------------------------------------------------------- faults


@dataclasses.dataclass
class Stall:
    after: int
    seconds: float


@dataclasses.dataclass
class Drop:
    after: int


@dataclasses.dataclass
class Status200:
    pass


@dataclasses.dataclass
class Html200:
    pass


@dataclasses.dataclass
class Req:
    n: int                      # 1-based, across every request this server has seen
    method: str
    name: str
    start: Optional[int]        # None for a request without a Range header
    end: Optional[int]
    ua: str
    if_range: Optional[str]
    status: int = 0             # filled in when the response line is sent


Fault = object  # one of the dataclasses above


class FaultServer:
    """Serves named sources over loopback HTTP with ranges, validators and faults."""

    def __init__(self, files: dict[str, object], *, etag: str = '"v1"',
                 honor_if_range: bool = True, validators: bool = True,
                 hide_length: bool = False):
        self.files = dict(files)
        self.etag = etag
        self.last_modified = formatdate(1_700_000_000, usegmt=True)
        self.honor_if_range = honor_if_range
        self.validators = validators
        # No Content-Length, no ranges: the body just ends when the connection does.
        self.hide_length = hide_length
        self.faults: list[Callable[[Req], Optional[Fault]]] = []
        # ua -> bytes per second, or None for full speed.
        self.throttle: Callable[[str], Optional[float]] = lambda ua: None
        self.log: list[Req] = []
        self.body_bytes = 0                 # bytes actually written, all responses
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_HEAD(self):
                outer._serve(self, head=True)

            def do_GET(self):
                outer._serve(self, head=False)

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            request_queue_size = 128

        self.httpd = Server(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    # ------------------------------------------------------------ lifecycle
    def __enter__(self) -> "FaultServer":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def url(self, name: str) -> str:
        return f"http://127.0.0.1:{self.port}/{name}"

    # ------------------------------------------------------------ test controls
    def swap(self, name: str, source: object, etag: str) -> None:
        """The file changes: new bytes and, with them, a new validator."""
        with self._lock:
            self.files[name] = source
            self.etag = etag
            self.last_modified = formatdate(1_800_000_000, usegmt=True)

    def ranged(self) -> list[Req]:
        with self._lock:
            return [r for r in self.log if r.start is not None]

    def starts(self) -> set[int]:
        return {r.start for r in self.ranged()}

    # ------------------------------------------------------------ protocol
    def _serve(self, h: BaseHTTPRequestHandler, head: bool) -> None:
        name = h.path.split("?")[0].lstrip("/")
        rng = h.headers.get("Range")
        match = RANGE_RE.match(rng.strip()) if rng else None
        with self._lock:
            req = Req(len(self.log) + 1, h.command, name,
                      int(match.group(1)) if match else None,
                      int(match.group(2)) if match and match.group(2) else None,
                      h.headers.get("User-Agent", ""), h.headers.get("If-Range"))
            self.log.append(req)
            fault = None
            for rule in self.faults:
                fault = rule(req)
                if fault is not None:
                    break
            source = self.files.get(name)
            etag, last_modified = self.etag, self.last_modified
        if source is None:
            req.status = 404
            h.send_error(404)
            return
        size = source.size

        if isinstance(fault, Html200) and not head:
            page = b"<html><body>502 upstream is busy, try again</body></html>"
            req.status = 200
            h.send_response(200)
            h.send_header("Content-Type", "text/html")
            h.send_header("Content-Length", str(len(page)))
            h.end_headers()
            h.wfile.write(page)
            return

        start, end, status = 0, size - 1, 200
        if match and not isinstance(fault, Status200) and not self.hide_length:
            start = int(match.group(1))
            end = min(int(match.group(2)) if match.group(2) else size - 1, size - 1)
            status = 206
            if req.if_range is not None and self.honor_if_range:
                if req.if_range not in (etag, last_modified):
                    # The validator is stale: the file changed, so HTTP says to ignore
                    # the Range and send the new entity whole.
                    start, end, status = 0, size - 1, 200
            if status == 206 and (start >= size or end < start):
                req.status = 416
                h.send_response(416)
                h.send_header("Content-Range", f"bytes */{size}")
                h.send_header("Content-Length", "0")
                h.end_headers()
                return
        length = end - start + 1

        req.status = status
        h.send_response(status)
        if status == 206:
            h.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if self.hide_length:
            h.send_header("Connection", "close")
            h.close_connection = True
        else:
            h.send_header("Content-Length", str(length))
        h.send_header("Content-Type", "application/octet-stream")
        h.send_header("Accept-Ranges", "bytes")
        if self.validators:
            h.send_header("ETag", etag)
            h.send_header("Last-Modified", last_modified)
        h.end_headers()
        if head:
            return

        bps = self.throttle(req.ua)
        sent = 0
        try:
            while sent < length:
                n = min(CHUNK, length - sent)
                if isinstance(fault, (Stall, Drop)):
                    n = min(n, max(fault.after - sent, 0)) or n
                chunk = source.read(start + sent, n)
                if isinstance(fault, Drop) and sent >= fault.after:
                    h.close_connection = True
                    try:
                        h.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    return
                if isinstance(fault, Stall) and sent >= fault.after:
                    time.sleep(fault.seconds)
                    fault = None
                h.wfile.write(chunk)
                sent += len(chunk)
                with self._lock:
                    self.body_bytes += len(chunk)
                if bps:
                    time.sleep(len(chunk) / bps)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            h.close_connection = True
