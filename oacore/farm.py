"""--farm: a fleet of client-only Tor instances on the systemd tor@<name> stack.

Moved out of OnionAccelerator.py unchanged, safety guards included: nothing here acts
on an instance whose name fails assert_farm_name().
"""

import glob
import os
import re
import shutil
import subprocess
import sys
import time

from tqdm import tqdm

from .common import _port_is_open, logger
from .config import MAX_WORKERS


# =========== NATIVE TOR FARM (--farm) ============
# A farm of client-only Tor instances managed via the native Debian/Ubuntu
# tor-instance-create + systemd tor@<name> stack (no Docker). Instances are
# named "<prefix><NN>" and each binds SocksPort 127.0.0.1:<BASE_PORT + NN>, so
# they line up with what the download modes already expect.
#
# SAFETY: farm operations act ONLY on instances whose name matches FARM_RE and
# is not in FARM_RESERVED. This guarantees a stray --farm destroy can never stop
# or purge a user's own relay/client instances (e.g. "relay2", "torclient").
TOR_INSTANCE_PREFIX = "oa"
FARM_RE = re.compile(r"^oa\d+$")
FARM_RESERVED = {"default", "relay2", "torclient"}
TOR_INSTANCES_DIR = "/etc/tor/instances"
TOR_LIB_DIR = "/var/lib/tor-instances"
TOR_RUN_DIR = "/run/tor-instances"
BOOTSTRAP_TIMEOUT = 120        # seconds to wait for each instance to reach 100%
# Gap between restarting one farm instance and the next when '--farm up' has to apply a
# changed torrc: the pool is never cold all at once, and a crawl running on it loses one
# endpoint at a time rather than every one together.
FARM_RESTART_STAGGER = 2.0

# --farm-tune: torrc lines appended to every farm instance, as (line, why) pairs. Drawn
# from "Tor Client Performance Tuning" (the section numbers below are its) and then cut
# down by an A/B benchmark against a self-hosted onion, with a crossover so guard luck
# could not pass for a config effect (README, "Tuning the farm"). What survived is what
# fixes something observed on a real farm; the latency and throughput knobs the guide
# rates Safe -- MaxClientCircuitsPending 96 among them, tried at 64 concurrent cold
# connects per instance -- measured no different from the defaults, and SocksTimeout
# duplicates timeouts every mode here already sets lower. Left out on principle:
# Conflux (exit traffic only, never onions), CircuitStreamTimeout (a defended onion
# legitimately takes tens of seconds), and anything that narrows path selection
# (ExitNodes, EntryNodes, NumEntryGuards): the target is an observer too, and a crawler
# that pins its path is a crawler that clusters in the target's logs. Congestion
# control, PoW and vanguards-lite are on by default on tor >= 0.4.8.
# "{mem}" is filled in by farm_mem_budget(); see there.
FARM_TUNE_SAFE = (
    ("DormantClientTimeout 7 days",
     "§4.4 Safe: the default 24 h put the whole farm to sleep between runs"),
    ("DormantCanceledByStartup 1",
     "§4.4 Safe: a farm brought up on purpose is never dormant at start"),
    ("CircuitsAvailableTimeout 21600",
     "§5.2 Safe: keep predicted onion circuits built through gaps between jobs"),
    ("MaxMemInQueues {mem} MB",
     "§10.2 Safe: N instances must not each budget for the whole host's RAM"),
)
FARM_TUNE_PROFILES = {
    "off": (),
    "onion": FARM_TUNE_SAFE,
    "onion-aggressive": FARM_TUNE_SAFE + (
        ("MaxCircuitDirtiness 1800",
         "§5 Trade-off: reuse a circuit for 30 min, not 10; more linkable, fewer builds"),
    ),
}
# ExtendedErrors (§7.3) is a SocksPort flag rather than an option. It makes tor report
# *why* an onion failed (descriptor gone, intro failed, rendezvous failed...) instead of
# 'host unreachable' for all of them, which the crawler turns into its retry policy.
FARM_TUNE_SOCKS_FLAGS = {"off": "", "onion": " ExtendedErrors",
                         "onion-aggressive": " ExtendedErrors"}
FARM_TUNE_HEADER = "# --farm-tune "


# =========== NATIVE TOR FARM ===========

def assert_farm_name(name):
    """
    Guard: only ever mutate instances that belong to the farm.

    Raises ValueError unless `name` matches FARM_RE (e.g. 'oa07') and is not one
    of the reserved instance names. Every privileged mutation (config write,
    systemctl stop/disable, directory removal) routes through here so the farm
    can never touch a user's own relay/client instance.
    """
    if name in FARM_RESERVED or not FARM_RE.match(name):
        raise ValueError(f"Refusing to operate on non-farm Tor instance: {name!r}")
    return name


def _run(cmd, check=True):
    """Run a command, prefixing sudo when not already root. Returns CompletedProcess."""
    if os.geteuid() != 0:
        cmd = ["sudo"] + cmd
    logger.debug(f"exec: {' '.join(cmd)}")
    return subprocess.run(cmd, check=check, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def _write_root_file(path, content):
    """Write `content` to `path` with root privileges (via tee), 0644."""
    if os.geteuid() != 0:
        subprocess.run(["sudo", "tee", path], input=content, text=True,
                       check=True, stdout=subprocess.DEVNULL)
    else:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)


def farm_instance_name(index):
    return f"{TOR_INSTANCE_PREFIX}{index:02d}"


def farm_preflight():
    """Abort early with a clear message if the native tooling isn't available."""
    if sys.platform != "linux":
        logger.error("--farm requires Linux (native tor-instance-create + systemd).")
        sys.exit(1)
    missing = [t for t in ("tor-instance-create", "systemctl") if not shutil.which(t)]
    if missing:
        logger.error(f"Missing required tool(s): {', '.join(missing)}. "
                     f"Install the 'tor' package (Debian/Ubuntu).")
        sys.exit(1)
    # Onion-service proof-of-work (tor >= 0.4.8) needs tor built with the pow module.
    # Without it a defended onion is unreachable however the farm is tuned, and it fails
    # as 'introduction failed', which reads like an overloaded target, not a local gap.
    try:
        modules = subprocess.run(["tor", "--list-modules"], capture_output=True,
                                 text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        modules = ""
    if modules and "pow: yes" not in modules:
        logger.warning("This tor was built without the 'pow' module, so onion services "
                       "defended by proof-of-work will refuse every connection. Install "
                       "tor from the Tor Project's own repository (tor >= 0.4.8).")


def farm_mem_budget(count):
    """MaxMemInQueues for one of `count` farm instances, in MB.

    Tor sizes its queue budget from the RAM it can see, and every instance sees all of
    it: twenty instances each planning to use most of the host is an OOM kill waiting
    for a busy crawl. Half the host split between them, never under tor's own 256 MB
    floor and never over 1 GB, which no single client needs.
    """
    try:
        ram_mb = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // (1024 * 1024)
    except (ValueError, OSError, AttributeError):
        ram_mb = 8192
    return min(1024, max(256, ram_mb // 2 // max(1, count)))


def render_torrc(name, port, tune="off", count=1):
    """Client-only torrc for a farm instance bound to 127.0.0.1:<port>.

    `tune` picks a FARM_TUNE_PROFILES entry; "off" renders exactly the torrc the farm
    always had, so an existing farm is not restarted by an upgrade. `count` is the size
    of the farm, which the memory budget is split across.
    """
    torrc = (
        "# MANAGED BY OnionAccelerator (--farm). Regenerated on every 'up'; edits are lost.\n"
        f"# Farm instance {name}: client-only SOCKS proxy, no relay descriptor published.\n"
        f"SocksPort 127.0.0.1:{port}{FARM_TUNE_SOCKS_FLAGS[tune]}\n"
        "ClientOnly 1\n"
        "AvoidDiskWrites 1\n"
        "SafeLogging 1\n"
        f"Log notice file {TOR_LIB_DIR}/{name}/notices.log\n"
    )
    if tune == "off":
        return torrc
    torrc += f"{FARM_TUNE_HEADER}{tune}\n"
    for line, why in FARM_TUNE_PROFILES[tune]:
        torrc += f"# {why}\n{line.format(mem=farm_mem_budget(count))}\n"
    return torrc


def farm_tune_of(torrc):
    """The --farm-tune profile a rendered torrc was built with ("off" if none)."""
    for line in torrc.splitlines():
        if line.startswith(FARM_TUNE_HEADER):
            return line[len(FARM_TUNE_HEADER):].strip()
    return "off"


def _read_torrc(name):
    """The torrc a farm instance is running now, or "" if it has none yet."""
    path = os.path.join(TOR_INSTANCES_DIR, name, "torrc")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except PermissionError:
        return _run(["cat", path], check=False).stdout
    except OSError:
        return ""


def _socks_line(torrc):
    return next((l for l in torrc.splitlines() if l.startswith("SocksPort ")), "")


def torrc_apply_action(old, new):
    """How a running instance picks up `new` in place of `old`: None, reload or restart.

    A reload (SIGHUP) keeps the instance's circuits and descriptor cache, which is the
    whole of what a warm client is worth, so it is preferred. But tor keeps an existing
    listener's flags across a reload -- a SocksPort that gained ExtendedErrors went on
    answering 'host unreachable' until restarted -- so a changed SocksPort line needs
    the restart.
    """
    if old == new:
        return None
    return "restart" if _socks_line(old) != _socks_line(new) else "reload"


def bootstrap_percent(name):
    """Parse the newest 'Bootstrapped NN%' line from an instance's notices.log."""
    log_path = f"{TOR_LIB_DIR}/{name}/notices.log"
    try:
        res = _run(["tail", "-n", "200", log_path], check=False)
    except Exception:
        return 0
    pct = 0
    for line in res.stdout.splitlines():
        m = re.search(r"Bootstrapped (\d+)%", line)
        if m:
            pct = int(m.group(1))
    return pct


def wait_bootstrap(names, base_port, timeout=BOOTSTRAP_TIMEOUT):
    """Wait until every instance reports Bootstrapped 100% (or times out)."""
    logger.info(f"Waiting for {len(names)} instance(s) to bootstrap (timeout {timeout}s each)...")
    deadline = time.time() + timeout
    pending = set(names)
    done = set()
    with tqdm(total=len(names), desc="Bootstrapping", unit="inst") as pbar:
        while pending and time.time() < deadline:
            for name in list(pending):
                idx = int(name[len(TOR_INSTANCE_PREFIX):])
                port = base_port + idx
                if bootstrap_percent(name) >= 100 and _port_is_open("127.0.0.1", port):
                    pending.discard(name)
                    done.add(name)
                    pbar.update(1)
            if pending:
                time.sleep(1)
    if pending:
        logger.warning(f"Timed out waiting on: {', '.join(sorted(pending))}. "
                       f"Check 'systemctl status tor@<name>' / their notices.log.")
    logger.info(f"{len(done)}/{len(names)} instance(s) bootstrapped and listening.")
    return done


def farm_up(count, base_port, timeout=BOOTSTRAP_TIMEOUT, tune="off"):
    farm_preflight()
    if count > MAX_WORKERS:
        logger.warning(f"count={count} exceeds MAX_WORKERS={MAX_WORKERS}; "
                       f"the download modes will only use the first {MAX_WORKERS}.")
    logger.info(f"Bringing up {count} Tor instance(s) on 127.0.0.1:{base_port}-"
                f"{base_port + count - 1} (--farm-tune {tune})")
    names = []
    restarted = 0
    for i in range(count):
        name = assert_farm_name(farm_instance_name(i))
        port = base_port + i
        inst_dir = os.path.join(TOR_INSTANCES_DIR, name)
        if not os.path.isdir(inst_dir):
            logger.info(f"[{name}] creating instance")
            _run(["tor-instance-create", name])
        else:
            logger.info(f"[{name}] already exists, refreshing config")
        torrc = render_torrc(name, port, tune=tune, count=count)
        old = _read_torrc(name)
        was_active = systemd_active(name) == "active"
        _write_root_file(os.path.join(inst_dir, "torrc"), torrc)
        _run(["systemctl", "enable", "--now", f"tor@{name}"])
        # 'enable --now' starts a stopped instance and does nothing to a running one, so
        # a changed torrc on a running instance used to wait for its next reboot.
        action = torrc_apply_action(old, torrc) if was_active else None
        if action == "restart":
            if restarted:
                time.sleep(FARM_RESTART_STAGGER)
            restarted += 1
        if action:
            logger.info(f"[{name}] torrc changed; {action}ing to apply it")
            _run(["systemctl", action, f"tor@{name}"])
        names.append(name)
    wait_bootstrap(names, base_port, timeout=timeout)
    logger.info("Farm is up. Run a download mode, e.g. "
                "'python3 OnionAccelerator.py --mode multi'.")


def farm_instances_on_disk():
    """Return sorted names of farm instances (oa*) that exist under /etc/tor/instances."""
    found = []
    for path in glob.glob(os.path.join(TOR_INSTANCES_DIR, f"{TOR_INSTANCE_PREFIX}*")):
        name = os.path.basename(path)
        if FARM_RE.match(name) and name not in FARM_RESERVED:
            found.append(name)
    return sorted(found)


def systemd_active(name):
    res = _run(["systemctl", "is-active", f"tor@{name}"], check=False)
    return res.stdout.strip()


def farm_status(base_port):
    names = farm_instances_on_disk()
    if not names:
        logger.info("No farm instances found. Create some with '--farm up'.")
        return
    logger.info(f"=== FARM STATUS ({len(names)} instance(s)) ===")
    for name in names:
        idx = int(name[len(TOR_INSTANCE_PREFIX):])
        port = base_port + idx
        state = systemd_active(name)
        listening = _port_is_open("127.0.0.1", port)
        pct = bootstrap_percent(name)
        tune = farm_tune_of(_read_torrc(name))
        mark = "✅" if (state == "active" and listening and pct >= 100) else "⚠️"
        logger.info(f"{mark} {name}: systemd={state}, socks=127.0.0.1:{port} "
                    f"({'listening' if listening else 'down'}), bootstrap={pct}%, "
                    f"tune={tune}")


def farm_down():
    names = farm_instances_on_disk()
    if not names:
        logger.info("No farm instances to stop.")
        return
    for name in names:
        assert_farm_name(name)
        logger.info(f"[{name}] stopping")
        _run(["systemctl", "disable", "--now", f"tor@{name}"], check=False)
    logger.info(f"Stopped {len(names)} instance(s). Configs/data kept (use '--farm destroy' to purge).")


def farm_destroy():
    farm_preflight()
    names = farm_instances_on_disk()
    if not names:
        logger.info("No farm instances to destroy.")
        return
    logger.warning(f"Destroying {len(names)} farm instance(s): {', '.join(names)}")
    for name in names:
        assert_farm_name(name)
        _run(["systemctl", "disable", "--now", f"tor@{name}"], check=False)
        for d in (os.path.join(TOR_INSTANCES_DIR, name),
                  os.path.join(TOR_LIB_DIR, name),
                  os.path.join(TOR_RUN_DIR, name)):
            if os.path.exists(d):
                _run(["rm", "-rf", d], check=False)
        logger.info(f"[{name}] removed")
    logger.info("Farm destroyed. Your own (non-farm) Tor instances were left untouched.")


def run_farm_action(action, count, base_port, timeout, tune="off"):
    if action == "up":
        farm_up(count, base_port, timeout=timeout, tune=tune)
    elif action == "status":
        farm_status(base_port)
    elif action == "down":
        farm_down()
    elif action == "destroy":
        farm_destroy()
