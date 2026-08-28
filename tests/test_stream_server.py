"""The HTTP face of the stream, driven the way a consumer actually drives it.

Everything here runs against a real socket on a real ephemeral port, because most of
what could break is in the framing rather than in the logic: a chunked body that is not
flushed per line, a header that makes a proxy buffer the response, an error path that
sends no body at all. A unit test of the handler would miss all three.
"""

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from eventstream import EventBus, StreamServer
from eventstream.server import API_VERSION, is_loopback, parse_bind


@pytest.fixture
def bus():
    return EventBus("job-7", "crawl", buffer=100, queue=20)


@pytest.fixture
def server(bus):
    srv = StreamServer(bus, "127.0.0.1:0",
                       status=lambda: {"seeds": ["http://x.onion/"], "out_dir": "/tmp/c"})
    srv.start()
    yield srv
    srv.stop(0.5)


class Consumer(threading.Thread):
    """Attaches to /events and collects lines, the way curl -sN would."""

    def __init__(self, url, headers=None):
        super().__init__(daemon=True)
        self.url = url
        self.headers = headers or {}
        self.lines: list[str] = []
        self.error = None
        self.start()

    def run(self):
        try:
            request = urllib.request.Request(self.url, headers=self.headers)
            with urllib.request.urlopen(request) as response:
                self.status = response.status
                for raw in response:
                    self.lines.append(raw.decode("utf-8").rstrip("\n"))
        except Exception as exc:                                  # noqa: BLE001
            self.error = exc

    def wait_for(self, count, timeout=3.0):
        deadline = time.time() + timeout
        while len(self.lines) < count and time.time() < deadline:
            time.sleep(0.02)
        return self.lines

    def json(self):
        return [json.loads(line) for line in self.lines if line.startswith("{")]


def get(url, headers=None):
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request) as response:
        return response.status, json.loads(response.read())


def fails(url, headers=None):
    try:
        get(url, headers)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())
    raise AssertionError(f"{url} did not fail")


# ----------------------------------------------------------------- the stream


def test_a_consumer_is_told_where_it_is_before_anything_else(server, bus):
    bus("crawl.file", {"url": "before"})
    consumer = Consumer(f"{server.url}/events")
    hello = json.loads(consumer.wait_for(1)[0])
    assert hello["kind"] == "stream.hello"
    assert hello["data"]["cursor"] == 1
    assert hello["data"]["api"] == API_VERSION


def test_events_arrive_while_the_run_is_still_going(server, bus):
    consumer = Consumer(f"{server.url}/events")
    consumer.wait_for(1)
    bus("crawl.file", {"url": "http://x.onion/a.txt", "size_bytes": 4})
    lines = consumer.wait_for(2)
    assert json.loads(lines[1])["data"]["url"] == "http://x.onion/a.txt"


def test_each_line_is_flushed_on_its_own(server, bus):
    """The whole product is that a grep sees line one before line two exists."""
    consumer = Consumer(f"{server.url}/events")
    consumer.wait_for(1)
    bus("crawl.file", {"url": "first"})
    assert len(consumer.wait_for(2)) == 2
    bus("crawl.file", {"url": "second"})
    assert len(consumer.wait_for(3)) == 3


def test_a_reconnect_picks_up_from_the_sequence_number_it_had(server, bus):
    for i in range(5):
        bus("crawl.file", {"url": f"u{i}"})
    consumer = Consumer(f"{server.url}/events?since=2")
    payloads = [json.loads(line) for line in consumer.wait_for(4)]
    assert [p["data"]["url"] for p in payloads[1:]] == ["u2", "u3", "u4"]


def test_kinds_and_match_filter_before_anything_crosses_the_socket(server, bus):
    consumer = Consumer(f"{server.url}/events?kinds=crawl.file&match=(?i)payroll")
    consumer.wait_for(1)
    bus("crawl.file", {"url": "http://x.onion/holiday.jpg"})
    bus("crawl.dir", {"url": "http://x.onion/payroll/"})       # right word, wrong kind
    bus("crawl.file", {"url": "http://x.onion/payroll_2019.xlsx"})
    lines = consumer.wait_for(2)
    assert len(lines) == 2
    assert json.loads(lines[1])["data"]["url"].endswith("payroll_2019.xlsx")


def test_shape_line_is_something_you_can_pipe(server, bus):
    consumer = Consumer(f"{server.url}/events?shape=line&kinds=crawl.file")
    consumer.wait_for(1)
    bus("crawl.file", {"url": "http://x.onion/a.txt"})
    bus("crawl.progress", {"queued": 2})                        # names nothing: skipped
    lines = consumer.wait_for(2)
    assert lines[0].startswith("# ")                            # hello, commented
    assert lines[1] == "http://x.onion/a.txt"
    assert [line for line in lines if not line.startswith("#")] == ["http://x.onion/a.txt"]


def test_a_consumer_that_falls_behind_is_told_so_in_band(bus):
    """The one thing a keyword index must never do is quietly miss something."""
    tiny = EventBus("job", "crawl", queue=2)
    server = StreamServer(tiny, "127.0.0.1:0").start()
    try:
        consumer = Consumer(f"{server.url}/events?heartbeat=0")
        consumer.wait_for(1)
        time.sleep(0.2)
        for i in range(50):                                     # far past the queue depth
            tiny("crawl.file", {"url": f"u{i}"})
        gaps = [p for p in consumer.wait_for(3) if "stream.gap" in p]
        assert gaps, consumer.lines
        lost = json.loads(gaps[0])["data"]["lost"]
        assert lost > 0 and "on disk is complete" in json.loads(gaps[0])["data"]["hint"]
    finally:
        server.stop(0.5)


def test_an_idle_run_still_writes_something(server):
    """Both to keep a NAT open, and because a write is how a dead consumer is noticed."""
    consumer = Consumer(f"{server.url}/events?heartbeat=1")
    lines = consumer.wait_for(2, timeout=4.0)
    assert json.loads(lines[1])["kind"] == "stream.heartbeat"


def test_the_stream_ends_when_the_run_does(server, bus):
    consumer = Consumer(f"{server.url}/events")
    consumer.wait_for(1)
    bus("run.stop", {"stopped_because": "completed"})
    server.stop(1.0)
    consumer.join(3.0)
    assert not consumer.is_alive()
    assert json.loads(consumer.lines[-1])["kind"] == "run.stop"


def test_a_consumer_that_vanishes_is_not_left_subscribed(server, bus):
    """Its queue would otherwise fill with the rest of the run on nobody's behalf."""
    import socket as socket_mod
    from urllib.parse import urlsplit

    parts = urlsplit(server.url)
    for _ in range(5):
        sock = socket_mod.create_connection((parts.hostname, parts.port))
        sock.sendall(b"GET /events HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        sock.close()                                   # gone before reading a byte

    deadline = time.time() + 3.0
    while bus.subscribers and time.time() < deadline:
        bus("crawl.file", {"url": "u"})                # a write is how they are noticed
        time.sleep(0.05)
    assert bus.subscribers == 0


def test_a_finished_run_ends_the_body_instead_of_hanging_up(server, bus):
    """A completed run must not look like a broken pipe to the shell.

    An unterminated chunked body makes curl exit 18, the same code it gives for a
    connection that dropped -- so `--stream | grep` in a script with `set -e` would
    report every successful run as a failure.
    """
    import shutil
    import subprocess

    if not shutil.which("curl"):
        pytest.skip("needs curl to check the framing a real consumer sees")

    process = subprocess.Popen(["curl", "-sN", f"{server.url}/events"],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(0.5)
    bus("run.stop", {"stopped_because": "completed"})
    time.sleep(0.2)
    server.stop(1.0)
    out, _ = process.communicate(timeout=10)

    assert process.returncode == 0
    assert "run.stop" in out


def test_sse_is_available_for_anything_that_speaks_it(server, bus):
    consumer = Consumer(f"{server.url}/events",
                        headers={"Accept": "text/event-stream"})
    consumer.wait_for(1)
    bus("crawl.file", {"url": "http://x.onion/a"})
    lines = consumer.wait_for(5)
    assert any(line.startswith("data: ") for line in lines)
    assert any(line.startswith("id: ") for line in lines)


# ----------------------------------------------------------------- the small endpoints


def test_health_is_a_liveness_check(server):
    status, payload = get(f"{server.url}/health")
    assert status == 200 and payload == {"ok": True, "run": "job-7"}


def test_schema_documents_every_kind_the_run_can_publish(server):
    from eventstream.bus import KINDS

    _, payload = get(f"{server.url}/schema")
    assert payload["kinds"] == KINDS
    assert payload["envelope"] == ["seq", "ts", "run", "mode", "kind", "data"]


def test_status_carries_the_stream_and_the_run(server, bus):
    bus("crawl.file", {"url": "u"})
    _, payload = get(f"{server.url}/status")
    assert payload["stream"]["kinds"] == {"crawl.file": 1}
    assert payload["run"]["out_dir"] == "/tmp/c"


def test_status_answers_even_when_the_run_cannot_describe_itself(bus):
    def broken():
        raise RuntimeError("mid-collapse")

    server = StreamServer(bus, "127.0.0.1:0", status=broken).start()
    try:
        _, payload = get(f"{server.url}/status")
        assert "mid-collapse" in payload["run"]["error"]
    finally:
        server.stop(0.5)


def test_the_root_tells_a_human_what_to_type(server):
    with urllib.request.urlopen(f"{server.url}/") as response:
        body = response.read().decode()
    assert "/events" in body and "curl -sN" in body


def test_an_unknown_endpoint_says_what_does_exist(server):
    code, payload = fails(f"{server.url}/evens")
    assert code == 404 and "/events" in payload["detail"]


def test_a_bad_query_is_refused_with_the_reason(server):
    code, payload = fails(f"{server.url}/events?match=(")
    assert code == 400 and "regex" in payload["detail"]


# ----------------------------------------------------------------- access


def test_without_a_token_a_loopback_stream_is_simply_open(server):
    assert get(f"{server.url}/health")[0] == 200


def test_a_token_is_required_once_one_is_set(bus):
    server = StreamServer(bus, "127.0.0.1:0", token="s3cret").start()
    try:
        code, payload = fails(f"{server.url}/status")
        assert code == 401 and "Bearer" in payload["detail"]
        assert get(f"{server.url}/status",
                   headers={"Authorization": "Bearer s3cret"})[0] == 200
        assert get(f"{server.url}/status?token=s3cret")[0] == 200
        assert fails(f"{server.url}/status?token=wrong")[0] == 401
    finally:
        server.stop(0.5)


def test_binding_off_loopback_without_a_token_is_refused(bus):
    """The stream names the target and its files. Publishing it must be deliberate."""
    with pytest.raises(ValueError, match="refusing to bind"):
        StreamServer(bus, "0.0.0.0:8787")
    # With a token it is the operator's call, and allowed.
    StreamServer(bus, "0.0.0.0:8787", token="t")


def test_a_request_for_someone_else_s_host_is_refused(server):
    """A browser cannot forge Host, which is what stops a page rebinding onto the run."""
    code, _ = fails(f"{server.url}/health", headers={"Host": "evil.example.com"})
    assert code == 403
    assert get(f"{server.url}/health", headers={"Host": "localhost"})[0] == 200


# ----------------------------------------------------------------- addresses


@pytest.mark.parametrize("value,expected", [
    ("127.0.0.1:8787", ("127.0.0.1", 8787)),
    ("8787", ("127.0.0.1", 8787)),
    ("[::1]:9", ("::1", 9)),
    ("0.0.0.0:80", ("0.0.0.0", 80)),
    ("", ("127.0.0.1", 8787)),
])
def test_a_bind_can_be_written_the_obvious_ways(value, expected):
    assert parse_bind(value) == expected


@pytest.mark.parametrize("value", ["nonsense", "127.0.0.1:port", "127.0.0.1:99999"])
def test_a_bind_that_is_not_one_says_so(value):
    with pytest.raises(ValueError):
        parse_bind(value)


def test_loopback_is_recognised_however_it_is_spelled():
    assert all(is_loopback(h) for h in ("127.0.0.1", "127.0.0.5", "::1", "localhost"))
    assert not any(is_loopback(h) for h in ("0.0.0.0", "10.0.0.1", "example.com"))


def test_port_zero_picks_one_and_says_which(bus):
    server = StreamServer(bus, "127.0.0.1:0").start()
    try:
        assert server.port > 0 and str(server.port) in server.url
    finally:
        server.stop(0.5)


def test_waiting_for_a_consumer_returns_once_one_attaches(bus):
    server = StreamServer(bus, "127.0.0.1:0").start()
    try:
        assert not server.wait_for_consumer(0.05)
        Consumer(f"{server.url}/events")
        assert server.wait_for_consumer(3.0)
    finally:
        server.stop(0.5)
