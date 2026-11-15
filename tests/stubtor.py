#!/usr/bin/env python3
"""A stand-in for the `tor` binary, so the process farm can be tested without Tor.

It does what the farm relies on and nothing else: parse a torrc, bind the SocksPort,
write the PidFile and the cookie, listen on the ControlSocket and answer the handful of
commands oacore.torcontrol speaks, with the reply formats from the control-spec
("Commands" and "Replies" pages of spec.torproject.org/control-spec). It never opens a
connection to anything but its own loopback listeners.

It is strict on purpose. `--verify-config` and a normal start both refuse a torrc that
uses an option outside KNOWN, which is exactly the set checked against the current Tor
manual (GENERAL OPTIONS, CLIENT OPTIONS, DORMANT MODE OPTIONS) -- so an option typo or an
invented one fails the offline suite instead of failing against a real tor.

Behaviour is steered by the environment, so one executable serves every test:

  STUB_TOR_BOOTSTRAP_SECONDS  seconds from start to 100% (default 0.3)
  STUB_TOR_STALL_AT           never get past this percentage
  STUB_TOR_LIE_IN_LOG         write "Bootstrapped 100%" to the log whatever the control
                              port says -- for proving health comes from tor itself
  STUB_TOR_CIRCUITS           built circuits to report once bootstrapped (default 3)
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import socket
import sys
import threading
import time

# The options this farm may emit, each verified against the current Tor manual:
#   GENERAL OPTIONS:  ControlSocket, CookieAuthentication, CookieAuthFile, DataDirectory,
#                     PidFile, Log, AvoidDiskWrites, SafeLogging
#   CLIENT OPTIONS:   SocksPort, ClientOnly
#   DORMANT MODE:     DormantClientTimeout, DormantCanceledByStartup
#   the --farm-tune profiles: CircuitsAvailableTimeout, MaxMemInQueues, MaxCircuitDirtiness
KNOWN = {
    "controlsocket", "cookieauthentication", "cookieauthfile", "datadirectory", "pidfile",
    "log", "avoiddiskwrites", "safelogging", "socksport", "clientonly",
    "dormantclienttimeout", "dormantcanceledbystartup", "circuitsavailabletimeout",
    "maxmeminqueues", "maxcircuitdirtiness",
}

PHASES = [  # (progress, tag, summary), as tor reports them
    (5, "conn_dir", "Connecting to directory server"),
    (10, "handshake_dir", "Finishing handshake with directory server"),
    (40, "loading_status", "Loading networkstatus consensus"),
    (80, "ap_conn", "Connecting to a relay to build circuits"),
    (100, "done", "Done"),
]


def parse_torrc(path):
    options = {}
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition(" ")
            options.setdefault(key.lower(), []).append(value.strip())
    unknown = sorted(set(options) - KNOWN)
    return options, unknown


class Stub:
    def __init__(self, torrc_path):
        self.torrc_path = torrc_path
        self.options, _ = parse_torrc(torrc_path)
        self.progress = 0
        self.reloads = 0
        self.newnyms = 0
        self.stop = threading.Event()
        self.cookie = secrets.token_bytes(32)
        self.lock = threading.Lock()
        data = self.options.get("datadirectory", [None])[0]
        self.state_path = os.path.join(data, "stub-state.json") if data else None

    def first(self, key):
        return (self.options.get(key) or [None])[0]

    def save_state(self):
        if self.state_path:
            with open(self.state_path, "w", encoding="utf-8") as fh:
                json.dump({"reloads": self.reloads, "newnyms": self.newnyms,
                           "progress": self.progress}, fh)

    def log(self, text):
        spec = self.first("log") or ""
        parts = spec.split(None, 2)
        if len(parts) == 3 and parts[1] == "file":
            with open(parts[2], "a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%b %d %H:%M:%S.000')} [notice] {text}\n")

    # ------------------------------------------------------------ bootstrap
    def bootstrap(self):
        seconds = float(os.environ.get("STUB_TOR_BOOTSTRAP_SECONDS", "0.3"))
        stall = int(os.environ.get("STUB_TOR_STALL_AT", "0") or 0)
        lie = bool(os.environ.get("STUB_TOR_LIE_IN_LOG"))
        for progress, tag, summary in PHASES:
            if stall and progress > stall:
                if lie:
                    self.log("Bootstrapped 100% (done): Done")
                return
            time.sleep(seconds / len(PHASES))
            with self.lock:
                self.progress = progress
            self.log(f"Bootstrapped {progress}% ({tag}): {summary}")
            self.save_state()

    # ------------------------------------------------------------ control port
    def phase(self):
        with self.lock:
            progress = self.progress
        tag, summary = next((t, s) for p, t, s in PHASES if p >= progress) if progress else (
            "starting", "Starting")
        return progress, tag, summary

    def circuit_lines(self):
        if self.phase()[0] < 100:
            return []
        built = int(os.environ.get("STUB_TOR_CIRCUITS", "3"))
        lines = [f"{i} BUILT $AAAA~guard,$BBBB~mid,$CCCC~exit BUILD_FLAGS=NEED_CAPACITY "
                 f"PURPOSE=GENERAL TIME_CREATED=2026-01-01T00:00:00.000000" for i in range(1, built + 1)]
        lines.append(f"{built + 1} LAUNCHED PURPOSE=GENERAL")
        return lines

    def serve_control(self, conn):
        authed = False
        reader = conn.makefile("rb")

        def send(text):
            conn.sendall(text.encode() + b"\r\n")

        try:
            for raw in reader:
                line = raw.decode().strip()
                if not line:
                    continue
                cmd, _, arg = line.partition(" ")
                cmd = cmd.upper()
                if cmd == "AUTHENTICATE":
                    if arg.strip().lower() == self.cookie.hex():
                        authed = True
                        send("250 OK")
                    else:
                        send("515 Bad authentication")
                        return
                elif cmd == "QUIT":
                    send("250 closing connection")
                    return
                elif not authed:
                    send("514 Authentication required.")
                    return
                elif cmd == "GETINFO":
                    self.getinfo(arg, send)
                elif cmd == "SIGNAL":
                    sig = arg.strip().upper()
                    if sig == "NEWNYM":
                        self.newnyms += 1
                        self.save_state()
                        send("250 OK")
                    elif sig == "SHUTDOWN":
                        send("250 OK")
                        self.stop.set()
                        return
                    else:
                        send("552 Unrecognized signal")
                else:
                    send(f"510 Unrecognized command \"{cmd}\"")
        except OSError:
            pass
        finally:
            conn.close()

    def getinfo(self, keys, send):
        out = []
        for key in keys.split():
            if key == "status/bootstrap-phase":
                p, tag, summary = self.phase()
                out.append(f'250-{key}=NOTICE BOOTSTRAP PROGRESS={p} TAG={tag} SUMMARY="{summary}"')
            elif key == "circuit-status":
                out.append(f"250+{key}=")
                out.extend(self.circuit_lines())
                out.append(".")
            else:
                send(f"552 Unrecognized key \"{key}\"")
                return
        for line in out:
            send(line)
        send("250 OK")


def main(argv):
    if "--version" in argv:
        print("Tor version 0.4.8.12 (stub).")
        return 0
    if "--list-modules" in argv:
        print("relay: yes\ndirauth: yes\ndirclient: yes\npow: yes")
        return 0
    if "-f" not in argv:
        print("stubtor: only -f TORRC is supported", file=sys.stderr)
        return 2
    path = argv[argv.index("-f") + 1]
    try:
        options, unknown = parse_torrc(path)
    except OSError as exc:
        print(f"[err] Unable to open configuration file: {exc}", file=sys.stderr)
        return 1
    if unknown:
        print(f"[warn] Unknown option '{unknown[0]}'.  Failing.", file=sys.stderr)
        return 1
    if "--verify-config" in argv:
        print("Configuration was valid")
        return 0

    stub = Stub(path)
    data = stub.first("datadirectory")
    if not data:
        print("[err] DataDirectory is required by this stub", file=sys.stderr)
        return 1
    os.makedirs(data, mode=0o700, exist_ok=True)

    socks = stub.first("socksport").split()[0]
    host, _, port = socks.rpartition(":")
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind((host or "127.0.0.1", int(port)))
    except OSError as exc:
        print(f"[err] Could not bind to {socks}: {exc}", file=sys.stderr)
        return 1
    listener.listen(16)
    threading.Thread(target=lambda: _accept_and_close(listener), daemon=True).start()

    cookie_path = stub.first("cookieauthfile")
    if cookie_path and (stub.first("cookieauthentication") == "1"):
        fd = os.open(cookie_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(stub.cookie)
    ctl_path = stub.first("controlsocket")
    control = None
    if ctl_path:
        if os.path.exists(ctl_path):
            os.unlink(ctl_path)
        control = socket.socket(socket.AF_UNIX)
        control.bind(ctl_path)
        os.chmod(ctl_path, 0o600)
        control.listen(8)
        threading.Thread(target=lambda: _accept_control(control, stub), daemon=True).start()

    pid_path = stub.first("pidfile")
    if pid_path:
        with open(pid_path, "w", encoding="utf-8") as fh:
            fh.write(f"{os.getpid()}\n")

    def cleanup(*_):
        stub.stop.set()

    def reload(*_):
        stub.reloads += 1
        stub.options, _ = parse_torrc(path)
        stub.save_state()

    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGHUP, reload)
    stub.log("Tor 0.4.8.12 (stub) running")
    threading.Thread(target=stub.bootstrap, daemon=True).start()
    stub.stop.wait()
    for p in (pid_path, ctl_path):
        if p and os.path.exists(p):
            os.unlink(p)
    return 0


def _accept_and_close(listener):
    while True:
        try:
            conn, _ = listener.accept()
        except OSError:
            return
        conn.close()


def _accept_control(control, stub):
    while True:
        try:
            conn, _ = control.accept()
        except OSError:
            return
        threading.Thread(target=stub.serve_control, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
