"""--farm-backend process: a farm of plain `tor -f` children, no root, no systemd.

Each instance `oaNN` is an ordinary tor process with its own DataDirectory under the
user's state directory, a pidfile, and the same port mapping as the systemd farm
(SocksPort 127.0.0.1:<base-port + NN>). Health is read from tor itself: every instance
has a ControlSocket with cookie authentication, and bootstrap progress and circuit counts
come from `GETINFO` over it (oacore/torcontrol.py) rather than from tailing a log -- a
log can say 100% about a tor whose control port says otherwise, and the farm used to
believe the log.

The control port is for bootstrap, health and telemetry. It is not a second rotation
mechanism: a fresh circuit in tree and crawl comes from a fresh SOCKS credential
(IsolateSOCKSAuth, on by default), which needs no help from here.

Layout, under $ONIONACCELERATOR_FARM_DIR or ~/.local/state/onionaccelerator/farm:

    .oa-farm              the farm's marker; a directory without it is not ours
    oaNN/.oa-instance     the instance's marker; destroy removes only marked ones
    oaNN/torrc            regenerated on every 'up'
    oaNN/data/            tor's DataDirectory
    oaNN/tor.pid  control.sock  control_auth_cookie  notices.log  tor.stderr

SAFETY, the same rule the systemd backend has and for the same reason. This host may have
a relay or a personal client of its own; nothing here may stop or delete it. So: an
instance is acted on only if its name passes assert_farm_name(), it is a real directory
(not a link) directly inside the state directory, and it carries our marker; a state
directory that exists and was not made by this program is refused, not adopted; and a pid
is signalled only after its command line shows it is `tor -f <our torrc>`, because a
pidfile outlives its process and pids are reused.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

from tqdm import tqdm

from .common import _port_is_open, logger
from .config import MAX_WORKERS
from .farm import (BOOTSTRAP_TIMEOUT, FARM_RE, FARM_RESERVED, FARM_RESTART_STAGGER,
                   FARM_TUNE_SOCKS_FLAGS, TOR_INSTANCE_PREFIX, FarmBackend, assert_farm_name,
                   farm_instance_name, farm_tune_of, torrc_apply_action, tune_block,
                   warn_if_tor_lacks_pow)
from .torcontrol import ControlError, Controller

STATE_ENV = "ONIONACCELERATOR_FARM_DIR"
TOR_ENV = "ONIONACCELERATOR_TOR"
STATE_MARKER = ".oa-farm"
INSTANCE_MARKER = ".oa-instance"
STOP_TIMEOUT = 10                  # seconds a stopping tor gets, at each escalation
# sun_path is 108 bytes on Linux and 104 on macOS; keep clear of both.
MAX_SOCKET_PATH = 100


class FarmError(RuntimeError):
    """The farm cannot do what was asked, and says why. Nothing was changed."""


def farm_state_dir(environ=None) -> str:
    env = os.environ if environ is None else environ
    explicit = env.get(STATE_ENV)
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    base = env.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "onionaccelerator", "farm")


def find_tor(environ=None):
    env = os.environ if environ is None else environ
    override = env.get(TOR_ENV)
    if override:
        return override if os.access(override, os.X_OK) else None
    return shutil.which("tor")


def render_process_torrc(name, port, inst_dir, tune="off", count=1) -> str:
    """The torrc for one process-backend instance.

    Every option below was checked against the current Tor manual (GENERAL OPTIONS:
    DataDirectory, PidFile, Log, ControlSocket, CookieAuthentication, CookieAuthFile,
    AvoidDiskWrites, SafeLogging; CLIENT OPTIONS: SocksPort, ClientOnly) and the control
    protocol's own spec. Nothing is added that was not looked up. Left out on purpose:
    RunAsDaemon (the default, 0: this program owns the child), ControlPort (a Unix socket
    needs no network exposure), and any isolation flag (IsolateSOCKSAuth is on by default
    and the isolation of every lane depends on it).
    """
    return (
        "# MANAGED BY OnionAccelerator (--farm, process backend). Regenerated on every "
        "'up'; edits are lost.\n"
        f"# Farm instance {name}: client-only SOCKS proxy, no relay descriptor published.\n"
        f"SocksPort 127.0.0.1:{port}{FARM_TUNE_SOCKS_FLAGS[tune]}\n"
        "ClientOnly 1\n"
        "AvoidDiskWrites 1\n"
        "SafeLogging 1\n"
        f"DataDirectory {inst_dir}/data\n"
        f"PidFile {inst_dir}/tor.pid\n"
        f"Log notice file {inst_dir}/notices.log\n"
        f"ControlSocket {inst_dir}/control.sock\n"
        "CookieAuthentication 1\n"
        f"CookieAuthFile {inst_dir}/control_auth_cookie\n"
    ) + tune_block(tune, count)


# ----------------------------------------------------------------------- processes


def _cmdline(pid):
    """What `pid` was started as, or None if it is gone, a zombie, or unreadable."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            text = fh.read().replace(b"\0", b" ").decode(errors="replace").strip()
        return text or None
    except OSError:
        pass
    if sys.platform == "darwin":                         # no /proc
        try:
            out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True,
                                 text=True, timeout=5).stdout.strip()
            return out or None
        except (OSError, subprocess.SubprocessError):
            return None
    return None


def _pid_is_ours(pid, torrc_path):
    """Is `pid` running `... -f <torrc_path>`? A recycled pid is somebody else's process."""
    command = _cmdline(pid)
    return bool(command) and torrc_path in command


def _read_pid(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def _write_private(path, text):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


class ProcessBackend(FarmBackend):
    name = "process"

    def __init__(self, state_dir=None, tor_bin=None):
        self.state_dir = os.path.abspath(state_dir) if state_dir else farm_state_dir()
        self._tor_bin = tor_bin
        self._children: dict[str, subprocess.Popen] = {}

    # ------------------------------------------------------------ preflight and paths
    def _tor(self):
        tor = self._tor_bin or find_tor()
        if not tor:
            raise FarmError(f"no tor binary found. Install tor (apt install tor, brew install "
                            f"tor) or point {TOR_ENV} at one.")
        return tor

    def preflight(self):
        if sys.platform not in ("linux", "darwin"):
            raise FarmError("--farm-backend process runs on Linux and macOS only.")
        tor = self._tor()
        warn_if_tor_lacks_pow(tor)
        return tor

    def instance_dir(self, name):
        """The directory for farm instance `name`; ValueError for anything not oaNN."""
        return os.path.join(self.state_dir, assert_farm_name(name))

    def _ensure_state(self):
        d = self.state_dir
        if os.path.isdir(d):
            if os.stat(d).st_uid != os.getuid():
                raise FarmError(f"{d} belongs to another user; refusing to use it.")
            if not os.path.exists(os.path.join(d, STATE_MARKER)):
                if os.listdir(d):
                    raise FarmError(
                        f"{d} exists and was not created by --farm (no {STATE_MARKER} "
                        f"marker); refusing to adopt it. Point {STATE_ENV} at an empty or "
                        f"new directory.")
                open(os.path.join(d, STATE_MARKER), "w").close()
            os.chmod(d, 0o700)
        else:
            os.makedirs(d, mode=0o700)
            os.chmod(d, 0o700)
            open(os.path.join(d, STATE_MARKER), "w").close()

    def _ensure_instance(self, inst):
        sock = os.path.join(inst, "control.sock")
        if len(sock.encode()) > MAX_SOCKET_PATH:
            raise FarmError(f"{sock} is too long for a Unix socket; set {STATE_ENV} to a "
                            f"shorter directory.")
        os.makedirs(os.path.join(inst, "data"), mode=0o700, exist_ok=True)
        os.chmod(inst, 0o700)
        marker = os.path.join(inst, INSTANCE_MARKER)
        if not os.path.exists(marker):
            open(marker, "w").close()

    def instances(self):
        """Names of the instances this farm made: oaNN, a real directory, our marker."""
        if not os.path.exists(os.path.join(self.state_dir, STATE_MARKER)):
            return []
        found = []
        for name in sorted(os.listdir(self.state_dir)):
            path = os.path.join(self.state_dir, name)
            if (FARM_RE.match(name) and name not in FARM_RESERVED and os.path.isdir(path)
                    and not os.path.islink(path)
                    and os.path.exists(os.path.join(path, INSTANCE_MARKER))):
                found.append(name)
        return found

    @staticmethod
    def _socks_port(inst):
        try:
            with open(os.path.join(inst, "torrc"), encoding="utf-8") as fh:
                match = re.search(r"^SocksPort 127\.0\.0\.1:(\d+)", fh.read(), re.M)
            return int(match.group(1)) if match else None
        except OSError:
            return None

    def _live_pid(self, inst):
        pid = _read_pid(os.path.join(inst, "tor.pid"))
        if pid and _pid_is_ours(pid, os.path.join(inst, "torrc")):
            return pid
        return None

    # ------------------------------------------------------------ one instance
    def _start(self, name, inst):
        tor = self._tor()
        torrc = os.path.join(inst, "torrc")
        check = subprocess.run([tor, "--verify-config", "-f", torrc], capture_output=True,
                               text=True, timeout=60)
        if check.returncode != 0:
            raise FarmError(f"[{name}] tor rejected its configuration: "
                            f"{(check.stderr or check.stdout).strip()[:300]}")
        log = open(os.path.join(inst, "tor.stderr"), "ab")
        try:
            # A session of its own: the farm outlives this command, the way the systemd
            # one does, and a Ctrl-C at the terminal does not reach it.
            self._children[name] = subprocess.Popen(
                [tor, "-f", torrc], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                start_new_session=True, close_fds=True)
        finally:
            log.close()

    def _gone(self, name, pid):
        """Has `pid` exited? Reaps it first if it is our own child, or it stays a zombie."""
        child = self._children.get(name)
        if child is not None and child.pid == pid:
            child.poll()
        else:
            try:
                os.waitpid(pid, os.WNOHANG)
            except (ChildProcessError, OSError):
                pass
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        return _cmdline(pid) is None                       # a zombie has no command line

    def _wait_gone(self, name, pid, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self._gone(name, pid):
                return True
            time.sleep(0.05)
        return self._gone(name, pid)

    def _stop(self, name, inst, pid):
        """Ask tor to exit over its control socket; escalate only to a verified pid."""
        torrc = os.path.join(inst, "torrc")
        try:
            with Controller.open(os.path.join(inst, "control.sock"),
                                 os.path.join(inst, "control_auth_cookie"), timeout=2) as ctl:
                ctl.shutdown()
        except ControlError:
            pass
        if self._wait_gone(name, pid, STOP_TIMEOUT):
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            if not _pid_is_ours(pid, torrc):
                return
            logger.warning(f"[{name}] still running; sending {sig.name}")
            try:
                os.kill(pid, sig)
            except OSError:
                return
            if self._wait_gone(name, pid, STOP_TIMEOUT):
                return

    def _phase(self, inst):
        """tor's own bootstrap phase, or None if its control socket cannot be reached."""
        sock, cookie = os.path.join(inst, "control.sock"), os.path.join(inst, "control_auth_cookie")
        if not (os.path.exists(sock) and os.path.exists(cookie)):
            return None
        try:
            with Controller.open(sock, cookie, timeout=2) as ctl:
                return ctl.bootstrap_phase()
        except ControlError:
            return None

    # ------------------------------------------------------------ the four actions
    def up(self, count, base_port, timeout=BOOTSTRAP_TIMEOUT, tune="off"):
        self.preflight()
        # Every port, before anything is created: one held by somebody else (this host's
        # other farm, say) must refuse the whole run and leave no directory behind.
        for i in range(count):
            inst = self.instance_dir(farm_instance_name(i))
            if self._live_pid(inst) is None and _port_is_open("127.0.0.1", base_port + i):
                raise FarmError(f"[{farm_instance_name(i)}] 127.0.0.1:{base_port + i} is "
                                f"already in use by something that is not this farm's "
                                f"instance. Pick another --base-port.")
        self._ensure_state()
        if count > MAX_WORKERS:
            logger.warning(f"count={count} exceeds MAX_WORKERS={MAX_WORKERS}; "
                           f"the speedtest will only probe the first {MAX_WORKERS}.")
        logger.info(f"Bringing up {count} Tor instance(s) on 127.0.0.1:{base_port}-"
                    f"{base_port + count - 1} (process backend, --farm-tune {tune}) "
                    f"under {self.state_dir}")
        names, restarted = [], 0
        for i in range(count):
            name = farm_instance_name(i)
            inst = self.instance_dir(name)
            port = base_port + i
            pid = self._live_pid(inst)
            self._ensure_instance(inst)
            torrc_path = os.path.join(inst, "torrc")
            try:
                with open(torrc_path, encoding="utf-8") as fh:
                    old = fh.read()
            except OSError:
                old = ""
            torrc = render_process_torrc(name, port, inst, tune=tune, count=count)
            _write_private(torrc_path, torrc)
            if pid is None:
                logger.info(f"[{name}] starting")
                self._start(name, inst)
            else:
                action = torrc_apply_action(old, torrc)
                if action == "restart":
                    if restarted:
                        time.sleep(FARM_RESTART_STAGGER)
                    restarted += 1
                    logger.info(f"[{name}] SocksPort changed; restarting")
                    self._stop(name, inst, pid)
                    self._start(name, inst)
                elif action == "reload":
                    logger.info(f"[{name}] torrc changed; reloading")
                    os.kill(pid, signal.SIGHUP)
                else:
                    logger.info(f"[{name}] already running")
            names.append(name)
        done = self.wait_bootstrap(names, timeout)
        logger.info("Farm is up. Run a download mode, e.g. "
                    "'python3 OnionAccelerator.py --mode multi'.")
        return done

    def wait_bootstrap(self, names, timeout=BOOTSTRAP_TIMEOUT):
        """Wait until tor reports PROGRESS=100 on every instance, or the time is up.

        A child that has already exited is reported at once -- a port someone else holds,
        a configuration tor refused -- rather than after the whole timeout.
        """
        logger.info(f"Waiting for {len(names)} instance(s) to bootstrap (timeout {timeout}s)...")
        deadline = time.monotonic() + timeout
        pending, done, dead = set(names), set(), {}
        with tqdm(total=len(names), desc="Bootstrapping", unit="inst") as bar:
            while pending and time.monotonic() < deadline:
                for name in sorted(pending):
                    inst = self.instance_dir(name)
                    child = self._children.get(name)
                    if child is not None and child.poll() is not None:
                        dead[name] = self._stderr_tail(inst)
                        pending.discard(name)
                        continue
                    phase = self._phase(inst)
                    port = self._socks_port(inst)
                    if (phase is not None and phase.progress >= 100 and port
                            and _port_is_open("127.0.0.1", port)):
                        pending.discard(name)
                        done.add(name)
                        bar.update(1)
                if pending:
                    time.sleep(0.25)
        for name, tail in sorted(dead.items()):
            logger.error(f"[{name}] tor exited during startup: {tail}")
        if pending:
            logger.warning(f"Timed out waiting on: {', '.join(sorted(pending))}. "
                           f"See their notices.log and tor.stderr under {self.state_dir}.")
        logger.info(f"{len(done)}/{len(names)} instance(s) bootstrapped and listening.")
        return done

    @staticmethod
    def _stderr_tail(inst):
        try:
            with open(os.path.join(inst, "tor.stderr"), "rb") as fh:
                return fh.read()[-300:].decode(errors="replace").strip() or "(no output)"
        except OSError:
            return "(no output)"

    def status(self, base_port):
        names = self.instances()
        if not names:
            logger.info("No farm instances found. Create some with '--farm up'.")
            return []
        logger.info(f"=== FARM STATUS ({len(names)} instance(s), process backend) ===")
        rows = []
        for name in names:
            inst = self.instance_dir(name)
            port = self._socks_port(inst)
            if port is None:
                port = base_port + int(name[len(TOR_INSTANCE_PREFIX):])
            pid = self._live_pid(inst)
            listening = _port_is_open("127.0.0.1", port)
            phase = self._phase(inst) if pid else None
            built = total = 0
            if phase is not None:
                try:
                    with Controller.open(os.path.join(inst, "control.sock"),
                                         os.path.join(inst, "control_auth_cookie"),
                                         timeout=2) as ctl:
                        circuits = ctl.circuits()
                    built, total = sum(c.status == "BUILT" for c in circuits), len(circuits)
                except ControlError:
                    pass
            try:
                with open(os.path.join(inst, "torrc"), encoding="utf-8") as fh:
                    tune = farm_tune_of(fh.read())
            except OSError:
                tune = "off"
            progress = phase.progress if phase is not None else 0
            row = {"name": name, "port": port, "pid": pid, "running": pid is not None,
                   "listening": listening, "bootstrap": progress,
                   "tag": phase.tag if phase is not None else "",
                   "circuits_built": built, "circuits": total, "tune": tune,
                   "ok": pid is not None and listening and progress >= 100}
            rows.append(row)
            logger.info(f"{'✅' if row['ok'] else '⚠️'} {name}: "
                        f"{'pid ' + str(pid) if pid else 'not running'}, "
                        f"socks=127.0.0.1:{port} ({'listening' if listening else 'down'}), "
                        f"bootstrap={progress}%"
                        f"{' (' + row['tag'] + ')' if row['tag'] else ''}, "
                        f"circuits={built} built/{total}, tune={tune}")
        return rows

    def down(self):
        names = self.instances()
        if not names:
            logger.info("No farm instances to stop.")
            return []
        stopped = []
        for name in names:
            inst = self.instance_dir(name)
            pid = self._live_pid(inst)
            if pid is None:
                continue
            logger.info(f"[{name}] stopping")
            self._stop(name, inst, pid)
            stopped.append(name)
        logger.info(f"Stopped {len(stopped)} instance(s). Configs/data kept "
                    f"(use '--farm destroy' to purge).")
        return stopped

    def destroy(self):
        names = self.instances()
        if not names:
            logger.info("No farm instances to destroy.")
            return []
        logger.warning(f"Destroying {len(names)} farm instance(s): {', '.join(names)}")
        self.down()
        removed = []
        for name in names:
            if self._remove_instance(name):
                removed.append(name)
                logger.info(f"[{name}] removed")
        # The state directory itself goes only when nothing but our marker is left in it.
        if os.listdir(self.state_dir) == [STATE_MARKER]:
            shutil.rmtree(self.state_dir)
        return removed

    def _remove_instance(self, name):
        """rmtree one instance, after every check that it is ours and where we think it is."""
        inst = self.instance_dir(name)
        root = os.path.realpath(self.state_dir)
        if (os.path.islink(inst) or not os.path.isdir(inst)
                or os.path.dirname(os.path.realpath(inst)) != root
                or not os.path.exists(os.path.join(inst, INSTANCE_MARKER))):
            logger.warning(f"[{name}] is not a directory this farm made; leaving it alone.")
            return False
        shutil.rmtree(inst)
        return True

    # ------------------------------------------------------------ supervising
    def run(self, count, base_port, timeout=BOOTSTRAP_TIMEOUT, tune="off"):
        """up, then stay in the foreground restarting what dies, until SIGTERM or Ctrl-C.

        For a container's PID 1 or a user service: the farm is exactly as long-lived as
        this process, and stops gracefully with it.
        """
        self.up(count, base_port, timeout=timeout, tune=tune)
        stop = threading.Event()
        previous = {sig: signal.signal(sig, lambda *_: stop.set())
                    for sig in (signal.SIGTERM, signal.SIGINT)}
        logger.info("Farm is up and supervised. SIGTERM or Ctrl-C stops it.")
        try:
            while not stop.wait(2.0):
                for name in self.instances():
                    inst = self.instance_dir(name)
                    child = self._children.get(name)
                    if child is not None:
                        child.poll()
                    if self._live_pid(inst) is None and os.path.exists(
                            os.path.join(inst, "torrc")):
                        logger.warning(f"[{name}] is not running; restarting it")
                        self._start(name, inst)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            self.down()


# ----------------------------------------------------------------------- telemetry


def circuit_counts(endpoints, state_dir=None):
    """{endpoint: {"built": n, "total": n}} for the endpoints that are this farm's.

    An endpoint that is not an instance of the process farm (a personal client on 9150, a
    systemd instance, an external proxy) has no control socket this program may open, and
    is simply absent from the answer.
    """
    backend = ProcessBackend(state_dir)
    by_endpoint = {}
    for name in backend.instances():
        inst = backend.instance_dir(name)
        port = backend._socks_port(inst)
        if port:
            by_endpoint[f"127.0.0.1:{port}"] = inst
    counts = {}
    for endpoint in endpoints:
        inst = by_endpoint.get(endpoint)
        if inst is None:
            continue
        try:
            with Controller.open(os.path.join(inst, "control.sock"),
                                 os.path.join(inst, "control_auth_cookie"), timeout=2) as ctl:
                circuits = ctl.circuits()
        except ControlError:
            continue
        counts[endpoint] = {"built": sum(c.status == "BUILT" for c in circuits),
                            "total": len(circuits)}
    return counts


def annotate_stats(path, endpoints, state_dir=None):
    """Add `tor_circuits` to a crawl's stats.json, if any endpoint has a control socket.

    Returns what it added. The crawler's own record is not touched beyond that one key,
    and the file is left exactly as it was when there is nothing to add.
    """
    counts = circuit_counts(endpoints, state_dir)
    if not counts:
        return {}
    with open(path, encoding="utf-8") as fh:
        stats = json.load(fh)
    stats["tor_circuits"] = counts
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(stats, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    return counts
