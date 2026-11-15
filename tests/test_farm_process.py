"""The rootless farm: plain `tor -f` children, health read from Tor's control port.

Everything runs against tests/stubtor.py, a stand-in for the tor binary that binds the
SocksPort and speaks the control commands oacore.torcontrol uses. No test here runs tor,
systemctl or tor-instance-create, and none touches an instance this farm did not create:
the state directory is a temp directory, and the suite builds decoys named like this
host's real instances (relay2, torclient, default) to prove they are left alone.

Written before the process backend, from the Phase 2 acceptance list in docs/ROADMAP.md:

  * `--farm up --count 4` succeeds as a non-root user and discovery finds all four ports
  * `--farm destroy` removes only the farm's own state directory
  * the systemd backend's behaviour and tests are unchanged
"""

import json
import os
import random
import signal
import socket
import subprocess
import sys
import textwrap
import time

import pytest

import OnionAccelerator as oa
from oacore import farm, farm_process, torcontrol

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STUB = os.path.join(HERE, "stubtor.py")


def free_block(n):
    """The first of `n` consecutive loopback ports that are all free right now."""
    for _ in range(500):
        base = random.randint(20000, 60000 - n)
        held = []
        try:
            for i in range(n):
                s = socket.socket()
                s.bind(("127.0.0.1", base + i))
                held.append(s)
            return base
        except OSError:
            continue
        finally:
            for s in held:
                s.close()
    raise RuntimeError("no free port block")


def port_open(port):
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


@pytest.fixture
def env(monkeypatch, tmp_path_factory):
    """A private state directory and the stub as `tor`; stray children killed afterwards."""
    state = str(tmp_path_factory.mktemp("farm") / "state")
    monkeypatch.setenv("ONIONACCELERATOR_FARM_DIR", state)
    monkeypatch.setenv("ONIONACCELERATOR_TOR", STUB)
    monkeypatch.setenv("STUB_TOR_BOOTSTRAP_SECONDS", "0.2")
    for var in ("STUB_TOR_STALL_AT", "STUB_TOR_LIE_IN_LOG", "STUB_TOR_CIRCUITS"):
        monkeypatch.delenv(var, raising=False)
    yield state
    for pidfile in _pidfiles(state):
        try:
            os.kill(int(open(pidfile).read()), signal.SIGKILL)
        except (OSError, ValueError):
            pass


def _pidfiles(state):
    out = []
    if os.path.isdir(state):
        for name in os.listdir(state):
            path = os.path.join(state, name, "tor.pid")
            if os.path.exists(path):
                out.append(path)
    return out


def pid_of(state, name):
    return int(open(os.path.join(state, name, "tor.pid")).read())


def alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def wait_dead(pid, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            os.waitpid(pid, os.WNOHANG)       # a stub we started and nobody reaped
        except ChildProcessError:
            pass
        if not alive(pid):
            return True
        time.sleep(0.05)
    return not alive(pid)


@pytest.fixture
def backend(env):
    return farm_process.ProcessBackend()


# ----------------------------------------------------------------- the torrc


@pytest.mark.parametrize("tune", ["off", "onion", "onion-aggressive"])
def test_every_option_the_process_torrc_emits_is_one_the_manual_documents(tmp_path, tune):
    """The stub refuses any option outside the set checked against the Tor manual."""
    torrc = tmp_path / "torrc"
    torrc.write_text(farm_process.render_process_torrc(
        "oa03", 5003, str(tmp_path / "oa03"), tune=tune, count=4))
    done = subprocess.run([STUB, "--verify-config", "-f", str(torrc)], capture_output=True,
                          text=True)
    assert done.returncode == 0, done.stderr


def test_the_stub_really_does_reject_an_invented_option(tmp_path):
    """Otherwise the test above proves nothing."""
    torrc = tmp_path / "torrc"
    torrc.write_text("SocksPort 127.0.0.1:1\nMakeItFaster 1\n")
    assert subprocess.run([STUB, "--verify-config", "-f", str(torrc)],
                          capture_output=True).returncode == 1


def test_the_process_torrc_is_client_only_isolated_and_controllable(tmp_path):
    inst = str(tmp_path / "oa03")
    torrc = farm_process.render_process_torrc("oa03", 5003, inst)
    lines = torrc.splitlines()
    assert "SocksPort 127.0.0.1:5003" in lines
    assert "ClientOnly 1" in lines and "SafeLogging 1" in lines
    assert f"DataDirectory {inst}/data" in lines
    assert f"PidFile {inst}/tor.pid" in lines
    assert f"ControlSocket {inst}/control.sock" in lines
    assert "CookieAuthentication 1" in lines
    assert f"CookieAuthFile {inst}/control_auth_cookie" in lines
    # Foreground, owned by this program: no daemonizing, and no TCP control port.
    assert not any(l.startswith(("RunAsDaemon", "ControlPort")) for l in lines)
    # SOCKS auth isolation is tor's default; the torrc must not switch it off.
    assert "NoIsolateSOCKSAuth" not in torrc


def test_tune_profiles_add_the_same_lines_to_both_backends():
    for tune in ("onion", "onion-aggressive"):
        process = farm_process.render_process_torrc("oa00", 5000, "/x/oa00", tune=tune, count=4)
        systemd = farm.render_torrc("oa00", 5000, tune=tune, count=4)
        for line in systemd.splitlines():
            if line.startswith(("#", "SocksPort", "Log ")) or line in (
                    "ClientOnly 1", "AvoidDiskWrites 1", "SafeLogging 1"):
                continue
            assert line in process.splitlines(), line


# ----------------------------------------------------------------- the control client


def test_the_control_client_reads_bootstrap_and_circuits_from_tor(backend, env):
    base = free_block(1)
    backend.up(count=1, base_port=base, timeout=20)
    inst = os.path.join(env, "oa00")
    with torcontrol.Controller.open(os.path.join(inst, "control.sock"),
                                    os.path.join(inst, "control_auth_cookie")) as ctl:
        phase = ctl.bootstrap_phase()
        assert (phase.progress, phase.tag, phase.severity) == (100, "done", "NOTICE")
        circuits = ctl.circuits()
        assert [c.status for c in circuits].count("BUILT") == 3
        built = next(c for c in circuits if c.status == "BUILT")
        assert built.path == ["$AAAA~guard", "$BBBB~mid", "$CCCC~exit"]
        assert built.fields["PURPOSE"] == "GENERAL"
        assert ctl.getinfo("status/bootstrap-phase").startswith("NOTICE BOOTSTRAP")


def test_newnym_and_unknown_requests_are_answered_with_tors_codes(backend, env):
    backend.up(count=1, base_port=free_block(1), timeout=20)
    inst = os.path.join(env, "oa00")
    sock, cookie = os.path.join(inst, "control.sock"), os.path.join(inst, "control_auth_cookie")
    with torcontrol.Controller.open(sock, cookie) as ctl:
        ctl.newnym()
        with pytest.raises(torcontrol.ControlError) as unknown:
            ctl.getinfo("not/a/key")
        assert unknown.value.code == 552
    assert json.load(open(os.path.join(inst, "data", "stub-state.json")))["newnyms"] == 1


def test_a_wrong_cookie_is_refused_and_a_bare_connection_gets_nothing(backend, env):
    backend.up(count=1, base_port=free_block(1), timeout=20)
    inst = os.path.join(env, "oa00")
    bad = os.path.join(env, "bad_cookie")
    open(bad, "wb").write(b"\x00" * 32)
    with pytest.raises(torcontrol.ControlError) as refused:
        torcontrol.Controller.open(os.path.join(inst, "control.sock"), bad)
    assert refused.value.code == 515
    raw = socket.socket(socket.AF_UNIX)
    raw.connect(os.path.join(inst, "control.sock"))
    raw.sendall(b"GETINFO status/bootstrap-phase\r\n")
    assert raw.recv(100).startswith(b"514")
    raw.close()


def test_the_control_socket_and_cookie_are_private_to_the_user(backend, env):
    backend.up(count=1, base_port=free_block(1), timeout=20)
    inst = os.path.join(env, "oa00")
    for path in (os.path.join(inst, "control.sock"), os.path.join(inst, "control_auth_cookie"),
                 env):
        assert os.stat(path).st_mode & 0o077 == 0, path


# ----------------------------------------------------------------- up / status / discovery


def test_four_instances_come_up_without_root_and_discovery_finds_all_four(backend, env):
    assert os.geteuid() != 0, "this test is about running as an ordinary user"
    base = free_block(4)
    done = backend.up(count=4, base_port=base, timeout=30)

    assert set(done) == {"oa00", "oa01", "oa02", "oa03"}
    assert [port_open(base + i) for i in range(4)] == [True] * 4
    assert oa.discover_local_socks_ports(base_port=base, max_ports=4) == [
        f"127.0.0.1:{base + i}" for i in range(4)]


def test_status_reads_bootstrap_and_circuit_counts_from_tor_itself(backend, env):
    base = free_block(2)
    backend.up(count=2, base_port=base, timeout=20)
    rows = backend.status(base)
    assert [r["name"] for r in rows] == ["oa00", "oa01"]
    for i, row in enumerate(rows):
        assert row["port"] == base + i and row["running"] and row["listening"]
        assert row["bootstrap"] == 100 and row["circuits_built"] == 3 and row["ok"]
        assert row["tune"] == "off"


def test_health_comes_from_the_control_port_not_the_log(backend, env, monkeypatch, caplog):
    """tor says 60%; its log says 100%. Believing the log is the bug this replaces."""
    monkeypatch.setenv("STUB_TOR_STALL_AT", "40")
    monkeypatch.setenv("STUB_TOR_LIE_IN_LOG", "1")
    base = free_block(1)
    done = backend.up(count=1, base_port=base, timeout=3)

    assert done == set(), "an instance stuck at 40% was reported as up"
    row = backend.status(base)[0]
    assert row["bootstrap"] == 40 and not row["ok"]
    assert "Bootstrapped 100%" in open(os.path.join(env, "oa00", "notices.log")).read()


def test_an_instance_that_is_already_running_is_left_alone_by_a_second_up(backend, env):
    base = free_block(2)
    backend.up(count=2, base_port=base, timeout=20)
    pids = {n: pid_of(env, n) for n in ("oa00", "oa01")}
    backend.up(count=2, base_port=base, timeout=20)
    assert {n: pid_of(env, n) for n in pids} == pids


def test_a_changed_torrc_is_reloaded_in_place_and_a_changed_socks_line_restarts(backend, env):
    base = free_block(1)
    backend.up(count=1, base_port=base, timeout=20, tune="off")
    first = pid_of(env, "oa00")

    # --farm-tune onion adds ExtendedErrors to the SocksPort line, which a reload does
    # not apply (tor keeps an existing listener's flags), so it has to restart.
    backend.up(count=1, base_port=base, timeout=20, tune="onion")
    second = pid_of(env, "oa00")
    assert second != first and wait_dead(first)
    assert backend.status(base)[0]["tune"] == "onion"

    # Same SocksPort line, different tuning lines: SIGHUP, same process.
    backend.up(count=1, base_port=base, timeout=20, tune="onion-aggressive")
    assert pid_of(env, "oa00") == second
    assert json.load(open(os.path.join(env, "oa00", "data", "stub-state.json")))["reloads"] == 1


# ----------------------------------------------------------------- down / destroy


def test_down_stops_the_processes_and_keeps_the_directories(backend, env):
    base = free_block(2)
    backend.up(count=2, base_port=base, timeout=20)
    pids = [pid_of(env, "oa00"), pid_of(env, "oa01")]
    backend.down()
    assert all(wait_dead(p) for p in pids)
    assert not port_open(base) and not port_open(base + 1)
    assert os.path.isdir(os.path.join(env, "oa00")) and os.path.isdir(os.path.join(env, "oa01"))
    backend.up(count=2, base_port=base, timeout=20)
    assert pid_of(env, "oa00") not in pids


def test_destroy_removes_only_the_farms_own_directories(backend, env, tmp_path):
    base = free_block(2)
    backend.up(count=2, base_port=base, timeout=20)
    pids = [pid_of(env, "oa00"), pid_of(env, "oa01")]

    # Decoys named like this host's real instances, and like farm instances made by hand.
    for decoy in ("relay2", "torclient", "default"):
        os.makedirs(os.path.join(env, decoy))
        open(os.path.join(env, decoy, "torrc"), "w").write("# not ours\n")
    handmade = os.path.join(env, "oa07")                 # right name, nobody made it for us
    os.makedirs(handmade)
    open(os.path.join(handmade, "keep.me"), "w").write("x")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "precious").write_text("do not delete")
    os.symlink(elsewhere, os.path.join(env, "oa09"))       # a link out of the state dir

    removed = backend.destroy()

    assert sorted(removed) == ["oa00", "oa01"]
    assert all(wait_dead(p) for p in pids)
    assert not os.path.exists(os.path.join(env, "oa00"))
    for kept in ("relay2/torrc", "torclient/torrc", "default/torrc", "oa07/keep.me"):
        assert os.path.exists(os.path.join(env, kept)), kept
    assert (elsewhere / "precious").read_text() == "do not delete"
    assert os.path.islink(os.path.join(env, "oa09"))


@pytest.mark.parametrize("name", ["relay2", "torclient", "default", "oa", "oa1x", "tor", "../oa00"])
def test_the_process_backend_refuses_every_name_it_does_not_own(backend, name):
    with pytest.raises(ValueError):
        backend.instance_dir(name)


def test_a_directory_the_farm_did_not_create_is_not_adopted(env, monkeypatch):
    os.makedirs(env)
    open(os.path.join(env, "somebody-elses-file"), "w").write("x")
    with pytest.raises(farm_process.FarmError):
        farm_process.ProcessBackend().up(count=1, base_port=free_block(1), timeout=2)
    assert os.listdir(env) == ["somebody-elses-file"]


def test_an_empty_directory_is_adopted_and_marked(backend, env):
    os.makedirs(env)
    backend.up(count=1, base_port=free_block(1), timeout=20)
    assert os.path.exists(os.path.join(env, ".oa-farm"))
    assert os.path.exists(os.path.join(env, "oa00", ".oa-instance"))


def test_a_recycled_pid_is_never_signalled(backend, env):
    """A stale pidfile must not let `down` kill whatever now owns that pid."""
    base = free_block(1)
    backend.up(count=1, base_port=base, timeout=20)
    backend.down()
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        open(os.path.join(env, "oa00", "tor.pid"), "w").write(f"{bystander.pid}\n")
        backend.down()
        backend.destroy()
        assert bystander.poll() is None, "down/destroy killed a process that was not tor"
    finally:
        bystander.kill()
        bystander.wait()


# ----------------------------------------------------------------- selection and the CLI


def test_the_default_backend_is_process_unless_root():
    assert farm.select_backend(None, euid=1000) == "process"
    assert farm.select_backend(None, euid=0) == "systemd"
    assert farm.select_backend("systemd", euid=1000) == "systemd"
    assert farm.select_backend("process", euid=0) == "process"
    with pytest.raises(ValueError):
        farm.select_backend("docker")


def test_the_systemd_backend_calls_the_functions_it_always_did(monkeypatch):
    calls = []
    monkeypatch.setattr(oa, "farm_up", lambda count, base_port, timeout, tune: calls.append(
        ("up", count, base_port, timeout, tune)))
    monkeypatch.setattr(oa, "farm_status", lambda base_port: calls.append(("status", base_port)))
    monkeypatch.setattr(oa, "farm_down", lambda: calls.append(("down",)))
    monkeypatch.setattr(oa, "farm_destroy", lambda: calls.append(("destroy",)))
    backend = farm.SystemdBackend()
    backend.up(4, 5000, 120, "onion")
    backend.status(5000)
    backend.down()
    backend.destroy()
    assert calls == [("up", 4, 5000, 120, "onion"), ("status", 5000), ("down",), ("destroy",)]


def test_farm_up_through_main_uses_the_process_backend_for_an_ordinary_user(
        env, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(oa, "setup_logging", lambda: None)
    monkeypatch.setattr(oa, "farm_up", lambda *a, **k: pytest.fail("systemd path was taken"))
    base = free_block(2)
    oa.main(["--farm", "up", "--count", "2", "--base-port", str(base),
             "--bootstrap-timeout", "20"])
    assert port_open(base) and port_open(base + 1)
    oa.main(["--farm", "destroy"])
    assert not port_open(base) and not os.path.exists(os.path.join(env, "oa00"))


def test_farm_run_stays_up_until_signalled_then_shuts_down_cleanly(env, tmp_path):
    base = free_block(2)
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "OnionAccelerator.py"), "--farm", "run",
         "--count", "2", "--base-port", str(base), "--bootstrap-timeout", "20"],
        cwd=tmp_path, env=dict(os.environ), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True)
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not (port_open(base) and port_open(base + 1)):
            assert proc.poll() is None, proc.stdout.read()
            time.sleep(0.1)
        assert port_open(base), "the farm never came up"
        time.sleep(0.5)
        assert proc.poll() is None, "run returned instead of staying in the foreground"
        pids = [pid_of(env, "oa00"), pid_of(env, "oa01")]
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=20) == 0
        assert all(wait_dead(p) for p in pids)
        assert not port_open(base)
    finally:
        if proc.poll() is None:
            proc.kill()


def test_run_is_not_available_for_the_systemd_backend(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(oa, "setup_logging", lambda: None)
    with pytest.raises(SystemExit) as excinfo:
        oa.main(["--farm", "run", "--farm-backend", "systemd"])
    assert excinfo.value.code == 2


# ----------------------------------------------------------------- stats.json


def test_circuit_counts_are_reported_for_farm_endpoints_only(backend, env):
    base = free_block(2)
    backend.up(count=2, base_port=base, timeout=20)
    counts = farm_process.circuit_counts(
        [f"127.0.0.1:{base}", f"127.0.0.1:{base + 1}", "127.0.0.1:9050"])
    assert counts == {f"127.0.0.1:{base}": {"built": 3, "total": 4},
                      f"127.0.0.1:{base + 1}": {"built": 3, "total": 4}}


def test_circuit_counts_are_empty_without_a_farm(env):
    assert farm_process.circuit_counts(["127.0.0.1:5000"]) == {}


def test_stats_json_gains_per_endpoint_circuit_counts_when_a_control_socket_exists(
        backend, env, tmp_path):
    base = free_block(1)
    backend.up(count=1, base_port=base, timeout=20)
    stats = tmp_path / "stats.json"
    stats.write_text(json.dumps({"totals": {"directories": 2}, "stopped_because": "done"}))

    annotated = farm_process.annotate_stats(str(stats), [f"127.0.0.1:{base}", "127.0.0.1:9050"])

    on_disk = json.load(open(stats))
    assert annotated == on_disk["tor_circuits"] == {
        f"127.0.0.1:{base}": {"built": 3, "total": 4}}
    assert on_disk["totals"] == {"directories": 2}, "the crawler's own record was changed"


def test_stats_json_is_left_alone_when_there_is_nothing_to_add(env, tmp_path):
    stats = tmp_path / "stats.json"
    original = json.dumps({"totals": {}})
    stats.write_text(original)
    assert farm_process.annotate_stats(str(stats), ["127.0.0.1:5000"]) == {}
    assert stats.read_text() == original


def test_crawl_mode_annotates_stats_json_after_the_crawl(env, tmp_path, monkeypatch):
    import types
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(oa, "JOB_ID", "job-under-test")
    seen = {}

    class FakeConfig:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

        compile_filter = staticmethod(lambda patterns: None)

    pkg = types.SimpleNamespace(
        CrawlConfig=FakeConfig,
        crawl=lambda config, proxies, agents, sink=None: (
            {"stopped_because": "exhausted", "totals": {"directories": 1}, "elapsed_s": 0.1}, []),
        config=types.SimpleNamespace(URLS_FILE="urls.txt"))
    monkeypatch.setattr(oa, "_import_crawler", lambda: pkg)
    monkeypatch.setattr(farm_process, "annotate_stats",
                        lambda path, endpoints: seen.update(path=path, endpoints=endpoints) or {})
    args, _ = oa.build_parser().parse_known_args(["--mode", "crawl"])
    oa.crawl_mode(["http://a.onion/"], ["127.0.0.1:5000"], args)

    assert os.path.basename(seen["path"]) == "stats.json" and seen["endpoints"] == ["127.0.0.1:5000"]
