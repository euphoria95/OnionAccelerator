#!/usr/bin/env python3
"""OnionAccelerator: discover and download files over pools of Tor SOCKS5 circuits.

This file is the command line and the glue between modes. The download modes, the size
probe and the farm live in oacore/ and are re-exported from here: tests, contrib
scripts and older callers reach them as `OnionAccelerator.<name>`, and the forwarding
table at the bottom keeps a monkeypatch applied here effective where the code runs.
"""
import argparse
import contextlib
import os
import re
import sys
import time
import random
import shlex
import string
import threading
import types

import requests
from tqdm import tqdm
import logging

# The --full-help reference. Kept out of this file because it is 300 lines of prose, and
# imported eagerly because it is pure stdlib with no side effects -- unlike crawler/ and
# remote_viewer/, it can never make --help fail on a host missing a dependency.
import fullhelp

# Importing oacore.common also silences urllib3's InsecureRequestWarning: TLS
# verification is off on purpose, since an onion address authenticates the peer.
from oacore import common as _common, config as _config, download as _download
from oacore import farm as _farm, sizeprobe as _sizeprobe
from oacore.config import (
    MAX_WORKERS, BASE_PORT, CHUNK_SIZE, MIN_PARTIAL_CHUNK_SIZE, REQUEST_TIMEOUT,
    TRANSFER_TIMEOUT, USERAGENTS_FILE, URLS_FILE, DOWNLOAD_DIR,
    PARTIAL_DOWNLOAD_DIR, LOGS_DIR, DEFAULT_RETRIES, MAX_SPEEDTEST_BYTES,
    SPEEDTEST_CHUNK_SIZE, EXTERNAL_PROXIES_URL, MAX_EXTERNAL_PROXIES,
    CONNECTIVITY_TIMEOUT, PROXY_TEST_URL,
)
from oacore.common import (
    TOR_SOCKS_REPLIES, load_user_agents, random_user_agent, url_to_local_path,
    _safe_path_component, make_proxies, _TOR_REPLY_RE, explain_error,
    PORT_PROBE_TIMEOUT, _port_is_open, discover_local_socks_ports,
)
from oacore.sizeprobe import (
    IDENTITY_ENCODINGS, SizeProbe, _positive_int, _size_from_content_length,
    _size_from_content_disposition, _size_from_content_range, _size_from_response,
    _probe, probe_remote_size, get_remote_file_size,
)
from oacore.download import (
    ProxyPool, _UMASK, _DOWNLOAD_MODE, _MARKUP_TYPES, _MARKUP_EXTENSIONS,
    _error_page_reason, _check_response, download_file, _warn_on_path_collisions,
    multi_download_mode, partial_download_chunk_to_file,
    fallback_sequential_download, partial_download_file, partial_download_mode,
)
from oacore.farm import (
    TOR_INSTANCE_PREFIX, FARM_RE, FARM_RESERVED, TOR_INSTANCES_DIR, TOR_LIB_DIR,
    TOR_RUN_DIR, BOOTSTRAP_TIMEOUT, FARM_RESTART_STAGGER, FARM_TUNE_SAFE,
    FARM_TUNE_PROFILES, FARM_TUNE_SOCKS_FLAGS, FARM_TUNE_HEADER, assert_farm_name,
    _run, _write_root_file, farm_instance_name, farm_preflight, farm_mem_budget,
    render_torrc, farm_tune_of, _read_torrc, _socks_line, torrc_apply_action,
    bootstrap_percent, wait_bootstrap, farm_up, farm_instances_on_disk,
    systemd_active, farm_status, farm_down, farm_destroy, run_farm_action,
)


# =========== LOGGER & JOB_ID SETUP ============
# Logger is configured in setup_logging(), called from main(), to avoid
# side effects (file creation, stdout output) on module import.
JOB_ID = None
logger = logging.getLogger("OnionAccelerator")


def generate_job_id():
    ts = time.strftime("%Y%m%d_%H%M%S")
    rnd = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"{ts}_{rnd}"


def setup_logging():
    global JOB_ID
    JOB_ID = generate_job_id()
    logger.setLevel(logging.DEBUG)
    os.makedirs(LOGS_DIR, exist_ok=True)
    log_filename = os.path.join(LOGS_DIR, f"OnionAccelerator_{JOB_ID}.log")

    fh = logging.FileHandler(log_filename)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
    logger.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter('[%(levelname)s] %(message)s'))
    logger.addHandler(ch)


# =========== EXTERNAL PROXIES ===========

def fetch_external_proxies():
    """
    Fetch 'host:port' SOCKS5 proxies from EXTERNAL_PROXIES_URL.

    The list is read fresh on every run and never written to disk, so each
    execution picks up an up-to-date set of proxies.
    """
    logger.info(f"Fetching external proxy list from {EXTERNAL_PROXIES_URL}")
    try:
        r = requests.get(EXTERNAL_PROXIES_URL, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
    except Exception as e:
        logger.error(f"Failed to fetch external proxies: {e}")
        return []

    endpoints = []
    for line in r.text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # rpartition on ':' keeps IPv6 hosts (e.g. [::1]:9050) intact.
        host, sep, port = line.rpartition(":")
        if not sep or not host or not port.isdigit():
            logger.debug(f"Skipping malformed proxy line: {line!r}")
            continue
        endpoints.append(f"{host}:{port}")
    logger.info(f"Fetched {len(endpoints)} candidate proxies.")
    return endpoints


def verify_proxy(proxy, test_url, user_agents):
    """Return True if the proxy can actually fetch test_url within the timeout."""
    proxies = make_proxies(proxy)
    ua = random_user_agent(user_agents)
    try:
        with requests.get(test_url, proxies=proxies, timeout=CONNECTIVITY_TIMEOUT,
                          verify=False, stream=True, headers={"User-Agent": ua}) as r:
            r.raise_for_status()
            # Pull one chunk to confirm bytes actually flow through the proxy.
            # An empty (0-byte) 200 response must NOT count as a working proxy.
            chunk = next(r.iter_content(SPEEDTEST_CHUNK_SIZE), None)
        return bool(chunk)
    except Exception as e:
        logger.debug(f"[proxy down] {proxy} via {test_url}: {e}")
        return False


def verify_proxies(candidates, test_url, user_agents):
    """
    Check each candidate proxy in parallel against a single connectivity-check
    URL. Return only the proxies that pass.
    """
    logger.info(f"Verifying connectivity of {len(candidates)} proxies against {test_url} ...")
    working = []
    lock = threading.Lock()

    with tqdm(total=len(candidates), desc="Verifying proxies", unit="proxy") as pbar:
        def check(proxy):
            ok = verify_proxy(proxy, test_url, user_agents)
            with lock:
                if ok:
                    working.append(proxy)
                pbar.update(1)

        threads = [threading.Thread(target=check, args=(c,)) for c in candidates]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    logger.info(f"{len(working)}/{len(candidates)} proxies passed the connectivity check.")
    return working


def resolve_proxies(use_external, urls, test_url=None):
    """
    Return the list of 'host:port' SOCKS5 endpoints to use for this run.

    Default: local Docker Tor instances on ports BASE_PORT..BASE_PORT+MAX_WORKERS-1.
    External (--external): fetched fresh from EXTERNAL_PROXIES_URL (never persisted),
    capped at MAX_EXTERNAL_PROXIES and filtered down to proxies that pass a live
    connectivity check against `test_url` (see PROXY_TEST_URL / --test-url).
    """
    if not use_external:
        live = discover_local_socks_ports()
        if live:
            first = live[0].rsplit(":", 1)[1]
            last = live[-1].rsplit(":", 1)[1]
            logger.info(f"Discovered {len(live)} live local Tor SOCKS ports ({first}-{last}).")
            return live
        # Nothing is listening on the expected range. Fall back to the fixed list
        # so a Docker/manual setup that races the probe still gets a chance, but
        # nudge the user toward the built-in farm provisioner.
        logger.warning(f"No live SOCKS ports found on 127.0.0.1:{BASE_PORT}-"
                       f"{BASE_PORT + MAX_WORKERS - 1}. Start a farm with "
                       f"'--farm up' or launch your proxies, then retry. "
                       f"Falling back to the fixed port list.")
        return [f"127.0.0.1:{BASE_PORT + i}" for i in range(MAX_WORKERS)]

    logger.warning("[SECURITY] --external routes traffic through untrusted third-party "
                   "proxies fetched from a remote list; this weakens the anonymity "
                   "guarantees of a local Tor setup.")
    candidates = fetch_external_proxies()
    if len(candidates) > MAX_EXTERNAL_PROXIES:
        candidates = random.sample(candidates, MAX_EXTERNAL_PROXIES)
        logger.info(f"Capped to {MAX_EXTERNAL_PROXIES} candidate proxies.")
    if not candidates:
        return []

    # Verify against a dedicated test URL so real targets aren't leaked to proxies
    # that fail the check. --test-url overrides the PROXY_TEST_URL config constant.
    check_url = test_url or PROXY_TEST_URL
    if not check_url:
        check_url = random.choice(urls)
        logger.warning("[OPSEC] No --test-url / PROXY_TEST_URL configured; verifying "
                       "proxies against a random target URL, which reveals it to every "
                       "candidate proxy. Set a dedicated test URL to avoid this.")

    user_agents = load_user_agents()
    return verify_proxies(candidates, check_url, user_agents)


# =========== SPEEDTEST + availability check ===========

def test_proxy_speed(url: str, proxy: str, ua) -> dict:
    proxies = make_proxies(proxy)
    result = {
        "proxy": proxy,
        "success": False,
        "downloaded_bytes": 0,
        "speed_bps": 0.0,
        "error": None
    }
    start_time = time.time()
    downloaded = 0
    try:
        with requests.get(url, proxies=proxies, timeout=REQUEST_TIMEOUT,
                          verify=False, stream=True, headers={"User-Agent": ua}) as r:
            r.raise_for_status()
            for chunk in r.iter_content(SPEEDTEST_CHUNK_SIZE):
                if not chunk:
                    break
                downloaded += len(chunk)
                if downloaded >= MAX_SPEEDTEST_BYTES:
                    break
    except Exception as e:
        result["error"] = str(e)
        return result

    elapsed = time.time() - start_time
    if elapsed < 0.0001:
        elapsed = 0.0001
    result["downloaded_bytes"] = downloaded
    result["speed_bps"] = downloaded / elapsed
    result["success"] = True
    return result


def check_url_availability(url, proxy, ua=None, speed_bps=None):
    """
    Probe the URL to see if it is available; if size and speed are known, estimate ETA.
    Returns (is_ok, size, eta_seconds).

    Availability means "some probe got an answer", not "HEAD didn't raise": a server
    that answers 405 to HEAD but serves the file happily was reported as unavailable
    before, and its size was missed whenever it withheld Content-Length.
    """
    probe = probe_remote_size(url, proxy, ua)
    if not probe.reachable:
        logger.error(f"URL not available: {url}, err={probe.error}")
        return (False, None, None)
    if probe.size is None:
        logger.info(f"URL available but no size info: {url}")
        return (True, None, None)

    eta_sec = None
    if speed_bps and speed_bps > 0:
        eta_sec = probe.size / speed_bps

    return (True, probe.size, eta_sec)


def speedtest_mode(urls, proxies):
    if not urls:
        logger.warning("No URLs for speedtest.")
        return
    test_url = urls[0]
    logger.info(f"[Speedtest] Testing first URL: {test_url}")

    user_agents = load_user_agents()
    n = len(proxies)
    results = [None] * n

    def test_one(i):
        proxy = proxies[i]
        ua = random_user_agent(user_agents)
        logger.info(f"Testing proxy={proxy} ...")
        results[i] = test_proxy_speed(test_url, proxy, ua)

    threads = [threading.Thread(target=test_one, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    successful_speeds = []
    logger.info("\n=== SPEEDTEST SUMMARY ===")
    for r in results:
        if r["success"]:
            sp_kbps = r["speed_bps"] / 1024
            successful_speeds.append(r["speed_bps"])
            logger.info(f"✅ Proxy {r['proxy']} => downloaded={r['downloaded_bytes']} B, speed={sp_kbps:.2f} KB/s")
        else:
            logger.error(f"❌ Proxy {r['proxy']} => err={r['error']}")
    logger.info("[DONE] Speedtest for proxies.\n")

    if successful_speeds:
        avg_speed_bps = sum(successful_speeds) / len(successful_speeds)
        logger.info(f"[INFO] Average speed among working proxies: {avg_speed_bps/1024:.2f} KB/s")
    else:
        avg_speed_bps = None
        logger.warning("[WARN] No successful proxies => no average speed.")

    # Use a random working proxy for availability checks instead of always the first.
    working_proxies = [r["proxy"] for r in results if r["success"]]

    logger.info("[INFO] Checking availability for all URLs, computing ETA if possible:")
    for u in urls:
        proxy = random.choice(working_proxies) if working_proxies else proxies[0]
        ua = random_user_agent(user_agents)
        is_ok, size, eta_sec = check_url_availability(u, proxy, ua=ua, speed_bps=avg_speed_bps)
        if not is_ok:
            logger.error(f"❌ Unavailable: {u}")
            continue
        if size is not None:
            if eta_sec:
                logger.info(f"✅ {u}, size={size} B, ETA ~ {eta_sec:.2f} s")
            else:
                logger.info(f"✅ {u}, size={size} B, no ETA (no speed?).")
        else:
            logger.info(f"✅ {u}, no size => no ETA.")

    logger.info("[DONE] Speedtest + availability check.\n")


# =========== REMOTE ARCHIVE TREE (--mode tree) ===========
# rvtree lists and extracts members of a huge remote archive (.rar, .tar, .zip, .7z,
# .tar.xz) using HTTP Range requests instead of downloading it. It ships in
# remote_viewer/ next to this script and is imported lazily, inside tree_mode(), so that
# importing this module stays free of side effects and --farm still works on a host
# where rvtree is absent.
#
# The point of the integration is the network layer: rvtree's pooled transport takes the
# same 'host:port' SOCKS5 endpoints the download modes use, so a --farm of independent
# Tor daemons (or an --external list) gives it real parallelism. Its own default is a
# single daemon, which is what made a 2.4 GB listing take two hours.
RVTREE_DIR = "remote_viewer"
# rvtree's subcommands, mirrored rather than imported: --url has to know where rvtree
# expects its URL positional, and working that out must not drag httpx into a run that
# only wants --help. tests/test_cli.py reads rvtree's own MODES out of its source and
# fails if the two ever disagree.
TREE_SUBCOMMANDS = ("list", "extract", "probe")
TREE_CIRCUITS_PER_ENDPOINT = 1
TREE_HEDGE = 3                 # copies of a small range raced across endpoints
LOCAL_TOR_SOCKS = "127.0.0.1:9150"   # a personal Tor client, used when no farm is up


def _import_rvtree():
    """Import rvtree, or explain why it could not be used.

    An installed rvtree (`pip install .` bundles it) wins. Only when none is importable
    does an uninstalled checkout fall back to the copy in remote_viewer/ beside this file.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    pkg_dir = os.path.join(here, RVTREE_DIR)
    try:
        try:
            from rvtree import cli as rvtree_cli
        except ModuleNotFoundError as e:
            if e.name != "rvtree" or not os.path.isdir(os.path.join(pkg_dir, "rvtree")):
                raise
            if pkg_dir not in sys.path:
                sys.path.insert(0, pkg_dir)
            from rvtree import cli as rvtree_cli
        from rvtree.transport import PooledTransport
        return rvtree_cli, PooledTransport
    except ImportError as e:
        logger.error(f"--mode tree needs the rvtree package: {e}. Expected it under "
                     f"{pkg_dir}, or install it with 'pip install -e {RVTREE_DIR}'.")
        return None


def tree_urls(rv_args):
    """The URLs in the passed-through rvtree arguments.

    Only used as the last-resort target for the external-proxy liveness check, which is
    why --test-url exists: see the warning resolve_proxies() emits. Deliberately crude: it
    will happily return the value of rvtree's own '--proxy socks5://...', which costs
    nothing when all you need is something to ping. Use rv_target_urls() instead wherever
    being wrong would cost the user an error message.
    """
    return [a for a in rv_args if "://" in a]


def rv_target_urls(rv_args):
    """The URLs rvtree would read as its *positional* target, ignoring option values.

    tree_urls() cannot be used to decide whether --url collides with an inline URL: it
    matches 'socks5://127.0.0.1:9050' after --proxy just as readily as the archive, and
    would reject a perfectly good command line. So skip anything that looks like an
    option, and anything sitting in an option's value slot.

    Boolean flags ('--force', '-v') make this under-detect, since the URL after one sits
    in what looks like a value slot. That is the safe direction: under-detecting hands
    rvtree an extra positional and it says so plainly, while over-detecting would reject
    a command line that was fine.
    """
    urls = []
    previous = None
    for arg in rv_args[1:]:            # rv_args[0] is the subcommand
        is_option = arg.startswith("-")
        # A value slot: the previous token was an option and did not carry its own '='.
        in_value_slot = (previous is not None and previous.startswith("-")
                         and previous != "--" and "=" not in previous)
        if not is_option and not in_value_slot and "://" in arg:
            urls.append(arg)
        previous = arg
    return urls


def resolve_tree_endpoints(args, urls):
    """SOCKS5 endpoints for tree mode, preferring a farm and degrading to a local client.

    Deliberately not resolve_proxies()' local branch: that falls back to the *fixed*
    BASE_PORT..BASE_PORT+19 list when nothing is listening, which for a download job is a
    reasonable bet on a racing Docker setup, but here would hand the transport twenty
    dead endpoints to time out on one at a time.
    """
    if args.external:
        return resolve_proxies(True, urls, test_url=args.test_url)

    live = discover_local_socks_ports(base_port=args.base_port, max_ports=args.count)
    if live:
        logger.info(f"Discovered {len(live)} live local Tor SOCKS port(s) "
                    f"({live[0]} - {live[-1]}).")
        return live

    host, port = LOCAL_TOR_SOCKS.rsplit(":", 1)
    if _port_is_open(host, int(port)):
        logger.warning(f"No farm on 127.0.0.1:{args.base_port}-"
                       f"{args.base_port + args.count - 1}; falling back to the local Tor "
                       f"client at {LOCAL_TOR_SOCKS}. That is a single daemon, so the "
                       f"listing runs one request at a time. For real parallelism start a "
                       f"farm first: 'sudo python3 OnionAccelerator.py --farm up --count 8'.")
        return [LOCAL_TOR_SOCKS]

    logger.error(f"No SOCKS5 proxy found: nothing on 127.0.0.1:{args.base_port}-"
                 f"{args.base_port + args.count - 1} and nothing on {LOCAL_TOR_SOCKS}. "
                 f"Start a farm with 'sudo python3 OnionAccelerator.py --farm up', or "
                 f"pass --external.")
    return []


def tree_mode(rv_args, proxies, retries=DEFAULT_RETRIES, sink=None):
    """Run one rvtree invocation over the resolved endpoints. Returns rvtree's exit code.

    `sink` is handed straight to rvtree, which publishes each archive member to it as the
    walk finds it. Listing a multi-gigabyte archive by range requests over Tor is an
    hour's work; this is what makes that hour searchable while it passes.
    """
    imported = _import_rvtree()
    if imported is None:
        return 1
    rvtree_cli, PooledTransport = imported

    rv_args = list(rv_args)
    if "--log-file" not in rv_args:
        # rvtree writes its own full debug log; point it at this job so the run is
        # recoverable the same way every other mode's is.
        os.makedirs(LOGS_DIR, exist_ok=True)
        rv_args += ["--log-file", os.path.join(LOGS_DIR, f"rvtree_{JOB_ID}.log")]

    try:
        transport = PooledTransport(
            endpoints=proxies,
            circuits_per_endpoint=TREE_CIRCUITS_PER_ENDPOINT,
            user_agents=load_user_agents(),
            timeout=(REQUEST_TIMEOUT, REQUEST_TIMEOUT * 6),
            retries=retries,
            verify=False,          # onion services are their own authentication
            hedge=TREE_HEDGE,
        )
    except Exception as e:
        logger.error(f"Could not build the pooled transport: {e}")
        return 1

    logger.info(f"rvtree over {transport.lanes} lane(s): {' '.join(rv_args)}")
    code = 1
    try:
        code = rvtree_cli.main(rv_args, transport=transport, sink=sink or None)
        return code
    except Exception as e:
        logger.error(f"rvtree failed: {e}")
        return 1
    finally:
        transport.close()
        if sink:
            sink("run.stop", {"mode": "tree", "exit_code": code,
                              "stopped_because": "finished" if code == 0 else "failed",
                              "bytes_fetched": transport.bytes_fetched,
                              "requests": transport.requests_made})


# =========== STREAMING API (--stream) ===========

# --stream publishes what a run finds, as it finds it, to an HTTP endpoint an external
# script can attach to. It exists because the runs that matter here take hours: a crawl
# of a leak site's file manager over Tor, or a listing of a 2 GB archive by range
# requests. Everything those runs learn is already written to disk, but not until they
# stop, and an incident response that has to wait for the run to finish before it can
# search for the names in scope is an incident response running at the speed of Tor.
#
# The implementation lives in eventstream/ and is standard library only, so switching it
# on cannot fail on a host that has not installed anything. It is still imported lazily,
# like crawler/ and rvtree/, so that importing this module stays free of side effects.
STREAM_BIND = "127.0.0.1:8787"
STREAM_BUFFER = 10000
STREAM_BODY_BYTES = 64 * 1024
# How long a run waits at the end for consumers to read what it just published. Bounded:
# the JSONL on disk is complete, so a wedged grep must not hold the process open.
STREAM_DRAIN = 5.0
# Ceiling on what the replay ring may hold when --stream-bodies is on. Without one the
# two defaults multiply: 10,000 events of 64 KiB of page text, each kept as the record
# *and* as the serialised line a consumer reads, is over a gigabyte of an analysis box's
# memory held for the whole run.
STREAM_BODY_RING_BYTES = 64 * 1024 * 1024


def _import_eventstream():
    """Import the eventstream package, or explain why the flag cannot be honoured."""
    try:
        import eventstream
        return eventstream
    except ImportError as e:                          # pragma: no cover - stdlib only
        logger.error(f"--stream needs the eventstream package ({e}). Expected it next "
                     f"to this script; it has no third-party dependencies.")
        return None


class _NoStream:
    """The sink a run gets when --stream is off: answers everything, does nothing.

    Callable so a mode can publish a run fact without checking first, and falsy so a
    mode that would rather skip the work entirely can. crawl_mode does the second and
    says why: with nobody watching, the crawler should take the path it always took
    rather than call into a sink per record.
    """

    def __call__(self, kind, data=None):
        return None

    publish = __call__

    def __bool__(self):
        return False


NO_STREAM = _NoStream()


def _stream_ring(args, mode):
    """How many events to keep for replay, given what each of them may weigh.

    --stream-buffer is counted in events because that is the unit a consumer reconnects
    in, and for metadata records the two defaults are a few megabytes. Page text is
    three orders of magnitude heavier, so when --stream-bodies is on the ring is sized
    by a byte budget instead: a replay depth of a few hundred findings is worth having,
    and a gigabyte of somebody else's markup pinned in RAM for four hours is not.

    Only crawl mode publishes bodies, so only crawl mode pays for them: a tree listing
    of a million-member archive should keep its full replay depth even when the flag was
    passed alongside it.
    """
    events = args.stream_buffer
    if mode != "crawl":
        return events
    if not getattr(args, "stream_bodies", False) or args.stream_body_bytes <= 0:
        return events
    affordable = max(100, STREAM_BODY_RING_BYTES // (2 * args.stream_body_bytes))
    if affordable >= events:
        return events
    logger.info(f"--stream-bodies: keeping {affordable} events for replay rather than "
                f"{events}, to hold the page text under "
                f"{STREAM_BODY_RING_BYTES // (1024 * 1024)} MiB.")
    return affordable


@contextlib.contextmanager
def streaming(args, mode, target, status=None):
    """Serve this run's events for as long as it lasts. Yields the sink to publish to.

    Yields NO_STREAM when --stream was not given, so every call site can pass the result
    straight into a producer's `sink=` without asking whether streaming is on.

    `status` is a callable the /status endpoint asks for the run's own counters; it is
    read while the run is in flight, so it must not block.
    """
    if not getattr(args, "stream", None):
        yield NO_STREAM
        return

    pkg = _import_eventstream()
    if pkg is None:
        # Same rule as a bind that cannot be honoured, below: somebody asked for this
        # run to be watched, and a run that quietly is not being watched is worse than
        # no run at all.
        sys.exit(1)

    ring = _stream_ring(args, mode)
    # A consumer's own queue is capped by the ring as well as by its own default. A
    # deeper queue than the ring means a lagging consumer pins events the ring has
    # already dropped -- which is memory outside the budget above, once per consumer,
    # and under --stream-bodies that is hundreds of megabytes of somebody else's markup.
    bus = pkg.EventBus(JOB_ID, mode, buffer=ring,
                       queue=min(pkg.DEFAULT_QUEUE, ring))
    try:
        server = pkg.StreamServer(bus, args.stream, token=args.stream_token,
                                  status=status).start()
    except (ValueError, OSError) as e:
        # A bind that fails is fatal rather than degraded: somebody asked for this run to
        # be watched, and a run that quietly is not being watched is worse than no run.
        logger.error(f"Cannot start the event stream: {e}")
        sys.exit(1)

    if args.stream_wait:
        logger.info(f"Waiting for a consumer on {server.url}/events "
                    f"(--stream-wait; Ctrl-C to give up) ...")
        server.wait_for_consumer()
        logger.info("Consumer attached; starting the run.")

    bus(pkg.RUN_START, {"mode": mode, "target": target, "job_id": JOB_ID,
                        "bodies": bool(getattr(args, "stream_bodies", False))})
    try:
        yield bus
    finally:
        if not bus.counts.get(pkg.RUN_STOP):
            # Every run ends with exactly one run.stop, including the ones that end by
            # exception -- a consumer that never sees one cannot tell "still working"
            # from "died an hour ago".
            bus(pkg.RUN_STOP, {"mode": mode, "stopped_because": "ended without a summary"})
        server.stop(STREAM_DRAIN)


# =========== CRAWL ("Index of /") ===========

# The crawler recursively harvests open directories on onion services. It lives in the
# crawler/ package next to this script and is imported lazily, for the same reason
# rvtree is: importing this module must stay side-effect free, and --farm has to keep
# working on a host that lacks aiohttp.
#
# It is also the only asyncio code in the project. Every other mode is threads over
# `requests`, and deliberately stays that way -- a crawl is thousands of small, slow,
# failure-prone requests, which is the one workload where an event loop plus a queue is
# clearly the better shape.
CRAWLER_DIR = "crawler"


def _import_crawler():
    """Import the crawler package, or explain what is missing."""
    try:
        import crawler
        return crawler
    except ImportError as e:
        logger.error(f"--mode crawl needs the crawler package and its dependencies "
                     f"({e}). Install them with 'pip install -e .' from the checkout "
                     f"(aiohttp, aiohttp-socks, beautifulsoup4, lxml).")
        return None


def crawl_out_dir():
    """Where this job's crawl artefacts go.

    One definition, because /status hands this path to a consumer as the thing to
    reconcile a gap against: a second derivation of it somewhere else is a promise that
    can quietly stop being true.
    """
    return os.path.join(crawler_default("CRAWLS_DIR", "crawls"), JOB_ID)


def crawler_default(name, fallback):
    """A default from crawler.config, without making --help depend on aiohttp.

    The argument parser is built before anything is imported lazily, and `--farm` must
    keep working on a host with no crawler dependencies installed -- so a missing
    package degrades to the documented fallback instead of killing --help.
    """
    try:
        from crawler import config as crawl_config
        return getattr(crawl_config, name, fallback)
    except ImportError:
        return fallback


def parse_socks_list(value):
    """Parse --socks 'host:port,host:port' into a list of endpoints."""
    endpoints = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        host, _, port = item.rpartition(":")
        if not host or not port.isdigit():
            raise argparse.ArgumentTypeError(f"not a host:port SOCKS endpoint: {item!r}")
        endpoints.append(item)
    if not endpoints:
        raise argparse.ArgumentTypeError("--socks needs at least one host:port")
    return endpoints


def parse_url(value):
    """Validate one --url. A scheme is required, and the reason is not pedantry.

    A bare 'example.onion/a.iso' would be handed to requests, which rejects it -- but not
    before resolve_proxies() may have picked it as the --external liveness target and
    published it to a hundred third-party proxies. Cheaper to refuse it here.
    """
    value = value.strip()
    if "://" not in value:
        raise argparse.ArgumentTypeError(
            f"not a URL, no scheme: {value!r} (try 'http://{value}')")
    return value


class _FullHelpAction(argparse.Action):
    """--full-help [MODE]: print the long-form reference and exit.

    An action rather than a flag checked in main(), so it fires during parsing and works
    on its own -- without --mode or --farm, which main() would otherwise insist on.
    """

    def __init__(self, option_strings, dest, **kwargs):
        kwargs.setdefault("nargs", "?")
        kwargs.setdefault("default", None)
        super().__init__(option_strings, dest, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        sys.stdout.write(fullhelp.render(values))
        parser.exit(0)


def resolve_crawl_endpoints(args, urls):
    """SOCKS5 endpoints for crawl mode.

    --socks wins outright and is checked but never second-guessed: it is how you point
    the crawler at a Tor daemon this script does not manage (a personal client on 9050,
    a remote SOCKS relay) without the farm tooling ever going near it.

    Otherwise this behaves like tree mode rather than like the download modes: a live
    probe of the farm range, then a single local client, then an error. The download
    modes' fallback to the *fixed* BASE_PORT..+19 list is a reasonable bet when a Docker
    setup might just be racing the probe, but a crawl issues thousands of requests and
    would spend all of them timing out on twenty endpoints that were never there.
    """
    if args.socks:
        live, dead = [], []
        for endpoint in args.socks:
            host, _, port = endpoint.rpartition(":")
            (live if _port_is_open(host, int(port)) else dead).append(endpoint)
        if dead:
            logger.warning(f"--socks endpoints not accepting connections: {', '.join(dead)}")
        if not live:
            logger.error("None of the --socks endpoints are reachable.")
        else:
            logger.info(f"Using {len(live)} explicitly configured SOCKS endpoint(s).")
        return live

    if args.external:
        return resolve_proxies(True, urls, test_url=args.test_url)

    live = discover_local_socks_ports(base_port=args.base_port, max_ports=args.count)
    if live:
        logger.info(f"Discovered {len(live)} live local Tor SOCKS port(s) "
                    f"({live[0]} - {live[-1]}).")
        return live

    host, port = LOCAL_TOR_SOCKS.rsplit(":", 1)
    if _port_is_open(host, int(port)):
        logger.warning(f"No farm on 127.0.0.1:{args.base_port}-"
                       f"{args.base_port + args.count - 1}; falling back to the local Tor "
                       f"client at {LOCAL_TOR_SOCKS}. One daemon still gives you "
                       f"--circuits-per-endpoint isolated circuits, but a farm gives you "
                       f"independent guards too: "
                       f"'sudo python3 OnionAccelerator.py --farm up --count 8'.")
        return [LOCAL_TOR_SOCKS]

    logger.error(f"No SOCKS5 proxy found: nothing on 127.0.0.1:{args.base_port}-"
                 f"{args.base_port + args.count - 1} and nothing on {LOCAL_TOR_SOCKS}. "
                 f"Start a farm with 'sudo python3 OnionAccelerator.py --farm up', pass "
                 f"--socks host:port, or pass --external.")
    return []


def list_profiles_mode(args):
    """--list-profiles: print the listing templates and exit. Returns an exit code.

    Deliberately reachable without a live target and without touching the network: the
    question "what does this thing already know how to read" is one you ask before you
    have a target, and one you should be able to ask on a laptop with no Tor running.
    """
    try:
        from crawler.listing.registry import describe, load_profiles
    except ImportError as e:
        logger.error(f"--list-profiles needs the crawler package ({e}). Install it with "
                     f"'pip install -e .' from the checkout.")
        return 1
    try:
        profiles = load_profiles(args.templates or [])
    except Exception as e:                            # a bad template names itself
        logger.error(f"Cannot load listing templates: {e}")
        return 1
    print(describe(profiles))
    print(f"\n{len(profiles)} template(s). Crawl with --profile NAME, or run --detect "
          f"against a target to be told which one fits.")
    return 0


def detect_mode(urls, proxies, args, sink=None):
    """--detect: one request per seed, then say what the target is. Returns an exit code."""
    pkg = _import_crawler()
    if pkg is None:
        return 1
    try:
        config = pkg.CrawlConfig(
            seeds=list(urls),
            circuits_per_endpoint=args.circuits_per_endpoint,
            max_page_bytes=args.max_page_bytes,
            connect_timeout=args.connect_timeout,
            read_timeout=args.read_timeout,
            total_timeout=args.total_timeout,
            allow_offsite=args.allow_offsite,
            profile=args.profile,
            templates=list(args.templates or []),
            out_dir="",
            job_id=JOB_ID,
        )
    except ValueError as e:
        logger.error(f"{e}")
        return 1

    try:
        results = pkg.detect_targets(config, proxies, load_user_agents())
    except ValueError as e:                           # an unknown --profile name
        logger.error(f"{e}")
        return 1

    # The command --detect prints has to be one that runs. A profile loaded from
    # --templates does not exist without it, so the flag has to travel with the advice.
    extra_flags = "".join(f" --templates {shlex.quote(d)}" for d in (args.templates or []))

    matched = 0
    for seed, findings in results:
        print()
        print(pkg.listing.render(findings, seed, extra_flags=extra_flags))
        matched += 1 if any(f.usable for f in findings) else 0
        if sink:
            sink("detect.result", {
                "seed": seed,
                "usable": any(f.usable for f in findings),
                "findings": [{"profile": f.profile.name, "score": round(f.score, 3),
                              "entries": f.entries, "directories": f.directories,
                              "files": f.files, "is_index": f.is_index,
                              "usable": f.usable, "probed": f.probed,
                              "error": f.error}
                             for f in findings],
            })
    if sink:
        sink("run.stop", {"mode": "detect", "seeds": len(results), "matched": matched,
                          "stopped_because": "finished"})
    return 0 if matched else 1


def crawl_mode(urls, proxies, args, sink=None):
    """Run one crawl, then optionally download everything it found. Returns an exit code."""
    pkg = _import_crawler()
    if pkg is None:
        return 1

    out_dir = crawl_out_dir()
    try:
        config = pkg.CrawlConfig(
            seeds=list(urls),
            max_depth=args.max_depth,
            order=args.order,
            workers=args.workers,
            circuits_per_endpoint=args.circuits_per_endpoint,
            per_host=args.per_host,
            retries=args.retries,
            max_pages=args.max_pages,
            time_budget=args.time_budget,
            max_page_bytes=args.max_page_bytes,
            connect_timeout=args.connect_timeout,
            read_timeout=args.read_timeout,
            total_timeout=args.total_timeout,
            allow_offsite=args.allow_offsite,
            include=pkg.CrawlConfig.compile_filter(args.include),
            exclude=pkg.CrawlConfig.compile_filter(args.exclude),
            switch_after=args.switch_after,
            download=args.download,
            resume=args.resume,
            profile=args.profile,
            templates=list(args.templates or []),
            stream_bodies=args.stream_bodies,
            stream_body_bytes=args.stream_body_bytes,
            out_dir=out_dir,
            job_id=JOB_ID,
        )
    except re.error as e:
        logger.error(f"Bad --include/--exclude regex: {e}")
        return 1

    try:
        # `sink or None` rather than the no-op sink: with no consumer the crawler should
        # take its original path and not pay a call per record.
        stats, file_urls = pkg.crawl(config, proxies, load_user_agents(),
                                     sink=sink or None)
    except ValueError as e:                           # an unknown --profile, or a bad template
        logger.error(f"{e}")
        return 1
    # Held until this function actually returns rather than published here: with
    # --download the process keeps pulling bytes for hours after the crawl stops, and a
    # consumer told the run ended while the job is still working cannot tell a finished
    # run from one that died.
    stop = {"mode": "crawl", "out_dir": os.path.abspath(out_dir),
            "stopped_because": stats["stopped_because"],
            "totals": stats["totals"], "elapsed_s": stats["elapsed_s"],
            "files": len(file_urls), "downloaded": 0}
    try:
        logger.info(f"Crawl manifest: {os.path.abspath(out_dir)} "
                    f"({len(file_urls)} file URL(s) in {pkg.config.URLS_FILE})")

        if not args.download:
            if file_urls:
                # --preserve-path is not optional advice here: a crawl manifest addresses
                # a whole tree, and without it every same-named file lands on one path.
                logger.info(f"Feed them to a download run with: cp "
                            f"{os.path.join(out_dir, pkg.config.URLS_FILE)} {URLS_FILE} && "
                            f"python3 OnionAccelerator.py --mode multi --preserve-path")
            return 0 if stats["totals"]["directories"] else 1

        if not file_urls:
            logger.warning("--download was given but the crawl found no files.")
            return 0

        # A separate, sequential phase on purpose: the download stack is threads over
        # `requests`, and running it alongside the event loop would put two unrelated
        # concurrency models on the same Tor daemons at the same time.
        logger.info(f"Downloading {len(file_urls)} discovered file(s) into "
                    f"{DOWNLOAD_DIR}/ ...")
        multi_download_mode(file_urls, proxies, retries=args.retries, preserve_path=True)
        stop["downloaded"] = len(file_urls)
        return 0
    finally:
        if sink:
            sink("run.stop", stop)


# =========== COMMAND LINE ===========

def build_parser():
    """The argument parser, built without side effects so tests can read it.

    Separate from main() for the reason rvtree's build_parser() is: a parser that can
    only be reached by running the program is a parser whose help nobody can test, and a
    reference that drifts is worse than none.
    """
    parser = argparse.ArgumentParser(
        description="OnionAccelerator: multi/partial download with Tor proxies, plus "
                    "speedtest & availability check, and a native Tor-instance farm.",
        epilog="Run --full-help for the per-mode reference: what each mode does, which "
               "of these flags actually reach it, and worked examples. "
               "--full-help MODE narrows it to one."
    )
    parser.add_argument("--mode", choices=list(fullhelp.MODES), default=None,
                        help="Mode: multi, partial, speedtest, tree, or crawl. 'tree' "
                             "lists/extracts members of a huge remote archive over the "
                             "proxy pool without downloading it; everything after '--' is "
                             "passed to rvtree. 'crawl' recursively harvests 'Index of /' "
                             "open directories starting from the seed URLs. "
                             "Mutually exclusive with --farm.")
    parser.add_argument("--full-help", action=_FullHelpAction, metavar="MODE",
                        choices=list(fullhelp.TOPICS),
                        help="Print the long-form per-mode reference and exit. Give a "
                             f"mode name ({', '.join(fullhelp.TOPICS)}) to print just "
                             f"that section.")
    parser.add_argument("--url", action="append", type=parse_url, default=None,
                        metavar="URL",
                        help="A target URL, given here instead of in URLs.txt. Repeat it "
                             "for several. When --url is given at least once it replaces "
                             "the file entirely: URLs.txt is not read and need not exist. "
                             "Under --mode tree it becomes rvtree's URL argument, so "
                             "'--url X -- extract p' runs 'extract X p'.")
    parser.add_argument("--farm", choices=["up", "status", "down", "destroy"], default=None,
                        help="Manage a native Tor-instance farm (tor-instance-create + "
                             "systemd, no Docker): 'up' creates/starts/bootstraps "
                             "instances, 'status' reports them, 'down' stops them, "
                             "'destroy' purges them. Only ever touches farm-owned "
                             f"'{TOR_INSTANCE_PREFIX}NN' instances. Mutually exclusive with --mode.")
    parser.add_argument("--count", type=int, default=MAX_WORKERS,
                        help=f"Number of farm instances for '--farm up' (default {MAX_WORKERS}).")
    parser.add_argument("--base-port", type=int, default=BASE_PORT,
                        help=f"First SOCKS port for the farm (default {BASE_PORT}); "
                             f"instance NN binds base_port+NN.")
    parser.add_argument("--bootstrap-timeout", type=int, default=BOOTSTRAP_TIMEOUT,
                        help=f"Seconds to wait for each instance to bootstrap on "
                             f"'--farm up' (default {BOOTSTRAP_TIMEOUT}).")
    parser.add_argument("--farm-tune", choices=list(FARM_TUNE_PROFILES), default=None,
                        help="torrc tuning for '--farm up' (default off). 'onion' adds "
                             "ExtendedErrors, so a crawl can tell a dead onion from a dead "
                             "circuit, no dormancy and warm circuits between jobs, and a "
                             "per-instance memory cap -- all rated Safe. 'onion-aggressive' "
                             "adds MaxCircuitDirtiness 1800, a linkability trade-off. "
                             "Instances whose torrc changes are reloaded, or restarted "
                             "one at a time if their SocksPort line changed.")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES,
                        help="Number of retries for download failures.")
    parser.add_argument("--preserve-path", action="store_true",
                        help=f"Mirror each URL's remote directory tree under "
                             f"{DOWNLOAD_DIR}/<host>/ instead of flattening every "
                             f"download to its basename. Use this for a crawl "
                             f"manifest: an open directory routinely holds a "
                             f"README.txt or Thumbs.db in every subdirectory, and the "
                             f"flat layout silently keeps only the last one. "
                             f"'--mode crawl --download' always preserves paths.")
    parser.add_argument("--external", action="store_true",
                        help=f"Fetch ip:port SOCKS5 proxies from the external list "
                             f"instead of local Docker Tor instances. The list is "
                             f"re-fetched every run (never persisted), capped at "
                             f"{MAX_EXTERNAL_PROXIES} proxies, and connectivity-checked.")
    parser.add_argument("--test-url", default=None,
                        help="URL used only to verify external-proxy liveness "
                             "(overrides the PROXY_TEST_URL config constant). Point "
                             "it at an endpoint you control/trust so real target URLs "
                             "are never revealed to proxies that get discarded. If "
                             "unset, a random one of your own targets is used instead.")

    crawl_opts = parser.add_argument_group(
        "crawl mode", "Options for --mode crawl (ignored by the other modes)."
    )
    crawl_opts.add_argument("--socks", type=parse_socks_list, default=None,
                            help="Comma-separated 'host:port' SOCKS5 endpoints to crawl "
                                 "through, e.g. '127.0.0.1:9050,127.0.0.1:9052'. "
                                 "Overrides farm discovery and --external. Use this to "
                                 "point the crawler at Tor daemons this script does not "
                                 "manage; the --farm tooling never touches them.")
    crawl_opts.add_argument("--max-depth", type=int,
                            default=crawler_default("DEFAULT_MAX_DEPTH", None),
                            help="How many directory levels below each seed to crawl. "
                                 "Default: unlimited -- crawl the whole tree, which is "
                                 "the point of the mode. Seeds are depth 0; pass a "
                                 "positive integer to cap it.")
    crawl_opts.add_argument("--order", choices=["bfs", "dfs"], default="bfs",
                            help="Traversal order: breadth-first (default) maps the whole "
                                 "tree shallow-first; depth-first finishes branches.")
    crawl_opts.add_argument("--switch-after", type=int, default=None,
                            help="Switch traversal order once N directories have been "
                                 "listed. Maps the shape of the tree breadth-first, then "
                                 "dives.")
    crawl_opts.add_argument("--workers", type=int, default=None,
                            help="Concurrent workers (default: one per circuit). Capped "
                                 "at the circuit count -- extra workers would only queue.")
    crawl_opts.add_argument("--circuits-per-endpoint", type=int,
                            default=crawler_default("DEFAULT_CIRCUITS_PER_ENDPOINT", 2),
                            help="Isolated Tor circuits to open per SOCKS endpoint "
                                 "(default 2). Each gets its own SOCKS credential, which "
                                 "is what makes them separate circuits rather than one "
                                 "shared one.")
    crawl_opts.add_argument("--per-host", type=int,
                            default=crawler_default("DEFAULT_PER_HOST", 8),
                            help="Maximum concurrent requests against any single target "
                                 "host (default 8). Onion services are usually one small "
                                 "process; past this they start refusing connections.")
    crawl_opts.add_argument("--max-pages", type=int, default=None,
                            help="Stop after listing this many directories. Default: "
                                 "unlimited -- list every directory under the seeds.")
    crawl_opts.add_argument("--time-budget", type=float, default=None,
                            help="Stop after this many seconds and write the report.")
    crawl_opts.add_argument("--max-page-bytes", type=int,
                            default=crawler_default("MAX_PAGE_BYTES", 16 * 1024 * 1024),
                            help="Abandon a body larger than this instead of parsing it "
                                 "and record the fetch as a failure (default 16 MiB). One "
                                 "directory of a leaked fileshare can be thousands of "
                                 "rows, so raise this rather than lose its subtree.")
    crawl_opts.add_argument("--connect-timeout", type=float,
                            default=crawler_default("CONNECT_TIMEOUT", 90.0),
                            help="Seconds to establish a connection, including the Tor "
                                 "rendezvous (default 90). Raise it for a slow onion: a "
                                 "rendezvous that needs longer than this is reported as a "
                                 "dead host when it is only a distant one.")
    crawl_opts.add_argument("--read-timeout", type=float,
                            default=crawler_default("READ_TIMEOUT", 180.0),
                            help="Seconds the server may go silent mid-response before the "
                                 "fetch is abandoned (default 180). This is the one to raise "
                                 "on a slow target: set below its normal response time, every "
                                 "listing times out and each miss costs a whole subtree.")
    crawl_opts.add_argument("--total-timeout", type=float,
                            default=crawler_default("TOTAL_TIMEOUT", 300.0),
                            help="Seconds for one whole request, connect and read together "
                                 "(default 300). Keep it above --connect-timeout plus "
                                 "--read-timeout or it caps them both.")
    crawl_opts.add_argument("--resume", metavar="JOB_DIR", default=None,
                            help="Continue a previous crawl from its output directory, "
                                 "e.g. crawls/20260908_110133_bfiyq8. Its frontier "
                                 "journal is replayed first: directories it finished are "
                                 "not fetched again, and the ones it discovered but never "
                                 "reached are queued, along with any subtree it gave up "
                                 "on. Use it when a target goes offline mid-crawl -- "
                                 "without it a restart re-walks the same shallow layers "
                                 "and never gets deeper. The new run writes its own "
                                 "journal, so resume from the newest directory each time.")
    crawl_opts.add_argument("--include", default=None,
                            help="Only crawl directory URLs matching this regex.")
    crawl_opts.add_argument("--exclude", default=None,
                            help="Never crawl directory URLs matching this regex.")
    crawl_opts.add_argument("--allow-offsite", action="store_true",
                            help="Follow links off the seed hosts. Off by default: on Tor "
                                 "this is how a directory walk becomes an unbounded crawl.")
    crawl_opts.add_argument("--download", action="store_true",
                            help="After crawling, download every discovered file through "
                                 "the same proxies, mirroring the remote directory tree "
                                 f"under {DOWNLOAD_DIR}/<host>/.")
    crawl_opts.add_argument("--profile", default=None, metavar="NAME",
                            help="Read every page with this listing template instead of "
                                 "detecting one per host. Required for API-driven targets "
                                 "(AList, WebDAV, h5ai): it is what lets the seed itself be "
                                 "requested in the target's own scheme. "
                                 "See --list-profiles and --detect.")
    crawl_opts.add_argument("--list-profiles", action="store_true",
                            help="Print the listing templates that are available and exit. "
                                 "'verified' ones are pinned by a fixture in the test suite; "
                                 "'unverified' ones were written from documented request "
                                 "shapes and want checking against a live target.")
    crawl_opts.add_argument("--detect", action="store_true",
                            help="Fetch each seed once, report which templates match it and "
                                 "what each one reads out of it, and print the --profile "
                                 "command to crawl with. Does not crawl. Cheap enough to be "
                                 "the first thing you run against an unknown target.")
    crawl_opts.add_argument("--templates", action="append", default=None, metavar="DIR",
                            help="Load extra listing templates from this directory (repeat "
                                 "for several). A template of the same name replaces the "
                                 "built-in one -- which is how a target-specific profile "
                                 "stays out of the repository.")

    stream_opts = parser.add_argument_group(
        "streaming API",
        "Mirror a running crawl, tree listing or detection to an external process "
        "(ignored by the download modes)."
    )
    stream_opts.add_argument("--stream", nargs="?", const=STREAM_BIND, default=None,
                             metavar="HOST:PORT",
                             help=f"Serve this run's findings as newline-delimited JSON "
                                  f"while it runs, for an external script to grep or "
                                  f"index. Default bind {STREAM_BIND}; a bare port works, "
                                  f"and port 0 picks a free one and logs it. Attach with: "
                                  f"curl -sN 'http://127.0.0.1:8787/events'. "
                                  f"See also /status, /schema and /health.")
    stream_opts.add_argument("--stream-token", default=None, metavar="TOKEN",
                             help="Require this bearer token on every stream request. "
                                  "Mandatory for any bind that is not loopback -- the "
                                  "stream carries target URLs and file names.")
    stream_opts.add_argument("--stream-buffer", type=int, default=STREAM_BUFFER,
                             metavar="N",
                             help=f"Events kept for replay, so a consumer that attaches "
                                  f"late or reconnects can ask for what it missed with "
                                  f"?since=SEQ (default {STREAM_BUFFER}).")
    stream_opts.add_argument("--stream-bodies", action="store_true",
                             help="Also publish the text of each fetched page, truncated, "
                                  "for hunts that must match inside a listing rather than "
                                  "in its file names. Crawl mode only. Never written to "
                                  "disk.")
    # The cap lives in crawler/config.py next to MAX_PAGE_BYTES, which is what it has to
    # stay proportionate to; the constant here is only the fallback for a host that
    # cannot import the crawler at all. Same bargain as --max-page-bytes above.
    body_bytes = crawler_default("STREAM_BODY_BYTES", STREAM_BODY_BYTES)
    stream_opts.add_argument("--stream-body-bytes", type=int, default=body_bytes,
                             metavar="N",
                             help=f"How much of each body --stream-bodies publishes "
                                  f"(default {body_bytes}). The event says whether "
                                  f"it was truncated.")
    stream_opts.add_argument("--stream-wait", action="store_true",
                             help="Do not start the run until a consumer has attached to "
                                  "/events. Use it when the keyword index has to see the "
                                  "first finding, not the first one after it woke up.")
    return parser


def resolve_urls(args):
    """The URLs for this run: --url if given, otherwise every line of URLs.txt.

    --url wins outright rather than adding to the file. Merging would mean a stale list
    left over from the last job silently rides along with the one URL you meant to fetch,
    and on Tor that is a lot of traffic you did not ask for.
    """
    if args.url:
        return list(args.url)

    if not os.path.exists(URLS_FILE):
        logger.error(f"{URLS_FILE} not found. Pass targets with --url instead, e.g. "
                     f"--url https://example.onion/file.bin")
        sys.exit(1)

    with open(URLS_FILE, "r", encoding="utf-8") as f:
        urls = [line.strip() for line in f if line.strip()]

    if not urls:
        logger.warning(f"{URLS_FILE} is empty. Nothing to do.")
        sys.exit(0)
    return urls


def inject_tree_url(parser, args, rv_args):
    """Put --url where rvtree expects its URL: as the positional after the subcommand.

    rvtree takes 'list URL', 'extract URL PATH' and 'probe URL', so appending would put
    extract's URL and PATH the wrong way round. Every rvtree option lives on the
    subcommands rather than the top-level parser, so the subcommand is always rv_args[0]
    and no searching is needed.
    """
    if not args.url:
        return rv_args

    if len(args.url) > 1:
        parser.error(f"--mode tree reads one archive per run; --url was given "
                     f"{len(args.url)} times")
    inline = rv_target_urls(rv_args)
    if inline:
        parser.error(f"URL given twice: --url {args.url[0]} and {inline[0]} after '--'")

    url = args.url[0]
    if not rv_args:
        # The common case deserves the short spelling: 'what is in this archive?'
        return ["list", url]
    if rv_args[0] not in TREE_SUBCOMMANDS:
        parser.error(f"--mode tree: the first argument after '--' must be one of "
                     f"{', '.join(TREE_SUBCOMMANDS)}, not {rv_args[0]!r}")
    return [rv_args[0], url] + rv_args[1:]


# =========== MAIN ===========

def main(argv=None):
    parser = build_parser()

    # Everything after '--' belongs to rvtree, so unknown arguments are collected rather
    # than rejected. Only tree mode may have any; for every other mode a stray argument
    # is still a typo and still an error.
    #
    # Parsing comes before setup_logging() so that --help and --full-help do not create a
    # timestamped log file for a run that never happens. Nothing below logs before it.
    args, rv_args = parser.parse_known_args(argv)

    setup_logging()
    logger.info(f"Starting OnionAccelerator with job_id={JOB_ID}")

    # argparse only consumes '--' when it has positionals to feed; this parser has none,
    # so the separator survives into the leftovers and would reach rvtree as a mode name.
    if "--" in rv_args:
        rv_args.remove("--")
    if rv_args and args.mode != "tree":
        parser.error(f"unrecognized arguments: {' '.join(rv_args)}")

    # Listing the templates answers a question about this program, not about a target, so
    # it runs before the mode check and never opens a socket.
    if args.list_profiles:
        sys.exit(list_profiles_mode(args))

    if bool(args.farm) == bool(args.mode):
        parser.error("specify exactly one of --farm or --mode")

    if args.farm and args.url:
        parser.error("--url has no meaning for --farm; it manages proxies, not targets")

    if args.farm and args.stream:
        # Refused rather than ignored, for the same reason a bind that cannot be honoured
        # is fatal: somebody asked for this to be watched, and quietly not watching it is
        # worse than not running it.
        parser.error("--stream has nothing to publish for --farm: it manages Tor "
                     "daemons and discovers nothing. It applies to --mode crawl, "
                     "--mode tree and --detect.")

    if args.farm_tune and args.farm != "up":
        parser.error("--farm-tune only applies to '--farm up', which is what writes the "
                     "torrc; '--farm status' shows the profile each instance runs.")

    # Farm management needs no URL list; run the action and exit.
    if args.farm:
        run_farm_action(args.farm, args.count, args.base_port, args.bootstrap_timeout,
                        tune=args.farm_tune or "off")
        logger.info("OnionAccelerator farm action finished.")
        return

    # Tree mode takes its target from --url or the passed-through arguments, never from
    # URLs.txt. Injection happens before the emptiness check, so '--url X' alone is a
    # complete command line, and before resolve_tree_endpoints(), so --external still has
    # a liveness target to fall back on.
    if args.mode == "tree":
        rv_args = inject_tree_url(parser, args, rv_args)
        if not rv_args:
            parser.error("--mode tree needs a target: either --url, e.g. "
                         "--mode tree --url https://example.onion/backup.rar, or rvtree "
                         "arguments after '--', e.g. -- list https://example.onion/b.rar")
        proxies = resolve_tree_endpoints(args, tree_urls(rv_args))
        if not proxies:
            sys.exit(1)
        target = rv_target_urls(rv_args) or tree_urls(rv_args)
        with streaming(args, "tree", target) as stream:
            code = tree_mode(rv_args, proxies, retries=args.retries, sink=stream)
        logger.info(f"OnionAccelerator tree mode finished (exit {code}).")
        sys.exit(code)

    if args.stream and args.mode in ("multi", "partial", "speedtest"):
        parser.error(f"--stream has nothing to publish in --mode {args.mode}: it mirrors "
                     f"what a run *discovers*, and these modes consume a URL list rather "
                     f"than produce one. It applies to --mode crawl, --mode tree and "
                     f"--detect.")

    urls = resolve_urls(args)
    source = "--url" if args.url else URLS_FILE
    logger.info(f"Mode={args.mode}, total URLs={len(urls)} (from {source}), "
                f"job_id={JOB_ID}")

    # Crawl resolves its own endpoints: --socks may override, and its fallback ladder
    # differs from the download modes' (see resolve_crawl_endpoints).
    if args.mode == "crawl":
        proxies = resolve_crawl_endpoints(args, urls)
        if not proxies:
            logger.error("No usable proxies available. Aborting.")
            sys.exit(1)
        logger.info(f"Using {len(proxies)} SOCKS endpoint(s) x "
                    f"{args.circuits_per_endpoint} circuit(s).")
        if args.detect:
            with streaming(args, "detect", urls) as stream:
                code = detect_mode(urls, proxies, args, sink=stream)
            logger.info(f"OnionAccelerator detection finished (exit {code}).")
            sys.exit(code)
        # /status answers with where this run's artefacts are going, so a consumer that
        # sees a gap knows which files to reconcile against. The live counters it also
        # wants are the stream's own, and the bus keeps those already.
        out_dir = crawl_out_dir()

        def status():
            return {"job_id": JOB_ID, "seeds": urls, "endpoints": len(proxies),
                    "out_dir": os.path.abspath(out_dir), "download": args.download}

        with streaming(args, "crawl", urls, status=status) as stream:
            code = crawl_mode(urls, proxies, args, sink=stream)
        logger.info(f"OnionAccelerator crawl mode finished (exit {code}).")
        sys.exit(code)

    proxies = resolve_proxies(args.external, urls, test_url=args.test_url)
    if not proxies:
        logger.error("No usable proxies available. Aborting.")
        sys.exit(1)
    logger.info(f"Using {len(proxies)} proxies "
                f"({'external list' if args.external else 'local docker'}).")

    if args.mode == "multi":
        multi_download_mode(urls, proxies, retries=args.retries,
                            preserve_path=args.preserve_path)
    elif args.mode == "partial":
        partial_download_mode(urls, proxies, retries=args.retries,
                              preserve_path=args.preserve_path)
    elif args.mode == "speedtest":
        speedtest_mode(urls, proxies)

    logger.info("OnionAccelerator finished successfully.")


# =========== RE-EXPORT FORWARDING ===========
# The download modes, the size probe and the farm moved to oacore/, but tests and older
# callers patch them here: `monkeypatch.setattr(oa, "_probe", ...)`. A plain re-export
# would quietly defeat that -- the patch would rebind this module's copy of the name
# while probe_remote_size() kept calling the one in oacore.sizeprobe, and the test would
# go on passing against the real network code it meant to replace. So a write to a
# re-exported name is forwarded to every oacore module that holds the same object
# under the same name, and monkeypatch's undo is a write too. tests/test_facade.py
# patches each forwarded name and checks the defining module sees it.
_MOVED_MODULES = (_config, _common, _sizeprobe, _download, _farm)


def _forwarding_table():
    table = {}
    for module in _MOVED_MODULES:
        for name, obj in vars(module).items():
            if name.startswith("__") or isinstance(obj, types.ModuleType):
                continue
            if name in globals() and globals()[name] is obj:
                table.setdefault(name, []).append(module)
    return table


_FORWARDED = _forwarding_table()


class _Facade(types.ModuleType):
    def __setattr__(self, name, value):
        for module in _FORWARDED.get(name, ()):
            setattr(module, name, value)
        super().__setattr__(name, value)

    def __delattr__(self, name):
        for module in _FORWARDED.get(name, ()):
            if name in vars(module):
                delattr(module, name)
        super().__delattr__(name)


sys.modules[__name__].__class__ = _Facade


if __name__ == "__main__":
    main()
