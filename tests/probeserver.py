"""A deliberately awkward HTTP server for the size-probe tests.

``remote_viewer/tests/rangeserver.py`` already models a well-behaved range server and is
reused directly wherever it fits. This one covers the failure modes it cannot express --
the ones the probe ladder exists to survive:

* ``head_status``         -- answer HEAD with 405, as HEAD-hostile onion services do
* ``range_status``        -- answer any Range request with 501, which is what puts the
                             plain streamed GET on the ladder in the first place
* ``ignore_ranges``       -- answer a Range request with 200 and the whole entity
* ``omit_content_length`` -- answer 200 with no Content-Length at all
* ``chunked``             -- reply chunked, so no length is advertised anywhere
* ``redirect``            -- 302 to the real path: the case ``requests.head()`` mishandles

It logs the request line of every probe it receives, so a test can assert which rung of
the ladder answered, and it records how much of the payload it actually managed to write,
so a test can assert that aborting a streamed GET really does leave the body untransferred.
"""

from __future__ import annotations

import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WRITE_CHUNK = 64 * 1024
RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


class Config:
    body = b""
    head_status = 200
    range_status = 206
    ignore_ranges = False
    omit_content_length = False
    chunked = False
    redirect = ""
    extra_headers: dict[str, str] = {}
    requests: list[tuple[str, str]] = []
    bytes_written = 0
    body_finished = threading.Event()
    lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep test output clean
        pass

    def _redirecting(self) -> bool:
        return bool(Config.redirect) and self.path != Config.redirect

    def _send_redirect(self) -> None:
        self.send_response(302)
        self.send_header("Location", Config.redirect)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_extra(self) -> None:
        for key, value in Config.extra_headers.items():
            self.send_header(key, value)

    def _send_entity_headers(self, size: int) -> None:
        self.send_response(200)
        self.send_header("Accept-Ranges", "bytes")
        if Config.chunked:
            self.send_header("Transfer-Encoding", "chunked")
        elif Config.omit_content_length:
            # Neither a length nor chunking, so HTTP/1.1 needs the body delimited by
            # the close of the connection.
            self.send_header("Connection", "close")
            self.close_connection = True
        else:
            self.send_header("Content-Length", str(size))
        self._send_extra()
        self.end_headers()

    def _write_payload(self, body: bytes) -> None:
        """Write the payload in pieces, recording how much of it got out.

        The probe drops the socket as soon as it has the headers, so on a large body
        this raises part-way through; what it managed to write before that is exactly
        the measurement the abort test is after.
        """
        try:
            for i in range(0, len(body), WRITE_CHUNK):
                piece = body[i:i + WRITE_CHUNK]
                if Config.chunked:
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(piece), piece))
                else:
                    self.wfile.write(piece)
                with Config.lock:
                    Config.bytes_written += len(piece)
            if Config.chunked:
                self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            Config.body_finished.set()

    def _log(self) -> None:
        with Config.lock:
            Config.requests.append((self.command, self.headers.get("Range") or "-"))

    def do_HEAD(self):
        self._log()
        if self._redirecting():
            self._send_redirect()
            return
        if Config.head_status != 200:
            self.send_error(Config.head_status)
            return
        self._send_entity_headers(len(Config.body))

    def do_GET(self):
        self._log()
        if self._redirecting():
            self._send_redirect()
            return

        rng = self.headers.get("Range")
        if rng and Config.ignore_ranges:
            rng = None  # answer as though no Range had been sent at all
        if rng and Config.range_status != 206:
            self.send_error(Config.range_status)
            return

        size = len(Config.body)
        if rng:
            self._send_range(rng, size)
            return

        self._send_entity_headers(size)
        self._write_payload(Config.body)

    def _send_range(self, rng: str, size: int) -> None:
        """Serve a single range for real.

        The probe only ever asks for ``bytes=0-0``, but the download path that follows
        it asks for the real chunks, so a server that answered every range with the
        same one byte would quietly corrupt any end-to-end use of this fixture.
        """
        m = RANGE_RE.match(rng.strip())
        if not m:
            self.send_error(501, "Unsupported client range")
            return
        start_s, end_s = m.group(1), m.group(2)
        if start_s == "":
            start, end = max(0, size - int(end_s or 0)), size - 1
        else:
            start = int(start_s)
            end = min(int(end_s), size - 1) if end_s else size - 1
        if start >= size or end < start:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        self.send_response(206)
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self._send_extra()
        self.end_headers()
        self._write_payload(Config.body[start:end + 1])


class QuietServer(ThreadingHTTPServer):
    """A client hanging up mid-body is what these tests are checking for, not an error.

    Without this, every aborted probe prints a socketserver traceback and buries the
    test output.
    """

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


class ProbeServer:
    """Context manager yielding a base URL, mirroring rangeserver.RangeServer."""

    def __init__(
        self,
        body: bytes = b"",
        head_status: int = 200,
        range_status: int = 206,
        ignore_ranges: bool = False,
        omit_content_length: bool = False,
        chunked: bool = False,
        redirect: str = "",
        extra_headers: dict = None,
    ):
        Config.body = body
        Config.head_status = head_status
        Config.range_status = range_status
        Config.ignore_ranges = ignore_ranges
        Config.omit_content_length = omit_content_length
        Config.chunked = chunked
        Config.redirect = redirect
        Config.extra_headers = extra_headers or {}
        Config.requests = []
        Config.bytes_written = 0
        Config.body_finished.clear()
        self.httpd = QuietServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def requests(self) -> list[tuple[str, str]]:
        return list(Config.requests)

    @property
    def methods(self) -> list[str]:
        return [method for method, _ in Config.requests]

    @property
    def bytes_written(self) -> int:
        return Config.bytes_written

    def wait_for_body(self, timeout: float = 10.0) -> bool:
        """Block until a payload write has finished or been cut off.

        Without this the abort test would read ``bytes_written`` while the handler
        thread is still filling the socket, and race its own assertion.
        """
        return Config.body_finished.wait(timeout)

    def __enter__(self) -> "ProbeServer":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
