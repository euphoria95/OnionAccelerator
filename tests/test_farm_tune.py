"""--farm-tune: the torrc each farm instance runs, and how '--farm up' applies a new one.

Offline: nothing here starts tor or calls systemctl. farm_up() is driven against stubs
that record every privileged command it would have run, which is the property that
matters on a host where the same systemd stack also runs a real relay.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

import OnionAccelerator as oa

# The torrc the farm rendered before --farm-tune existed. "off" must reproduce it byte for
# byte: anything else and an upgrade restarts every running instance for no reason.
LEGACY_TORRC = (
    "# MANAGED BY OnionAccelerator (--farm). Regenerated on every 'up'; edits are lost.\n"
    "# Farm instance oa03: client-only SOCKS proxy, no relay descriptor published.\n"
    "SocksPort 127.0.0.1:5003\n"
    "ClientOnly 1\n"
    "AvoidDiskWrites 1\n"
    "SafeLogging 1\n"
    "Log notice file /var/lib/tor-instances/oa03/notices.log\n"
)


def _options(torrc):
    return [line for line in torrc.splitlines() if line and not line.startswith("#")]


# ----------------------------------------------------------------- rendering


def test_off_renders_the_legacy_torrc_exactly():
    assert oa.render_torrc("oa03", 5003) == LEGACY_TORRC
    assert oa.render_torrc("oa03", 5003, tune="off", count=20) == LEGACY_TORRC


@pytest.mark.parametrize("tune", ["onion", "onion-aggressive"])
def test_a_profile_adds_its_lines_and_extended_errors(tune):
    torrc = oa.render_torrc("oa03", 5003, tune=tune, count=20)
    options = _options(torrc)
    assert "SocksPort 127.0.0.1:5003 ExtendedErrors" in options
    # Everything the untuned farm had is still there, in the same order.
    assert options[1:5] == _options(LEGACY_TORRC)[1:5]
    for line, _why in oa.FARM_TUNE_PROFILES[tune]:
        assert line.format(mem=oa.farm_mem_budget(20)) in options
    assert oa.farm_tune_of(torrc) == tune


def test_only_the_aggressive_profile_carries_the_trade_off():
    assert "MaxCircuitDirtiness 1800" not in _options(oa.render_torrc("oa00", 5000, "onion"))
    assert "MaxCircuitDirtiness 1800" in _options(
        oa.render_torrc("oa00", 5000, "onion-aggressive"))


def test_every_profile_has_a_socks_flag_entry():
    assert set(oa.FARM_TUNE_SOCKS_FLAGS) == set(oa.FARM_TUNE_PROFILES)


def test_farm_tune_of_an_untuned_torrc_is_off():
    assert oa.farm_tune_of(LEGACY_TORRC) == "off"
    assert oa.farm_tune_of("") == "off"


def test_the_memory_budget_splits_the_host_and_stays_in_bounds(monkeypatch):
    sizes = {"SC_PAGE_SIZE": 4096, "SC_PHYS_PAGES": 16 * 1024 ** 3 // 4096}   # 16 GiB
    monkeypatch.setattr(oa.os, "sysconf", lambda key: sizes[key])
    assert oa.farm_mem_budget(20) == 409           # 8 GiB over twenty instances
    assert oa.farm_mem_budget(1) == 1024           # capped: no client needs more
    assert oa.farm_mem_budget(100) == 256          # floored at tor's own minimum
    assert oa.farm_mem_budget(0) == 1024           # a zero count is not a crash


@pytest.mark.skipif(not shutil.which("tor"), reason="tor is not installed")
@pytest.mark.parametrize("tune", list(oa.FARM_TUNE_PROFILES))
def test_tor_accepts_every_profile(tune, tmp_path):
    """Every option name and value is one this tor actually understands."""
    path = tmp_path / "torrc"
    path.write_text(oa.render_torrc("oa03", 5003, tune=tune, count=20))
    res = subprocess.run(
        ["tor", "--verify-config", "-f", str(path), "--DataDirectory", str(tmp_path / "d"),
         "--Log", "notice stderr"],
        capture_output=True, text=True, timeout=60,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "[warn]" not in res.stdout, res.stdout


# ----------------------------------------------------------------- applying


def test_an_unchanged_torrc_needs_nothing():
    torrc = oa.render_torrc("oa00", 5000, "onion", 20)
    assert oa.torrc_apply_action(torrc, torrc) is None


def test_an_option_change_is_a_reload():
    """MaxCircuitDirtiness is read on SIGHUP; a reload keeps the warm circuits."""
    old = oa.render_torrc("oa00", 5000, "onion", 20)
    new = oa.render_torrc("oa00", 5000, "onion-aggressive", 20)
    assert oa.torrc_apply_action(old, new) == "reload"


def test_a_socks_flag_change_is_a_restart():
    """tor keeps a listener's flags across a reload, so ExtendedErrors needs a restart."""
    old = oa.render_torrc("oa00", 5000, "off", 20)
    new = oa.render_torrc("oa00", 5000, "onion", 20)
    assert oa.torrc_apply_action(old, new) == "restart"
    assert oa.torrc_apply_action(new, old) == "restart"


class _FakeHost:
    """Stands in for sudo/systemctl/tor-instance-create and records what was asked."""

    def __init__(self, monkeypatch, torrcs, active):
        self.commands = []
        self.written = {}
        self.sleeps = []
        self.torrcs = dict(torrcs)
        monkeypatch.setattr(oa, "farm_preflight", lambda: None)
        monkeypatch.setattr(oa, "wait_bootstrap", lambda names, *a, **k: set(names))
        monkeypatch.setattr(oa, "_read_torrc", lambda name: self.torrcs.get(name, ""))
        monkeypatch.setattr(oa, "systemd_active",
                            lambda name: "active" if name in active else "inactive")
        monkeypatch.setattr(oa.os.path, "isdir", lambda path: True)
        monkeypatch.setattr(oa.time, "sleep", self.sleeps.append)
        monkeypatch.setattr(oa, "_run", self._run)
        monkeypatch.setattr(oa, "_write_root_file", self._write)

    def _run(self, cmd, check=True):
        self.commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="")

    def _write(self, path, content):
        self.written[path] = content

    def actions(self, verb):
        return [c[-1] for c in self.commands if c[:2] == ["systemctl", verb]]


def test_farm_up_restarts_running_instances_one_at_a_time(monkeypatch):
    names = [oa.farm_instance_name(i) for i in range(3)]
    torrcs = {n: oa.render_torrc(n, 5000 + i) for i, n in enumerate(names)}
    host = _FakeHost(monkeypatch, torrcs, active=set(names))

    oa.farm_up(3, 5000, tune="onion")

    assert host.actions("restart") == [f"tor@{n}" for n in names]
    assert host.actions("reload") == []
    # Staggered: a gap before every restart but the first.
    assert host.sleeps == [oa.FARM_RESTART_STAGGER] * 2
    for i, n in enumerate(names):
        path = os.path.join(oa.TOR_INSTANCES_DIR, n, "torrc")
        assert host.written[path] == oa.render_torrc(n, 5000 + i, "onion", 3)


def test_farm_up_reloads_when_only_options_changed(monkeypatch):
    torrcs = {"oa00": oa.render_torrc("oa00", 5000, "onion", 1)}
    host = _FakeHost(monkeypatch, torrcs, active={"oa00"})
    oa.farm_up(1, 5000, tune="onion-aggressive")
    assert host.actions("reload") == ["tor@oa00"]
    assert host.actions("restart") == []


def test_farm_up_leaves_an_unchanged_running_farm_alone(monkeypatch):
    torrcs = {"oa00": LEGACY_TORRC.replace("oa03", "oa00").replace("5003", "5000")}
    host = _FakeHost(monkeypatch, torrcs, active={"oa00"})
    oa.farm_up(1, 5000)
    assert host.actions("restart") == [] and host.actions("reload") == []


def test_farm_up_does_not_restart_what_enable_now_just_started(monkeypatch):
    """A stopped instance starts on the new torrc; restarting it too would be waste."""
    host = _FakeHost(monkeypatch, {"oa00": LEGACY_TORRC}, active=set())
    oa.farm_up(1, 5000, tune="onion")
    assert host.actions("enable") == ["tor@oa00"]
    assert host.actions("restart") == []


def test_farm_up_only_ever_names_farm_instances(monkeypatch):
    host = _FakeHost(monkeypatch, {}, active=set())
    oa.farm_up(4, 5000, tune="onion")
    touched = {c[-1].split("@", 1)[-1] for c in host.commands if c[0] == "systemctl"}
    assert touched and all(oa.FARM_RE.match(n) for n in touched)
    assert not touched & oa.FARM_RESERVED


# ----------------------------------------------------------------- the CLI


@pytest.mark.parametrize("action", ["status", "down", "destroy"])
def test_farm_tune_outside_farm_up_is_a_usage_error(action, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        oa.main(["--farm", action, "--farm-tune", "onion"])
    assert excinfo.value.code == 2


def test_farm_tune_reaches_farm_up(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    seen = {}
    monkeypatch.setattr(oa, "farm_up", lambda count, base_port, timeout, tune: seen.update(
        count=count, tune=tune))
    oa.main(["--farm", "up", "--count", "2", "--farm-tune", "onion"])
    assert seen == {"count": 2, "tune": "onion"}


def test_farm_up_without_the_flag_is_untuned(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    seen = {}
    monkeypatch.setattr(oa, "farm_up", lambda count, base_port, timeout, tune: seen.update(
        tune=tune))
    oa.main(["--farm", "up", "--count", "1"])
    assert seen == {"tune": "off"}


# ----------------------------------------------------------------- ExtendedErrors


def test_the_download_modes_and_the_crawler_agree_on_tor_codes():
    fetcher = pytest.importorskip("crawler.fetcher")
    assert oa.TOR_SOCKS_REPLIES == fetcher.TOR_SOCKS_REPLIES


def test_explain_error_names_a_pysocks_onion_code():
    msg = "Failed to establish a new connection: 0xf0: Unknown error"
    assert oa.explain_error(Exception(msg)).endswith("(tor: onion descriptor not found)")


def test_explain_error_leaves_other_errors_and_addresses_alone():
    msg = "<SOCKSConnection object at 0xf3a51c>: 0x06: TTL expired"
    assert oa.explain_error(Exception(msg)) == msg
