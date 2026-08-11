"""A minimal range-capable static file server for the tests.

``http.server`` ignores the Range header entirely and answers 200 with the whole file,
which is useless here. This one implements single ranges properly and can also be told
to misbehave in the two specific ways observed against a real CDN:

* ``reject_suffix``  — answer ``bytes=-N`` with 501, as kernel.org's cache tier does
* ``ignore_ranges``  — answer every range with 200 and the full entity

It can also serve over TLS with a self-signed certificate (``certfile``), which is what
an onion service typically presents, and attach arbitrary response headers
(``extra_headers``) so that Content-Type and Content-Disposition detection can be
exercised against a real response rather than a hand-built dict.
"""

from __future__ import annotations

import os
import re
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


class Config:
    root = "."
    reject_suffix = False
    ignore_ranges = False
    reject_multirange = True
    extra_headers: dict[str, str] = {}
    requests: list[str] = []


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep test output clean
        pass

    def _path(self) -> str:
        rel = self.path.split("?")[0].lstrip("/")
        full = os.path.normpath(os.path.join(Config.root, rel))
        if not full.startswith(os.path.normpath(Config.root)):
            return ""
        return full

    def _common_headers(self):
        self.send_header("Accept-Ranges", "bytes")
        extra = {k.lower(): v for k, v in Config.extra_headers.items()}
        self.send_header("Content-Type", extra.pop("content-type", "application/octet-stream"))
        for key, value in extra.items():
            self.send_header(key, value)

    def do_HEAD(self):
        self.do_GET(head_only=True)

    def do_GET(self, head_only: bool = False):
        full = self._path()
        if not full or not os.path.isfile(full):
            self.send_error(404)
            return
        size = os.path.getsize(full)
        rng = self.headers.get("Range")
        Config.requests.append(rng or "-")

        if rng and "," in rng and Config.reject_multirange:
            self.send_error(501, "Unsupported client range")
            return

        if not rng or Config.ignore_ranges:
            self.send_response(200)
            self.send_header("Content-Length", str(size))
            self._common_headers()
            self.end_headers()
            if not head_only:
                with open(full, "rb") as fh:
                    self.wfile.write(fh.read())
            return

        m = RANGE_RE.match(rng.strip())
        if not m:
            self.send_error(501, "Unsupported client range")
            return
        start_s, end_s = m.group(1), m.group(2)

        if start_s == "":
            if Config.reject_suffix:
                self.send_error(501, "Unsupported client range")
                return
            length = int(end_s or 0)
            start = max(0, size - length)
            end = size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else size - 1
        if start >= size or end < start:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        end = min(end, size - 1)
        length = end - start + 1

        self.send_response(206)
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(length))
        self._common_headers()
        self.end_headers()
        if not head_only:
            with open(full, "rb") as fh:
                fh.seek(start)
                self.wfile.write(fh.read(length))


class RangeServer:
    """Context manager yielding a base URL."""

    def __init__(
        self,
        root: str,
        reject_suffix: bool = False,
        ignore_ranges: bool = False,
        extra_headers: dict = None,
        certfile: str = None,
    ):
        Config.root = root
        Config.reject_suffix = reject_suffix
        Config.ignore_ranges = ignore_ranges
        Config.extra_headers = extra_headers or {}
        Config.requests = []
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.scheme = "http"
        if certfile:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(certfile)
            self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
            self.scheme = "https"
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        return f"{self.scheme}://127.0.0.1:{self.port}"

    def __enter__(self) -> "RangeServer":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    @property
    def requests(self) -> list[str]:
        return list(Config.requests)


if __name__ == "__main__":
    import sys

    root = sys.argv[1] if len(sys.argv) > 1 else "."
    with RangeServer(root) as srv:
        print(f"serving {root} at {srv.base}")
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
