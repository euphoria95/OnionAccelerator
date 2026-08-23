"""A server that behaves like a proof-of-work download gate.

Modelled on a live one, and the details are the point — every one of them is a way an
earlier rvtree drew the wrong conclusion:

* the first GET of the archive answers **200**, not 403, carrying an HTML challenge page
  under ``Transfer-Encoding: chunked`` — so there is no Content-Length to read and the
  status code says nothing is wrong;
* the answer is posted to ``/verify``, which **302s** to ``/download?token=...``;
* the token is **single-use**, so a resolved URL cannot be cached and replayed; and
* ``/download`` sends ``Accept-Ranges: none`` and ignores Range entirely, because the
  application is handing the file out rather than the web server.

``verified_ranges`` flips that last one, for the sake of proving that a gate on its own
does not cost rvtree its random access.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

CHALLENGE_PAGE = """<!DOCTYPE html>
<html><head><title>[ SYSTEM VERIFICATION ]</title></head>
<body>
  <form id="form" method="POST" action="/verify" style="display:none;">
    <input type="hidden" name="challenge" value="{seed}">
    <input type="hidden" name="nonce" id="nonce">
  </form>
  <script>
    (function() {{
      const challenge = "{seed}";
      const difficulty =  {difficulty} ;
      async function sha256(message) {{
        const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(message));
        return Array.from(new Uint8Array(buf)).map(b => b.toString(16).padStart(2, "0")).join("");
      }}
    }})();
  </script>
</body></html>
"""


class Config:
    root = "."
    difficulty = 3
    verified_ranges = False
    # Seeds handed out and not yet answered, then tokens granted and not yet spent.
    seeds: set[str] = set()
    tokens: dict[str, str] = {}
    solved = 0
    downloads = 0


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep test output clean
        pass

    def _challenge(self, path: str) -> None:
        """Answer with the gate page, chunked and without a Content-Length."""
        seed = secrets.token_hex(16)
        Config.seeds.add(seed)
        body = CHALLENGE_PAGE.format(seed=seed, difficulty=Config.difficulty).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self.wfile.write(b"%x\r\n%s\r\n0\r\n\r\n" % (len(body), body))

    def do_HEAD(self):
        self._challenge(self.path)

    def do_POST(self):
        if urlsplit(self.path).path != "/verify":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(length).decode())
        seed = (form.get("challenge") or [""])[0]
        nonce = (form.get("nonce") or [""])[0]
        digest = hashlib.sha256((seed + nonce).encode()).hexdigest()
        if seed not in Config.seeds or not digest.startswith("0" * Config.difficulty):
            self.send_error(403, "bad proof of work")
            return
        Config.seeds.discard(seed)
        Config.solved += 1
        token = secrets.token_hex(16)
        Config.tokens[token] = "/payload"
        self.send_response(302)
        self.send_header("Location", f"/download?token={token}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        parts = urlsplit(self.path)
        if parts.path != "/download":
            self._challenge(self.path)
            return
        token = (parse_qs(parts.query).get("token") or [""])[0]
        if token not in Config.tokens:
            # Spent, or never granted. The gate does not say so — it simply starts again.
            self._challenge(self.path)
            return
        del Config.tokens[token]
        Config.downloads += 1
        self._serve()

    def _serve(self) -> None:
        full = os.path.join(Config.root, Config.name)
        size = os.path.getsize(full)
        rng = self.headers.get("Range")

        if rng and Config.verified_ranges:
            start_s, _, end_s = rng.split("=", 1)[1].partition("-")
            start = int(start_s)
            end = int(end_s) if end_s else size - 1
            end = min(end, size - 1)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            with open(full, "rb") as fh:
                fh.seek(start)
                self.wfile.write(fh.read(end - start + 1))
            return

        self.send_response(200)
        self.send_header("Content-Length", str(size))
        self.send_header("Accept-Ranges", "none")
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", f'attachment; filename="{Config.name}"')
        self.end_headers()
        with open(full, "rb") as fh:
            try:
                self.wfile.write(fh.read())
            except BrokenPipeError:
                # A one-byte Range probe reads the headers and hangs up, which is the
                # only sane thing to do when the answer is "the whole file is coming".
                pass


class GateServer:
    """Context manager yielding a base URL, mirroring ``rangeserver.RangeServer``."""

    def __init__(
        self,
        root: str,
        name: str,
        difficulty: int = 3,
        verified_ranges: bool = False,
    ):
        Config.root = root
        Config.name = name
        Config.difficulty = difficulty
        Config.verified_ranges = verified_ranges
        Config.seeds = set()
        Config.tokens = {}
        Config.solved = 0
        Config.downloads = 0
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def url(self) -> str:
        return f"{self.base}/files/{Config.name}"

    @property
    def solved(self) -> int:
        return Config.solved

    @property
    def downloads(self) -> int:
        return Config.downloads

    def __enter__(self) -> "GateServer":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
