"""A control-port client that speaks only what the farm needs.

Tor's control protocol is large; the farm reads health and asks for two things, so this
is the handful of commands it uses and nothing more:

    AUTHENTICATE <cookie in hex>        control-spec, "Commands" -> AUTHENTICATE
    GETINFO status/bootstrap-phase      control-spec, "Commands" -> GETINFO
    GETINFO circuit-status
    SIGNAL NEWNYM / SIGNAL SHUTDOWN     control-spec, "Commands" -> SIGNAL

and the reply grammar from "Replies": `250-key=value` mid-lines, `250+key=` then data
lines ended by a lone `.`, and a final `250 OK` -- anything else is an error code. It
connects to a Unix ControlSocket (a TCP ControlPort would put a credential-bearing
socket on the network for no benefit) and authenticates with the cookie file, so access
is whatever the filesystem says: the socket and cookie are mode 0600 in a 0700 directory.

Standard library only, like eventstream/ and fullhelp.py: the farm must work on a host
that has installed nothing.
"""

from __future__ import annotations

import dataclasses
import re
import socket


class ControlError(Exception):
    """Tor refused a command, or could not be reached. `code` is its reply code, 0 if none."""

    def __init__(self, code: int, message: str):
        super().__init__(f"{code} {message}".strip())
        self.code, self.message = code, message


@dataclasses.dataclass
class BootstrapPhase:
    progress: int
    tag: str
    summary: str
    severity: str


@dataclasses.dataclass
class Circuit:
    id: str
    status: str
    path: list
    fields: dict


_ARG = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|\S*)')


def _args(text: str) -> dict:
    """KEY=value pairs, with a quoted value unquoted."""
    out = {}
    for key, value in _ARG.findall(text):
        out[key] = value[1:-1].replace('\\"', '"') if value.startswith('"') else value
    return out


class Controller:
    """One authenticated control connection. Use `Controller.open`, as a context manager."""

    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._reader = sock.makefile("rb")

    @classmethod
    def open(cls, socket_path: str, cookie_path: str, timeout: float = 5.0) -> "Controller":
        try:
            with open(cookie_path, "rb") as fh:
                cookie = fh.read()
        except OSError as exc:
            raise ControlError(0, f"cannot read the cookie: {exc}") from exc
        sock = socket.socket(socket.AF_UNIX)
        sock.settimeout(timeout)
        try:
            sock.connect(socket_path)
        except OSError as exc:
            sock.close()
            raise ControlError(0, f"cannot connect to {socket_path}: {exc}") from exc
        controller = cls(sock)
        try:
            controller._request(f"AUTHENTICATE {cookie.hex()}")
        except BaseException:
            controller.close()
            raise
        return controller

    def __enter__(self) -> "Controller":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        for closer in (self._reader.close, self._sock.close):
            try:
                closer()
            except OSError:
                pass

    # ------------------------------------------------------------ protocol
    def _request(self, command: str) -> list[str]:
        """Send one command; return its entries, or raise ControlError on a non-250 reply."""
        try:
            self._sock.sendall(command.encode() + b"\r\n")
            return self._read_reply()
        except (OSError, socket.timeout) as exc:
            raise ControlError(0, f"control connection failed: {exc}") from exc

    def _read_reply(self) -> list[str]:
        entries: list[str] = []
        while True:
            raw = self._reader.readline()
            if not raw:
                raise ControlError(0, "tor closed the control connection")
            line = raw.decode(errors="replace").rstrip("\r\n")
            if len(line) < 4 or not line[:3].isdigit():
                raise ControlError(0, f"unparseable reply: {line!r}")
            code, marker, text = int(line[:3]), line[3], line[4:]
            if marker == "+":
                data = []
                while True:
                    raw_more = self._reader.readline()
                    if not raw_more:
                        raise ControlError(0, "tor closed the control connection mid-reply")
                    more = raw_more.decode(errors="replace").rstrip("\r\n")
                    if more == ".":
                        break
                    data.append(more[1:] if more.startswith("..") else more)
                entries.append(text + "\n".join(data))
            elif marker == "-":
                entries.append(text)
            else:                                    # " ": the last line of the reply
                if code // 100 != 2:
                    raise ControlError(code, text)
                return entries

    # ------------------------------------------------------------ commands
    def getinfo(self, key: str) -> str:
        for entry in self._request(f"GETINFO {key}"):
            name, _, value = entry.partition("=")
            if name == key:
                return value
        raise ControlError(0, f"GETINFO {key}: no value in the reply")

    def bootstrap_phase(self) -> BootstrapPhase:
        """`NOTICE BOOTSTRAP PROGRESS=100 TAG=done SUMMARY="Done"`, as a record."""
        line = self.getinfo("status/bootstrap-phase")
        severity = line.split(None, 1)[0] if line else ""
        fields = _args(line)
        return BootstrapPhase(int(fields.get("PROGRESS", 0)), fields.get("TAG", ""),
                              fields.get("SUMMARY", ""), severity)

    def circuits(self) -> list[Circuit]:
        """One record per line of `circuit-status`: id, status, path, and the KEY=values."""
        out = []
        for line in self.getinfo("circuit-status").splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            path = parts[2].split(",") if len(parts) > 2 and "=" not in parts[2] else []
            out.append(Circuit(parts[0], parts[1], path, _args(line)))
        return out

    def newnym(self) -> None:
        self._request("SIGNAL NEWNYM")

    def shutdown(self) -> None:
        """Ask tor to exit. It may close the socket without a reply, which is success."""
        try:
            self._request("SIGNAL SHUTDOWN")
        except ControlError as exc:
            if exc.code != 0:
                raise
