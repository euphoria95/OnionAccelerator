"""The HTTP face of the bus: an endless NDJSON body, and three things to ask about it.

    GET /events    one JSON object per line, forever, until the run ends
    GET /status    a snapshot: counters, cursor, who is attached
    GET /schema    the event kinds and what they mean
    GET /health    liveness, for a supervisor

The reason it is HTTP and not a bare socket is the query string. A hunt is a filter, and
a filter that travels in the URL is a filter that can be written by hand, pasted into a
ticket, and run by anyone with curl:

    curl -sN 'http://127.0.0.1:8787/events?kinds=crawl.file&match=(?i)passw'

Stdlib only, and deliberately small. This serves one process's own event sequence to a
handful of local consumers; it is not a web framework and must never grow into one.

Opsec. The stream carries live onion URLs and the file names of somebody's breach, so
the defaults are the careful ones: loopback, no CORS, a Host check so a page in the
analyst's browser cannot reach it by rebinding DNS, and a flat refusal to bind anywhere
but loopback without a token. That refusal is an error rather than a warning on purpose
-- the same reasoning that keeps a target URL out of the external-proxy liveness check.
"""

from __future__ import annotations

import dataclasses
import hmac
import ipaddress
import json
import logging
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional
from urllib.parse import urlparse, parse_qs

from .bus import KINDS, STREAM_GAP, STREAM_HEARTBEAT, STREAM_HELLO, EventBus
from .filters import LINE, NDJSON, Selector, SelectorError

logger = logging.getLogger("OnionAccelerator.eventstream")

API_VERSION = 1
DEFAULT_BIND = "127.0.0.1:8787"
# How long a consumer's socket may take to send its request line before its thread is
# reclaimed. Nothing to do with how long a *stream* may stay open, which is forever.
REQUEST_TIMEOUT = 30.0
NDJSON_TYPE = "application/x-ndjson"
SSE_TYPE = "text/event-stream"

_USAGE = """\
OnionAccelerator event stream (API v{version}), run {run}, mode {mode}.

  GET /events   one JSON object per line, until the run ends
  GET /status   counters, cursor and attached consumers
  GET /schema   the event kinds
  GET /health   liveness

/events parameters:
  since=SEQ     replay everything after this sequence number, then follow
  kinds=A,B     only these kinds; a family prefix works ('crawl' takes every crawl.*)
  match=REGEX   only lines matching this regex
  shape=SHAPE   ndjson (default) or line, which emits only the URL or path
  heartbeat=S   seconds between keepalives on an idle stream; 0 disables

Examples:
  curl -sN '{url}/events?kinds=crawl.file&match=(?i)passw'
  curl -sN '{url}/events?shape=line&kinds=crawl.file' | tee found.txt
  curl -s  '{url}/status' | jq
"""


class StreamServer:
    """Serves one EventBus over HTTP, on its own thread.

    Owns nothing about the run: it is handed a bus and a callable that can describe the
    run's own counters, and it never reaches back into the crawl.
    """

    def __init__(self, bus: EventBus, bind: str = DEFAULT_BIND, *,
                 token: Optional[str] = None,
                 status: Optional[Callable[[], dict[str, Any]]] = None) -> None:
        self.bus = bus
        self.token = token
        self.status = status
        self.host, self.port = parse_bind(bind)
        if not is_loopback(self.host) and not token:
            raise ValueError(
                f"refusing to bind the event stream to {self.host}: anything but "
                f"loopback publishes the run's findings to the network. Pass "
                f"--stream-token to do it deliberately, or bind 127.0.0.1 and reach it "
                f"over an SSH tunnel.")
        self._http: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._first_consumer = threading.Event()
        bus.on_subscribe(self._first_consumer.set)

    # -- lifecycle -------------------------------------------------------------
    def start(self) -> "StreamServer":
        """Bind, then serve on a daemon thread. Raises OSError if the port is taken."""
        handler = _handler_for(self)
        # parse_bind accepts '[::1]:8787', so the socket has to be able to be an IPv6
        # one: ThreadingHTTPServer only ever opens AF_INET, and an IPv6 bind that got
        # this far would die in getaddrinfo instead of listening.
        family = socket.AF_INET6 if ":" in self.host else socket.AF_INET

        class _Server(ThreadingHTTPServer):
            address_family = family

        self._http = _Server((self.host, self.port), handler)
        # A consumer holds its connection open for the whole run; without daemon threads
        # a single attached grep would stop the process from ever exiting.
        self._http.daemon_threads = True
        # Port 0 means "pick one" -- take back what was picked, so it can be logged.
        self.port = self._http.server_address[1]
        self._thread = threading.Thread(target=self._http.serve_forever,
                                        name="eventstream-http", daemon=True)
        self._thread.start()
        logger.info("event stream on %s -- attach with: curl -sN '%s/events'",
                    self.url, self.url)
        return self

    def stop(self, drain_timeout: float = 5.0) -> None:
        """Let consumers finish reading, then shut down.

        The wait is what makes the last events of a run -- run.stop and the totals with
        it -- reach a grep that was still catching up when the crawl ended. It is
        bounded: a consumer that has genuinely stopped reading must not hold the process
        open, having already been told it is behind.
        """
        deadline = time.monotonic() + max(0.0, drain_timeout)
        while self.bus.subscribers and not self.bus.drained():
            if time.monotonic() >= deadline:
                logger.warning("event stream: %d consumer(s) still behind after %.0fs; "
                               "closing anyway (the JSONL on disk is complete)",
                               self.bus.subscribers, drain_timeout)
                break
            time.sleep(0.05)
        self.bus.close()
        if self._http is not None:
            self._http.shutdown()
            self._http.server_close()
            self._http = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def wait_for_consumer(self, timeout: Optional[float] = None) -> bool:
        """Block until something attaches to /events. True if one did."""
        return self._first_consumer.wait(timeout)

    def __enter__(self) -> "StreamServer":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # -- what the handler asks it ---------------------------------------------
    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"

    def authorised(self, headers, query: dict) -> bool:
        """Bearer token or ?token=. No token configured means loopback-only, so open."""
        if not self.token:
            return True
        offered = ""
        header = headers.get("Authorization", "")
        if header.startswith("Bearer "):
            offered = header[7:].strip()
        elif query.get("token"):
            offered = query["token"][0]
        # Compared as bytes: compare_digest refuses two str that are not both ASCII, so
        # a token with an accent in it -- or a ?token= that carries one -- would raise
        # out of the handler instead of answering 401.
        return hmac.compare_digest(offered.encode("utf-8", "replace"),
                                   self.token.encode("utf-8", "replace"))

    def host_allowed(self, headers) -> bool:
        """Reject a Host this server was not asked to answer to.

        Without this, a page the analyst has open in a browser can point a fetch at
        http://<attacker-controlled-name>/events, have it resolve to 127.0.0.1, and read
        the run. The Host header is the one part of that request the attacker's page
        cannot forge.

        A wildcard bind is the exception: 0.0.0.0 answers on every address the host has,
        so the name that reached it is not something the request chose and cannot be
        checked against the bind. That bind is already refused without --stream-token,
        which is what actually guards it.
        """
        name = _host_name(headers.get("Host", ""))
        if not name:
            return False
        if name == self.host or name == "localhost" or is_loopback(name):
            return True
        return is_wildcard(self.host)

    def snapshot(self) -> dict[str, Any]:
        payload = {"api": API_VERSION, "stream": self.bus.stats()}
        if self.status is not None:
            try:
                payload["run"] = self.status()
            except Exception as exc:                                  # noqa: BLE001
                # /status must answer even when the run is mid-collapse; that is when
                # somebody is most likely to be asking it.
                payload["run"] = {"error": f"{type(exc).__name__}: {exc}"}
        return payload


def _handler_for(server: StreamServer):
    """Build the handler class bound to one StreamServer."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = f"OnionAccelerator-eventstream/{API_VERSION}"
        timeout = REQUEST_TIMEOUT
        stream = server

        # -- plumbing ---------------------------------------------------------
        def log_message(self, fmt: str, *args) -> None:
            """Into the run's log at debug, not onto the run's console.

            The default writes every request to stderr, which for a mode that already
            draws a progress bar there would be actively destructive.
            """
            logger.debug("%s %s", self.address_string(), fmt % args)

        def do_GET(self) -> None:                                     # noqa: N802
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query, keep_blank_values=True)

            if not self.stream.host_allowed(self.headers):
                self._json(403, {"error": "host not allowed",
                                 "detail": "this stream answers only to its bind address"})
                return
            if not self.stream.authorised(self.headers, query):
                self._json(401, {"error": "unauthorised",
                                 "detail": "pass Authorization: Bearer <token> or ?token="},
                           headers=[("WWW-Authenticate", 'Bearer realm="eventstream"')])
                return

            route = parsed.path.rstrip("/") or "/"
            if route == "/health":
                self._json(200, {"ok": True, "run": self.stream.bus.run})
            elif route == "/schema":
                self._json(200, {"api": API_VERSION, "envelope":
                                 ["seq", "ts", "run", "mode", "kind", "data"],
                                 "kinds": KINDS})
            elif route == "/status":
                self._json(200, self.stream.snapshot())
            elif route == "/events":
                self._events(query)
            elif route == "/":
                self._text(200, _USAGE.format(version=API_VERSION, run=self.stream.bus.run,
                                              mode=self.stream.bus.mode,
                                              url=self.stream.url))
            else:
                self._json(404, {"error": "no such endpoint",
                                 "detail": "try /events, /status, /schema or /health"})

        # -- the stream -------------------------------------------------------
        def _events(self, query: dict) -> None:
            try:
                selector = Selector.from_query(query)
            except SelectorError as exc:
                self._json(400, {"error": "bad query", "detail": str(exc)})
                return

            sse = SSE_TYPE in (self.headers.get("Accept") or "")
            if sse and selector.since is None:
                # A browser reconnecting an EventSource sends the last id it saw; that is
                # exactly `since`, and honouring it is what makes SSE recovery work.
                last = self.headers.get("Last-Event-ID")
                if last and last.isdigit():
                    selector = dataclasses.replace(selector, since=int(last))

            # Registered before the headers go out, and everything after it is inside
            # the try: a consumer that dies between connect and first byte must still be
            # unsubscribed, or its queue fills with the rest of the run for nobody.
            subscriber = self.stream.bus.subscribe(selector.since)
            try:
                self.send_response(200)
                self.send_header("Content-Type", SSE_TYPE if sse else NDJSON_TYPE)
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Cache-Control", "no-store")
                # Belt and braces for anyone who puts a reverse proxy in front of this:
                # both nginx and some corporate middleboxes buffer a response body by
                # default, which would turn a live stream into a very long silence.
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()

                self._pump(subscriber, selector, sse)
                # The run ended, so end the body properly rather than by hanging up.
                # Without the terminating chunk a consumer cannot tell "the crawl
                # finished" from "the connection broke": curl exits 18 either way, and a
                # pipeline that treats that as failure would report a completed run as a
                # crashed one.
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, socket.timeout, OSError):
                logger.debug("event stream consumer %s went away", self.address_string())
            finally:
                subscriber.close()
                self.close_connection = True

        def _pump(self, subscriber, selector, sse: bool) -> None:
            """Feed one consumer until it leaves or the run ends."""
            self._control(STREAM_HELLO, _hello(self.stream, selector), selector, sse)

            # A consumer that has gone away is only discovered by writing to it. The
            # heartbeat is therefore also the reaper: it guarantees a write, and so a
            # discovery, at least that often even when the run has nothing to say.
            #
            # Keyed off the last *write*, not off an empty take: the common case is a
            # busy run and a narrow filter -- 'kinds=crawl.file&match=payroll' -- where
            # every take returns events and every one of them is dropped. Treating that
            # as activity is how such a consumer sits silent behind a NAT for an hour
            # and is never noticed to have gone.
            wait = selector.heartbeat or 60.0
            last_write = time.monotonic()
            while True:
                events, lost, closed = subscriber.take(wait)
                if lost:
                    self._control(STREAM_GAP, _gap(lost, events[0].seq if events else None),
                                  selector, sse)
                    last_write = time.monotonic()
                for event in events:
                    if not selector.wants(event):
                        continue
                    payload = selector.render(event)
                    if payload is not None:
                        self._emit(payload, sse, seq=event.seq)
                        last_write = time.monotonic()
                if closed:
                    return
                if selector.heartbeat and time.monotonic() - last_write >= selector.heartbeat:
                    self._control(STREAM_HEARTBEAT, _heartbeat(self.stream.bus),
                                  selector, sse)
                    last_write = time.monotonic()

        def _control(self, kind: str, data: dict, selector: Selector, sse: bool) -> None:
            """A protocol line, in the shape this consumer asked for.

            Under `shape=line` the body is a list of locators somebody is about to pipe
            into wget or sort -u, so a control line arrives commented rather than as a
            JSON object in the middle of it -- still visible, still greppable, and
            filtered out entirely by `grep -v '^#'`. It is never dropped: a consumer that
            lost events has to be told even when it asked for the terse shape.
            """
            if selector.shape == LINE:
                self._emit("# " + _comment(kind, data), sse)
                return
            self._emit(json.dumps({"kind": kind, "data": data},
                                  ensure_ascii=False, default=str,
                                  separators=(",", ":")), sse)

        def _emit(self, payload: str, sse: bool, seq: Optional[int] = None) -> None:
            if sse:
                head = f"id: {seq}\n" if seq is not None else ""
                self._chunk(f"{head}data: {payload}\n\n".encode("utf-8"))
            else:
                self._chunk((payload + "\n").encode("utf-8"))

        def _chunk(self, payload: bytes) -> None:
            """One chunk of a chunked body, flushed immediately.

            Flushing per line is the whole product: a stream that arrives in 8 KB blocks
            is not something you can grep against a running crawl.
            """
            self.wfile.write(b"%X\r\n" % len(payload) + payload + b"\r\n")
            self.wfile.flush()

        # -- small responses ---------------------------------------------------
        def _json(self, code: int, payload: dict, headers=()) -> None:
            body = json.dumps(payload, indent=2, default=str).encode("utf-8") + b"\n"
            self._respond(code, "application/json", body, headers)

        def _text(self, code: int, text: str) -> None:
            self._respond(code, "text/plain; charset=utf-8", text.encode("utf-8"), ())

        def _respond(self, code: int, content_type: str, body: bytes, headers) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

    return Handler


# ---------------------------------------------------------------- control lines


def _hello(server: StreamServer, selector: Selector) -> dict:
    bus = server.bus
    return {"api": API_VERSION, "run": bus.run, "mode": bus.mode, "cursor": bus.seq,
            "published": bus.published, "filter": selector.describe(),
            "hint": "reconnect with ?since=<the seq of your last line>"}


def _gap(lost: int, resuming_at: Optional[int]) -> dict:
    return {"lost": lost, "resuming_at": resuming_at,
            "hint": "this consumer fell behind; the run's JSONL on disk is complete"}


def _heartbeat(bus: EventBus) -> dict:
    return {"seq": bus.seq, "published": bus.published,
            "uptime_s": round(time.time() - bus.started, 1)}


def _comment(kind: str, data: dict) -> str:
    """One control line for a consumer reading `shape=line`. Terse and human-first."""
    if kind == STREAM_GAP:
        return f"gap: {data['lost']} event(s) lost -- the run's JSONL on disk is complete"
    if kind == STREAM_HEARTBEAT:
        return f"heartbeat: seq {data['seq']}, {data['published']} published"
    return (f"{kind} run={data.get('run')} mode={data.get('mode')} "
            f"cursor={data.get('cursor')}")


# ---------------------------------------------------------------- addresses


def parse_bind(value: str) -> tuple[str, int]:
    """'host:port', '[::1]:port' or a bare port into (host, port)."""
    text = (value or "").strip()
    if not text:
        return parse_bind(DEFAULT_BIND)
    if text.isdigit():
        return "127.0.0.1", _port(text)
    if text.startswith("["):
        host, _, port = text.partition("]")
        return host[1:], _port(port.lstrip(":"))
    if text.count(":") > 1:
        # '::1' splits into host '::' and port '1' -- a wildcard bind on port 1, which
        # is not remotely what was asked for. An address with more than one colon in it
        # is IPv6 and has to say where the port stops.
        raise ValueError(f"not a bind address: {value!r} -- bracket an IPv6 address so "
                         f"the port can be told from it (try [::1]:8787)")
    host, _, port = text.rpartition(":")
    if not host or not port:
        raise ValueError(f"not a bind address: {value!r} (try 127.0.0.1:8787)")
    return host, _port(port)


def _port(text: str) -> int:
    if not text.isdigit() or not 0 <= int(text) <= 65535:
        raise ValueError(f"not a port: {text!r}")
    return int(text)


def _host_name(header: str) -> str:
    """The name out of a Host header, without its port and without its brackets.

    '[::1]:8787' is one address with three colons in it, so the port cannot be split off
    by counting them -- which is how an IPv6 stream would refuse every request it got.
    """
    text = (header or "").strip()
    if text.startswith("["):
        return text.partition("]")[0][1:]
    return text.rpartition(":")[0] if text.count(":") == 1 else text


def is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_wildcard(host: str) -> bool:
    """'0.0.0.0' or '::' -- a bind that answers on every address this machine has."""
    try:
        return ipaddress.ip_address(host).is_unspecified
    except ValueError:
        return False
