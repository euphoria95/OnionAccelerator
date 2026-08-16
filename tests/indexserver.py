"""A local directory-listing server for the crawl tests.

`http.server.SimpleHTTPRequestHandler` already generates a real, unmocked autoindex --
a seventh flavour the parser has never been shown -- so the crawl end-to-end test runs
against genuinely server-generated HTML rather than a fixture.

What it does not do is fail, and failure is most of what the crawler's control flow is
about. So this wraps it with two switches:

* ``flaky``   -- paths that answer 503 for their first N requests before serving
                 normally, which is what an overloaded onion looks like;
* ``request_log`` -- every path served, so a test can assert how many attempts a URL
                 actually took.
"""

from __future__ import annotations

import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional


class IndexServer:
    """A threaded HTTP server serving `directory`, with optional injected failures."""

    def __init__(
        self,
        directory: str,
        *,
        flaky: Optional[dict[str, int]] = None,
        retry_after: Optional[str] = "0",
    ) -> None:
        self.directory = directory
        self.flaky = dict(flaky or {})
        self.retry_after = retry_after
        self.request_log: list[str] = []
        self._lock = threading.Lock()
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------ lifecycle

    def __enter__(self) -> "IndexServer":
        outer = self

        class Handler(SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=outer.directory, **kwargs)

            def do_GET(self):                      # noqa: N802 - stdlib naming
                with outer._lock:
                    outer.request_log.append(self.path)
                    remaining = outer.flaky.get(self.path, 0)
                    if remaining:
                        outer.flaky[self.path] = remaining - 1
                if remaining:
                    self.send_response(503)
                    if outer.retry_after is not None:
                        self.send_header("Retry-After", outer.retry_after)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                super().do_GET()

            def log_message(self, *args):          # keep the test output readable
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    # ------------------------------------------------------------ accessors

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def hits(self, path: str) -> int:
        with self._lock:
            return self.request_log.count(path)
