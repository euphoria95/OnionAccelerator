"""The lab: a content server and shaped SOCKS5 stubs, in a process of their own.

Why a separate process: the engine under test is Python, the stubs are Python, and one
interpreter shares one GIL. Sixteen lanes at 8 MiB/s each is 128 MiB/s of relaying; in the
same process the benchmark would measure the interpreter, not the engine.

What a stub models (all of it optional, all of it per stub, so one stub can be the slow lane):

    rate_mib       throughput cap per *connection*: one Tor circuit
    shared_mib     throughput cap shared by every connection through this stub: one daemon's
                   single-threaded crypto path (what made a farm worth building)
    rtt            mean added delay per request, plus
    jitter         an exponential tail on it (mean `jitter * rtt`): the long tail that makes
                   hedging worth anything
    connect_delay  delay before a connection is established: circuit and rendezvous setup
    fail_mean_mib  a connection is reset after an exponential number of downstream bytes with
                   this mean (0 = never): a circuit dying mid-piece

The content: a deterministic file (no block repeats, so a piece written to the wrong offset
changes its hash) and a deterministic tar of many small members, both generated on demand and
never held in memory, so a multi-GiB file costs nothing to serve.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import multiprocessing
import random
import socket
import socketserver
import struct
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

CHUNK = 64 * 1024
MIB = 1024 * 1024


# ----------------------------------------------------------------------------- content


class PatternSource:
    """`size` bytes of incompressible, never-repeating data, generated on demand."""

    BLOCK = MIB

    def __init__(self, size: int, seed: int = 1):
        self.size = size
        self._base = random.Random(seed).randbytes(self.BLOCK)
        self._cache: dict[int, bytes] = {}

    def _block(self, k: int) -> bytes:
        got = self._cache.get(k)
        if got is None:
            shift = (k * 7919) % self.BLOCK
            got = self._base[shift:] + self._base[:shift]
            if len(self._cache) > 64:
                self._cache.clear()
            self._cache[k] = got
        return got

    def read(self, start: int, length: int) -> bytes:
        end = min(start + length, self.size)
        out, pos = bytearray(), start
        while pos < end:
            k, within = divmod(pos, self.BLOCK)
            take = min(self.BLOCK - within, end - pos)
            out += self._block(k)[within:within + take]
            pos += take
        return bytes(out)

    def sha256(self) -> str:
        digest, pos = hashlib.sha256(), 0
        while pos < self.size:
            piece = self.read(pos, self.BLOCK)
            digest.update(piece)
            pos += len(piece)
        return digest.hexdigest()


class TarSource:
    """A tar of `members` small files, generated on demand: header, then zero-filled data.

    Offsets are computed up front (a few MB for 100,000 members) so a read anywhere is a
    bisect and a slice, which is what lets a lister seek around a "10 GB" archive for free.
    """

    def __init__(self, members: int, avg_size: int = 64 * 1024, seed: int = 2):
        rng = random.Random(seed)
        self.sizes = [int(rng.expovariate(1 / avg_size)) + 1 for _ in range(members)]
        self.starts, pos = [], 0
        for s in self.sizes:
            self.starts.append(pos)
            pos += 512 + ((s + 511) // 512) * 512
        self.size = pos + 1024
        self._headers: dict[int, bytes] = {}

    def _header(self, i: int) -> bytes:
        got = self._headers.get(i)
        if got is None:
            info = tarfile.TarInfo(f"d{i // 100:04d}/file{i:07d}.bin")
            info.size, info.mtime, info.mode = self.sizes[i], 1_700_000_000, 0o644
            got = info.tobuf(format=tarfile.USTAR_FORMAT)
            if len(self._headers) > 4096:
                self._headers.clear()
            self._headers[i] = got
        return got

    def read(self, start: int, length: int) -> bytes:
        end = min(start + length, self.size)
        out, pos = bytearray(), start
        while pos < end:
            i = bisect.bisect_right(self.starts, pos) - 1
            if i < 0 or i >= len(self.starts) or pos >= self.starts[i] + 512 + (
                    (self.sizes[i] + 511) // 512) * 512:
                out += b"\0" * (end - pos)                        # the end-of-archive blocks
                break
            rel = pos - self.starts[i]
            member_end = self.starts[i] + 512 + ((self.sizes[i] + 511) // 512) * 512
            take = min(member_end, end) - pos
            if rel < 512:
                out += self._header(i)[rel:rel + take] if rel + take <= 512 else (
                    self._header(i)[rel:] + b"\0" * (take - (512 - rel)))
            else:
                out += b"\0" * take
            pos += take
        return bytes(out[:length])


# ----------------------------------------------------------------------------- shaping


class Limiter:
    """A throughput limiter: take(n) sleeps until n more bytes are allowed."""

    def __init__(self, rate_bytes: float):
        self.rate = rate_bytes
        self._next = 0.0
        self._lock = threading.Lock()

    def take(self, n: int) -> None:
        if not self.rate:
            return
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + n / self.rate
            wait = start - now
        if wait > 0:
            time.sleep(wait)


class Stub:
    """A SOCKS5 proxy on loopback that shapes what passes through it."""

    def __init__(self, *, rate_mib: float = 0, shared_mib: float = 0, rtt: float = 0.0,
                 jitter: float = 0.0, connect_delay: float = 0.0, fail_mean_mib: float = 0,
                 seed: int = 0, mirror_to: Optional[int] = None):
        self.rate = rate_mib * MIB
        self.shared = Limiter(shared_mib * MIB)
        self.rtt, self.jitter, self.connect_delay = rtt, jitter, connect_delay
        self.fail_mean = fail_mean_mib * MIB
        self._rng = random.Random(seed)
        self._rng_lock = threading.Lock()
        # A mirror is the same shaping with no SOCKS in front: plain HTTP to a loopback port,
        # for the one comparison (aria2c) whose subject cannot use a SOCKS5 proxy.
        self.mirror_to = mirror_to
        self.connections = 0
        self.resets = 0
        self.credentials: set[str] = set()
        self._server: Optional[socketserver.ThreadingTCPServer] = None
        self.port = 0

    # the dice, under a lock: handlers are threads and a Random is not thread-safe
    def _roll_delay(self) -> float:
        if not self.rtt:
            return 0.0
        with self._rng_lock:
            return self.rtt * (1.0 + (self._rng.expovariate(1.0 / self.jitter) if self.jitter else 0))

    def _roll_death(self) -> Optional[float]:
        if not self.fail_mean:
            return None
        with self._rng_lock:
            return self._rng.expovariate(1.0 / self.fail_mean)

    def start(self) -> int:
        outer = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                try:
                    if outer.mirror_to:
                        outer._serve_mirror(self.request)
                    else:
                        outer._serve(self.request)
                except (OSError, ValueError, IndexError, struct.error):
                    pass

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True
            request_queue_size = 256

        self._server = Server(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self.port

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    # ------------------------------------------------------------------ SOCKS5
    @staticmethod
    def _recv(sock, n):
        buf = b""
        while len(buf) < n:
            more = sock.recv(n - len(buf))
            if not more:
                raise OSError("closed")
            buf += more
        return buf

    def _serve(self, sock: socket.socket) -> None:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        _version, n_methods = self._recv(sock, 2)
        methods = set(self._recv(sock, n_methods))
        if 0x02 in methods:
            sock.sendall(b"\x05\x02")
            self._recv(sock, 1)
            user = self._recv(sock, self._recv(sock, 1)[0]).decode()
            password = self._recv(sock, self._recv(sock, 1)[0]).decode()
            self.credentials.add(f"{user}:{password}")
            sock.sendall(b"\x01\x00")
        else:
            sock.sendall(b"\x05\x00")
        _v, _cmd, _rsv, atyp = self._recv(sock, 4)
        # The requested host is read off the wire and then ignored; see below.
        if atyp == 1:
            self._recv(sock, 4)
        elif atyp == 3:
            self._recv(sock, self._recv(sock, 1)[0])
        else:
            self._recv(sock, 16)
        port = struct.unpack("!H", self._recv(sock, 2))[0]
        self.connections += 1
        if self.connect_delay:
            time.sleep(self.connect_delay)                       # circuit + rendezvous setup
        # Whatever name the client asked for, the stub dials loopback: the lab's hostnames are
        # labels, not names, and a lookup of one would be a real DNS query to nowhere.
        upstream = socket.create_connection(("127.0.0.1", port), timeout=10)
        upstream.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.sendall(b"\x05\x00\x00\x01" + socket.inet_aton("0.0.0.0") + b"\x00\x00")
        self._relay(sock, upstream)

    def _serve_mirror(self, sock: socket.socket) -> None:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.connections += 1
        if self.connect_delay:
            time.sleep(self.connect_delay)
        upstream = socket.create_connection(("127.0.0.1", self.mirror_to), timeout=10)
        upstream.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._relay(sock, upstream)

    def _relay(self, client: socket.socket, upstream: socket.socket) -> None:
        dead = threading.Event()
        budget = self._roll_death()
        sent = [0]

        def up():                                               # client -> content server
            try:
                while not dead.is_set():
                    data = client.recv(CHUNK)
                    if not data:
                        break
                    delay = self._roll_delay()                   # one round trip per request
                    if delay:
                        time.sleep(delay)
                    upstream.sendall(data)
            except OSError:
                pass
            finally:
                try:
                    upstream.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        threading.Thread(target=up, daemon=True).start()
        try:
            while True:
                data = upstream.recv(CHUNK)
                if not data:
                    break
                if budget is not None and sent[0] + len(data) > budget:
                    cut = max(0, int(budget - sent[0]))
                    if cut:
                        client.sendall(data[:cut])
                    self.resets += 1
                    dead.set()
                    try:
                        client.shutdown(socket.SHUT_RDWR)       # a circuit that died mid-piece
                    except OSError:
                        pass
                    return
                self.shared.take(len(data))
                if self.rate:
                    time.sleep(len(data) / self.rate)            # this circuit's own ceiling
                client.sendall(data)
                sent[0] += len(data)
        except OSError:
            pass
        finally:
            dead.set()
            for s in (client, upstream):
                try:
                    s.close()
                except OSError:
                    pass


# ----------------------------------------------------------------------------- content server


class Content:
    """Serves the lab's files over HTTP with ranges, and counts what it served."""

    def __init__(self, sources: dict):
        self.sources = sources
        self.requests = 0
        self.bytes_sent = 0
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_HEAD(self):
                self._serve(head=True)

            def do_GET(self):
                self._serve(head=False)

            def _serve(self, head):
                path = self.path.split("?")[0]
                if path == "/__stats":
                    body = json.dumps(outer.stats()).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if path == "/__reset":
                    outer.reset()
                    self.send_response(204)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                source = outer.sources.get(path)
                if source is None:
                    self.send_error(404)
                    return
                size = source.size
                start, end, status = 0, size - 1, 200
                rng = self.headers.get("Range")
                if rng and rng.startswith("bytes="):
                    a, _, b = rng[6:].partition("-")
                    if a:
                        start = int(a)
                        end = min(int(b), size - 1) if b else size - 1
                        status = 206
                if status == 206 and (start >= size or end < start):
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                length = end - start + 1
                with outer.lock:
                    outer.requests += 1
                self.send_response(status)
                if status == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Content-Length", str(length))
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("ETag", '"bench-v1"')
                self.end_headers()
                if head:
                    return
                sent = 0
                try:
                    while sent < length:
                        chunk = source.read(start + sent, min(CHUNK, length - sent))
                        self.wfile.write(chunk)
                        sent += len(chunk)
                        with outer.lock:
                            outer.bytes_sent += len(chunk)
                except OSError:
                    pass

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            request_queue_size = 256

            def handle_error(self, request, client_address):
                pass

        self.httpd = Server(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]

    def stats(self) -> dict:
        with self.lock:
            return {"requests": self.requests, "bytes_sent": self.bytes_sent}

    def reset(self) -> None:
        with self.lock:
            self.requests = self.bytes_sent = 0


# ----------------------------------------------------------------------------- the process


def _serve(config: dict, conn) -> None:
    """Child process: build the lab, report its ports, run until told to stop."""
    sources = {}
    if config.get("file_mib"):
        sources["/file.bin"] = PatternSource(int(config["file_mib"] * MIB), seed=config.get("seed", 1))
    if config.get("tar_members"):
        sources["/archive.tar"] = TarSource(config["tar_members"], seed=config.get("seed", 2))
    content = Content(sources)
    threading.Thread(target=content.httpd.serve_forever, daemon=True).start()
    stubs = [Stub(**spec) for spec in config.get("stubs", [])]
    ports = [s.start() for s in stubs]
    info = {"content_port": content.port, "stub_ports": ports,
            "sizes": {k: v.size for k, v in sources.items()}}
    if config.get("mirrors"):
        mirrors = [Stub(**spec, mirror_to=content.port) for spec in config.get("stubs", [])]
        info["mirror_ports"] = [m.start() for m in mirrors]
        stubs += mirrors
    if "/file.bin" in sources and config.get("hash", True):
        info["sha256"] = sources["/file.bin"].sha256()
    conn.send(info)
    while True:
        msg = conn.recv()
        if msg == "stop":
            break
        if msg == "stubs":
            conn.send([{"connections": s.connections, "resets": s.resets,
                        "credentials": len(s.credentials)} for s in stubs])
    for s in stubs:
        s.stop()
    content.httpd.shutdown()


class Lab:
    """Context manager: start the lab process, expose its ports and counters."""

    def __init__(self, **config):
        self.config = config
        self._proc = None
        self._conn = None
        self.info: dict = {}

    def __enter__(self) -> "Lab":
        ctx = multiprocessing.get_context("spawn")
        self._conn, child = ctx.Pipe()
        self._proc = ctx.Process(target=_serve, args=(self.config, child), daemon=True)
        self._proc.start()
        if not self._conn.poll(120):
            raise RuntimeError("the lab process did not start")
        self.info = self._conn.recv()
        return self

    def __exit__(self, *exc) -> None:
        try:
            self._conn.send("stop")
        except OSError:
            pass
        self._proc.join(10)
        if self._proc.is_alive():
            self._proc.kill()

    @property
    def endpoints(self) -> list[str]:
        return [f"127.0.0.1:{p}" for p in self.info["stub_ports"]]

    @property
    def mirror_urls(self) -> list:
        return [f"http://127.0.0.1:{p}/file.bin" for p in self.info.get("mirror_ports", [])]

    def url(self, name: str = "file.bin") -> str:
        # The hostname is not resolved by anyone: socks5h hands it to the stub, which dials
        # the content server on loopback. It is a label, and it is not a real name.
        return f"http://bench.lab:{self.info['content_port']}/{name}"

    def direct_url(self, name: str = "file.bin") -> str:
        return f"http://127.0.0.1:{self.info['content_port']}/{name}"

    def stats(self) -> dict:
        import urllib.request
        with urllib.request.urlopen(self.direct_url("__stats"), timeout=10) as r:
            return json.load(r)

    def reset(self) -> None:
        import urllib.request
        urllib.request.urlopen(self.direct_url("__reset"), timeout=10).read()

    def stub_stats(self) -> list:
        self._conn.send("stubs")
        return self._conn.recv()
