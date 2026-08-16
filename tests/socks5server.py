"""A minimal SOCKS5 CONNECT server, standing in for a Tor daemon.

`tests/test_crawl_local.py` swaps the SOCKS connector out for a plain TCP one, which is
the right trade for exercising crawl logic quickly -- but it leaves the connector itself,
and the credential isolation the whole multi-circuit design rests on, untested. This
server speaks the same handshake Tor does and records which credential every connection
presented, so that claim can be asserted rather than assumed.

It implements only what the crawler uses: CONNECT, no-auth and username/password
(RFC 1929), IPv4 and domain-name targets.
"""

from __future__ import annotations

import socket
import socketserver
import struct
import threading
from collections import Counter
from typing import Optional


class Socks5Server:
    """A threaded SOCKS5 proxy on an ephemeral loopback port."""

    def __init__(self, *, require_auth: bool = True) -> None:
        self.require_auth = require_auth
        self.credentials: Counter[str] = Counter()
        self.targets: list[str] = []
        self._lock = threading.Lock()
        self._server: Optional[socketserver.ThreadingTCPServer] = None

    # ------------------------------------------------------------ lifecycle

    def __enter__(self) -> "Socks5Server":
        outer = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                try:
                    outer._serve(self.request)
                except (OSError, ValueError, AssertionError, IndexError):
                    # A bare TCP probe that never speaks SOCKS (the crawler's own
                    # liveness check does exactly this) lands here. Not an error.
                    pass

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self._server = Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    # ------------------------------------------------------------ accessors

    @property
    def address(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address[:2]
        return f"{host}:{port}"

    @property
    def distinct_credentials(self) -> int:
        with self._lock:
            return len(self.credentials)

    # ------------------------------------------------------------ protocol

    def _serve(self, sock: socket.socket) -> None:
        version, n_methods = sock.recv(2)
        assert version == 5, version
        methods = set(sock.recv(n_methods))

        if self.require_auth and 0x02 in methods:
            sock.sendall(b"\x05\x02")
            assert sock.recv(1) == b"\x01"
            user = sock.recv(sock.recv(1)[0]).decode()
            password = sock.recv(sock.recv(1)[0]).decode()
            with self._lock:
                self.credentials[f"{user}:{password}"] += 1
            sock.sendall(b"\x01\x00")
        else:
            sock.sendall(b"\x05\x00")
            with self._lock:
                self.credentials["<anonymous>"] += 1

        version, command, _, atyp = sock.recv(4)
        assert version == 5 and command == 1, (version, command)
        if atyp == 1:
            host = socket.inet_ntoa(sock.recv(4))
        elif atyp == 3:
            host = sock.recv(sock.recv(1)[0]).decode()
        else:
            host = socket.inet_ntop(socket.AF_INET6, sock.recv(16))
        port = struct.unpack("!H", sock.recv(2))[0]
        with self._lock:
            self.targets.append(f"{host}:{port}")

        try:
            upstream = socket.create_connection((host, port), timeout=10)
        except OSError:
            sock.sendall(b"\x05\x05\x00\x01" + b"\x00" * 6)   # connection refused
            return
        sock.sendall(b"\x05\x00\x00\x01" + socket.inet_aton("0.0.0.0") + b"\x00\x00")
        _relay(sock, upstream)


def _relay(a: socket.socket, b: socket.socket) -> None:
    def copy(src: socket.socket, dst: socket.socket) -> None:
        try:
            while True:
                chunk = src.recv(65536)
                if not chunk:
                    break
                dst.sendall(chunk)
        except OSError:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    forward = threading.Thread(target=copy, args=(a, b), daemon=True)
    forward.start()
    copy(b, a)
    forward.join(timeout=5)
    b.close()
