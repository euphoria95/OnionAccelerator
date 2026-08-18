"""Local directory-listing servers for the crawl tests.

`http.server.SimpleHTTPRequestHandler` already generates a real, unmocked autoindex --
a seventh flavour the parser has never been shown -- so the crawl end-to-end test runs
against genuinely server-generated HTML rather than a fixture.

What it does not do is fail, and failure is most of what the crawler's control flow is
about. So `IndexServer` wraps it with two switches:

* ``flaky``   -- paths that answer 503 for their first N requests before serving
                 normally, which is what an overloaded onion looks like;
* ``request_log`` -- every path served, so a test can assert how many attempts a URL
                 actually took.

`FileManagerServer` is the other shape entirely: an application serving a filesystem,
which is what the leak sites run and what an autoindex-shaped server cannot stand in
for. See its docstring.
"""

from __future__ import annotations

import html
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterable, Optional
from urllib.parse import quote, unquote


class IndexServer:
    """A threaded HTTP server serving `directory`, with optional injected failures."""

    # A page with links but none of a listing's structure: no sort controls, no up-link,
    # a search form, and a title that is not "Index of". This is what the crawler must
    # refuse to walk into, and it scores well under the confidence threshold.
    APPLICATION_PAGE = (
        "<html><head><title>Welcome</title></head><body>"
        '<form action="/search"><input name="q"></form>'
        '<a href="/rules">Rules</a><a href="/faq">FAQ</a><a href="/login">Log in</a>'
        '<a href="topic-1">First topic</a><a href="topic-2">Second topic</a>'
        "</body></html>"
    )

    def __init__(
        self,
        directory: str,
        *,
        flaky: Optional[dict[str, int]] = None,
        retry_after: Optional[str] = "0",
        applications: Optional[Iterable[str]] = None,
    ) -> None:
        self.directory = directory
        self.flaky = dict(flaky or {})
        self.retry_after = retry_after
        # Paths that answer 200 with an application instead of a listing.
        self.applications = set(applications or ())
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
                if self.path in outer.applications:
                    body = outer.APPLICATION_PAGE.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
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


class ApiServer:
    """A file manager that has no listings at all -- only a JSON API.

    The shape AList, FileGator and h5ai have, and the one the old crawler could not touch:
    every directory is the *same URL*, answered only to a POST whose body says which
    directory is wanted. A GET of any browse URL returns a JavaScript shell with no links
    in it, so a structural reader finds nothing, scores zero, and records the target as a
    single empty page -- with no error anywhere.

    Kept deliberately minimal: one endpoint, one body field, an AList-shaped answer. What
    is being tested is the engine's ability to carry a method and a body from a template
    all the way through the frontier and the fetcher, not anybody's exact API.
    """

    ENDPOINT = "/api/fs/list"

    def __init__(self, directory: str) -> None:
        self.directory = directory
        self.request_log: list[str] = []      # the *paths asked for*, not the URLs
        self.method_log: list[str] = []
        self._lock = threading.Lock()
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def __enter__(self) -> "ApiServer":
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):                      # noqa: N802 - stdlib naming
                with outer._lock:
                    outer.method_log.append("GET")
                raw = unquote(self.path.split("?", 1)[0]).strip("/")
                target = os.path.join(outer.directory, raw)
                if os.path.isfile(target):
                    # Files really are served over HTTP; only listings are API-only.
                    with open(target, "rb") as fh:
                        self._respond(fh.read(), "application/octet-stream")
                    return
                # Every browse URL answers the same shell, exactly like the real thing.
                self._respond(
                    "<!doctype html><html><head><title>files</title></head>"
                    "<body><div id='root'></div><script src='/assets/alist.js'>"
                    "</script></body></html>",
                    "text/html; charset=utf-8")

            def do_POST(self):                     # noqa: N802 - stdlib naming
                with outer._lock:
                    outer.method_log.append("POST")
                if self.path.split("?", 1)[0] != outer.ENDPOINT:
                    self.send_error(404)
                    return
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode("utf-8") if length else "{}"
                try:
                    asked = str(json.loads(body).get("path", "")).strip("/")
                except ValueError:
                    self.send_error(400)
                    return
                with outer._lock:
                    outer.request_log.append(asked)

                target = os.path.join(outer.directory, asked)
                if not os.path.isdir(target):
                    self._respond(json.dumps({"code": 500, "message": "object not found"}),
                                  "application/json")
                    return
                content = []
                for name in sorted(os.listdir(target)):
                    full = os.path.join(target, name)
                    is_dir = os.path.isdir(full)
                    content.append({
                        "name": name,
                        "is_dir": is_dir,
                        "size": 0 if is_dir else os.path.getsize(full),
                        "modified": "2024-01-01T00:00:00Z",
                    })
                self._respond(
                    json.dumps({"code": 200, "data": {"content": content,
                                                      "total": len(content)}}),
                    "application/json")

            def _respond(self, body, content_type: str) -> None:
                if isinstance(body, str):
                    body = body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

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

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.server_address[1]

    @property
    def seed_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def template(self) -> str:
        """A profile for this server, as an operator would write it for a real one."""
        return (
            'name = "test-api"\n'
            'title = "The local API test server"\n'
            'priority = 95\n'
            '[match]\n'
            'body_regex = "alist"\n'
            '[extract]\n'
            'strategy = "json"\n'
            'rows = "data.content"\n'
            'dir_field = "is_dir"\n'
            '[navigate]\n'
            'kind = "api"\n'
            'method = "POST"\n'
            f'url = "{{origin}}{self.ENDPOINT}"\n'
            'body = \'{"path": "/{path}"}\'\n'
            '[navigate.headers]\n'
            'Content-Type = "application/json"\n'
            '[download]\n'
            'url = "{origin}/{path}"\n'
        )


class FileManagerServer:
    """A file manager that addresses a nested directory with an encoded separator.

    Not a variant template: a different *addressing scheme*. An autoindex serves the
    tree at the tree's own paths, so `/a/deep/` is one URL with three segments. A file
    manager serves the whole tree from one route and passes the relative path as a
    parameter, so `a/deep` arrives percent-encoded into a single segment:

        /r/fm/TOK/a%2Fdeep/buried.txt

    Read as raw path text that is a directory literally named `a%2Fdeep`, which is
    below neither the page that linked it nor the crawl's scope root -- so every link
    on every page below the first nested level is discarded as off-tree navigation and
    the crawl stops one level down, having found only the top of the tree. The parent
    up-link is the same trap in reverse: this route spells it with real separators
    (`/r/fm/TOK/a/deep`), so the page's own URL and its parent's are encoded
    differently and only a decoded comparison sees the relationship.

    Everything else is copied from the shape these file managers actually have: no
    "Index of" title, no server `<address>` footer, a search `<form>` on every page,
    Apache-style column-sort links, folder icons rather than trailing slashes, and
    directory URLs served 200 without a redirect to a trailing slash.
    """

    ROUTE = "/r/fm/TOK"

    def __init__(self, directory: str) -> None:
        self.directory = directory
        self.request_log: list[str] = []
        self._lock = threading.Lock()
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------ lifecycle

    def __enter__(self) -> "FileManagerServer":
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):                      # noqa: N802 - stdlib naming
                raw = self.path.split("?", 1)[0]
                with outer._lock:
                    outer.request_log.append(raw)
                if not raw.startswith(outer.ROUTE):
                    self.send_error(404)
                    return
                # The route's parameter, decoded exactly once -- which is the whole
                # point: `a%2Fdeep` and `a/deep` name the same directory here.
                relative = unquote(raw[len(outer.ROUTE):]).strip("/")
                target = os.path.join(outer.directory, relative)
                if os.path.isdir(target):
                    self._respond(outer.render(relative), "text/html; charset=utf-8")
                elif os.path.isfile(target):
                    with open(target, "rb") as fh:
                        self._respond(fh.read(), "application/octet-stream")
                else:
                    self.send_error(404)

            def _respond(self, body, content_type: str) -> None:
                if isinstance(body, str):
                    body = body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

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

    # ------------------------------------------------------------ the template

    def render(self, relative: str) -> str:
        """The listing for `relative`, with children addressed the file manager's way."""
        rows = [self._parent_row(relative)] if relative else []
        target = os.path.join(self.directory, relative)
        for name in sorted(os.listdir(target)):
            is_dir = os.path.isdir(os.path.join(target, name))
            size = "-" if is_dir else str(os.path.getsize(os.path.join(target, name)))
            icon = "folder.svg" if is_dir else "file.svg"
            rows.append(
                f'<tr><td class="link"><a href="{self.child_url(relative, name)}">'
                f'<img class="icons" src="/static/icons/{icon}">{html.escape(name)}</a>'
                f'</td><td class="size">{size}</td><td class="date">2024-01-01 00:00</td></tr>'
            )
        return (
            "<html><head><meta charset='utf-8'></head><body>"
            f'<form action="{self.ROUTE}/search" method="GET"><input name="search"></form>'
            '<table id="list"><thead><tr>'
            '<th><a href="?C=N&amp;O=A">File Name</a><a href="?C=N&amp;O=D">down</a></th>'
            '<th><a href="?C=S&amp;O=A">File Size</a><a href="?C=S&amp;O=D">down</a></th>'
            '<th><a href="?C=M&amp;O=A">Date</a><a href="?C=M&amp;O=D">down</a></th>'
            "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></body></html>"
        )

    def child_url(self, relative: str, name: str) -> str:
        """`/r/fm/TOK/<relative, separators encoded>/<name>`.

        At the top level there is no relative path and the result is indistinguishable
        from an ordinary URL; one level down it still is, because a single segment has
        no separator to encode. The encoding only appears from the second level, which
        is exactly why a crawler can look correct against a shallow fixture.
        """
        prefix = f"{self.ROUTE}/{quote(relative, safe='')}" if relative else self.ROUTE
        return f"{prefix}/{quote(name)}"

    def _parent_row(self, relative: str) -> str:
        """The up-link -- spelled with real separators, unlike every other link here."""
        parent = relative.rsplit("/", 1)[0] if "/" in relative else ""
        href = f"{self.ROUTE}/{quote(parent)}" if parent else self.ROUTE
        return (f'<tr><td class="link"><a href="{href}">'
                f'<img class="icons" src="/static/icons/home.png">Parent directory/</a>'
                f'</td><td class="size">-</td><td class="date">-</td></tr>')

    # ------------------------------------------------------------ accessors

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.server_address[1]

    @property
    def seed_url(self) -> str:
        return f"http://127.0.0.1:{self.port}{self.ROUTE}/"

    def hits(self, path: str) -> int:
        with self._lock:
            return self.request_log.count(path)
