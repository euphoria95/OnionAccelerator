#!/usr/bin/env python3
import argparse
import os
import re
import sys
import glob
import time
import socket
import shutil
import random
import string
import threading
import queue
import subprocess
from urllib.parse import urlparse

import requests
# Wyłączamy ostrzeżenia o certyfikatach
import requests.packages.urllib3
from requests.packages.urllib3.exceptions import InsecureRequestWarning
requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)

from tqdm import tqdm
import logging

# ================== GLOBAL CONFIG ==================
MAX_WORKERS = 20              # e.g. ports 5000..5019
BASE_PORT = 5000
CHUNK_SIZE = 1024 * 64        # 64 KB
MIN_PARTIAL_CHUNK_SIZE = 1024 * 1024  # 1 MB: don't split a file finer than this
REQUEST_TIMEOUT = 20          # seconds
USERAGENTS_FILE = "UserAgents.tsv"
URLS_FILE = "URLs.txt"

DOWNLOAD_DIR = "downloads"
PARTIAL_DOWNLOAD_DIR = "partials"
LOGS_DIR = "logs"
DEFAULT_RETRIES = 3

# Dla speedtest:
MAX_SPEEDTEST_BYTES = 5 * 1024 * 1024  # 5 MB
SPEEDTEST_CHUNK_SIZE = 1024 * 64

# External proxy list (--external): fetched fresh every run, never persisted.
EXTERNAL_PROXIES_URL = "https://raw.githubusercontent.com/euphoria95/external-tor-proxies/refs/heads/main/proxies.txt"
MAX_EXTERNAL_PROXIES = 100     # hard cap on threads/proxies for external mode
CONNECTIVITY_TIMEOUT = 15      # seconds, per-proxy liveness check

# Proxy connectivity check (--external): the URL hit to confirm a candidate proxy
# is alive. Point this at an endpoint you control or trust so that real target
# URLs are never revealed to proxies that end up discarded. Leave empty to fall
# back to a random URL from URLS_FILE (convenient, but leaks a target to every
# candidate proxy). Override per-run with --test-url.
PROXY_TEST_URL = ""            # e.g. "http://your-own-service.onion/ping"

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
PORT_PROBE_TIMEOUT = 0.3       # seconds, per-port local liveness probe

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


# =========== UTILS ===========

def load_user_agents(filepath=USERAGENTS_FILE):
    """
    Load user-agent strings from a TSV file (first column). Fallback if missing.
    """
    if not os.path.isfile(filepath):
        logger.warning(f"UserAgents file not found: {filepath}. Using fallback.")
        return [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Mozilla/5.0 (X11; Linux x86_64)"
        ]
    agents = []
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            # TSV: first column is the UA string, remaining columns are metadata
            parts = line.strip().split('\t')
            ua = parts[0].strip()
            if ua:
                agents.append(ua)
    if not agents:
        agents = ["Mozilla/5.0 (Windows NT 10.0; Win64; x64)"]
    return agents


def random_user_agent(uas):
    return random.choice(uas)


def url_to_local_path(url: str, base_dir: str) -> str:
    """
    Derive a collision-free local path from a URL using hostname as a subdirectory.
    e.g. http://example.onion/file.zip -> base_dir/example.onion/file.zip
    """
    parsed = urlparse(url)
    host = parsed.netloc.replace(":", "_") or "unknown"
    filename = os.path.basename(parsed.path) or "index.html"
    return os.path.join(base_dir, host, filename)


def make_proxies(proxy: str) -> dict:
    """
    Build a SOCKS5 proxy dict for a 'host:port' endpoint (local or remote).
    Accept any cert (verify=False), resolve DNS via Tor (socks5h).
    """
    return {
        "http": f"socks5h://{proxy}",
        "https": f"socks5h://{proxy}",
    }


# =========== LOCAL PORT DISCOVERY ===========

def _port_is_open(host, port, timeout=PORT_PROBE_TIMEOUT):
    """Return True if a TCP connect to host:port succeeds within `timeout`."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def discover_local_socks_ports(base_port=BASE_PORT, max_ports=MAX_WORKERS, host="127.0.0.1"):
    """
    Probe host:base_port .. host:base_port+max_ports-1 and return the
    'host:port' endpoints that actually accept a TCP connection.

    This makes local mode adapt to however many SOCKS proxies are really up —
    a native farm of any size (see --farm) or legacy Docker instances — instead
    of blindly assuming exactly MAX_WORKERS are listening.
    """
    live = []
    for i in range(max_ports):
        port = base_port + i
        if _port_is_open(host, port):
            live.append(f"{host}:{port}")
    return live


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


# =========== PROXY POOL ===========

class ProxyPool:
    """
    Thread-safe pool of 'host:port' SOCKS5 endpoints shared across a run.

    Instead of pinning a download to one proxy by index, callers acquire() a
    proxy per attempt and report the outcome. A failed attempt can retry on a
    *different* live proxy (mark_dead + acquire(exclude=...)), so a single dead
    endpoint no longer sinks a whole file. If every proxy is parked as dead the
    pool falls back to reusing them — a stale retry beats giving up — and any
    proxy that later succeeds is rehabilitated via mark_alive().
    """

    def __init__(self, proxies):
        self._proxies = list(proxies)
        self._lock = threading.Lock()
        self._idx = 0
        self._dead = set()

    def __len__(self):
        return len(self._proxies)

    def acquire(self, exclude=None):
        """Return a proxy, preferring live ones and avoiding `exclude` when possible."""
        with self._lock:
            n = len(self._proxies)
            # One full rotation looking for a live, non-excluded endpoint.
            for _ in range(n):
                proxy = self._proxies[self._idx % n]
                self._idx += 1
                if proxy not in self._dead and proxy != exclude:
                    return proxy
            # Everything is dead or excluded: hand back the next in rotation so a
            # transient outage doesn't permanently strand the download.
            proxy = self._proxies[self._idx % n]
            self._idx += 1
            return proxy

    def mark_dead(self, proxy):
        with self._lock:
            self._dead.add(proxy)

    def mark_alive(self, proxy):
        with self._lock:
            self._dead.discard(proxy)


# =========== MULTI-DOWNLOAD ===========

def download_file(url, proxy, user_agents, progress_queue=None):
    """
    Download a single file with requests, streaming, ignoring HTTPS cert.
    """
    proxies = make_proxies(proxy)
    ua = random_user_agent(user_agents)
    out_path = url_to_local_path(url, DOWNLOAD_DIR)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    try:
        with requests.get(url, proxies=proxies, timeout=REQUEST_TIMEOUT,
                          verify=False, stream=True, headers={"User-Agent": ua}) as r:
            r.raise_for_status()
            with open(out_path, "wb") as f:
                for chunk in r.iter_content(CHUNK_SIZE):
                    if chunk:
                        f.write(chunk)
                        if progress_queue:
                            progress_queue.put(len(chunk))
        logger.debug(f"[OK] {url} -> {out_path} (proxy={proxy})")
        return True
    except Exception as e:
        logger.error(f"[ERR] Download failed for {url} on proxy={proxy}: {e}")
        if os.path.exists(out_path):
            os.remove(out_path)
        return False


def multi_download_mode(urls, proxies, retries=DEFAULT_RETRIES):
    logger.info(f"multi_download_mode: {len(urls)} URLs, {len(proxies)} proxies, retries={retries}")
    user_agents = load_user_agents()
    pool = ProxyPool(proxies)

    q = queue.Queue()
    for u in urls:
        q.put(u)

    progress_queue = queue.Queue()
    stop_flag = threading.Event()

    def progress_manager():
        with tqdm(desc="Multi-Download", unit="B", unit_scale=True, total=None) as pbar:
            while not stop_flag.is_set() or not progress_queue.empty():
                try:
                    chunk_size = progress_queue.get(timeout=0.2)
                    pbar.update(chunk_size)
                except queue.Empty:
                    pass

    pm_thread = threading.Thread(target=progress_manager, daemon=True)
    pm_thread.start()

    def worker_thread():
        while True:
            try:
                url = q.get_nowait()
            except queue.Empty:
                break
            # Inline retries — no re-queuing, which would deadlock if all workers
            # exit with an empty queue before the retried item can be picked up.
            # Each attempt draws a fresh proxy from the pool so a dead endpoint
            # doesn't doom the URL.
            last = None
            for attempt in range(retries):
                proxy = pool.acquire(exclude=last)
                success = download_file(url, proxy, user_agents, progress_queue=progress_queue)
                if success:
                    pool.mark_alive(proxy)
                    logger.info(f"[OK] {url} (proxy={proxy})")
                    break
                pool.mark_dead(proxy)
                last = proxy
                if attempt < retries - 1:
                    logger.warning(f"Retrying {url} ({attempt + 2}/{retries}) on a different proxy")
            else:
                logger.error(f"[FAIL] Giving up on {url} after {retries} attempts.")
            q.task_done()

    # Bound concurrency by the pool size (don't oversubscribe a single proxy) and
    # by the URL count (extra workers would just exit on an empty queue).
    n_workers = min(len(pool), len(urls))
    threads = []
    for _ in range(n_workers):
        t = threading.Thread(target=worker_thread)
        t.start()
        threads.append(t)

    q.join()
    for t in threads:
        t.join()

    stop_flag.set()
    pm_thread.join()
    logger.info("multi_download_mode: completed")


# =========== PARTIAL-DOWNLOAD ===========

def get_content_length(url, proxy, ua):
    proxies = make_proxies(proxy)
    try:
        r = requests.head(url, proxies=proxies, timeout=REQUEST_TIMEOUT,
                          verify=False, headers={"User-Agent": ua})
        r.raise_for_status()
        cl = r.headers.get("Content-Length")
        if cl is not None:
            return int(cl)
    except Exception as e:
        logger.debug(f"HEAD request for {url} (proxy={proxy}) failed: {e}")
    return None


def partial_download_chunk_to_file(url, start, end, proxy, ua, progress_queue, part_path):
    proxies = make_proxies(proxy)
    headers = {
        "User-Agent": ua,
        "Range": f"bytes={start}-{end}"
    }
    try:
        with requests.get(url, proxies=proxies, timeout=REQUEST_TIMEOUT,
                          verify=False, stream=True, headers=headers) as r:
            r.raise_for_status()
            with open(part_path, "wb") as f:
                for chunk in r.iter_content(CHUNK_SIZE):
                    if chunk:
                        f.write(chunk)
                        progress_queue.put(len(chunk))
        return True
    except Exception as e:
        logger.error(f"Chunk {start}-{end} failed on proxy={proxy}: {e}")
        if os.path.exists(part_path):
            os.remove(part_path)
        return False


def fallback_sequential_download(url, out_path, user_agent, proxy, progress_queue):
    proxies = make_proxies(proxy)
    try:
        with requests.get(url, proxies=proxies, timeout=REQUEST_TIMEOUT,
                          verify=False, stream=True, headers={"User-Agent": user_agent}) as r:
            r.raise_for_status()
            total_downloaded = 0
            with open(out_path, "wb") as f, tqdm(
                desc=f"Fallback seq: {os.path.basename(out_path)}",
                total=None, unit="B", unit_scale=True
            ) as pbar:
                for chunk in r.iter_content(CHUNK_SIZE):
                    if chunk:
                        f.write(chunk)
                        chunk_len = len(chunk)
                        total_downloaded += chunk_len
                        pbar.update(chunk_len)
                        if progress_queue:
                            progress_queue.put(chunk_len)
        logger.info(f"[SEQUENTIAL OK] {url} -> {out_path}, size={total_downloaded} B")
        return True
    except Exception as e:
        logger.error(f"Fallback sequential download failed for {url}: {e}")
        if os.path.exists(out_path):
            os.remove(out_path)
        return False


def partial_download_file(url, pool, user_agents):
    final_path = url_to_local_path(url, PARTIAL_DOWNLOAD_DIR)
    os.makedirs(os.path.dirname(final_path), exist_ok=True)

    # HEAD for size, on a single pooled proxy. A missing Content-Length is a
    # server trait, not a proxy fault, so we don't mark the proxy dead here.
    ua_head = random_user_agent(user_agents)
    size = get_content_length(url, pool.acquire(), ua_head)
    if not size or size < 1:
        logger.warning(f"No Content-Length for {url}, fallback to sequential.")
        return fallback_sequential_download(url, final_path, ua_head, pool.acquire(), progress_queue=None)

    # Cap chunks at the proxy count, and don't split finer than
    # MIN_PARTIAL_CHUNK_SIZE. Guarantees 1 <= n_chunks <= size, so chunk_size is
    # always >= 1 (no degenerate ranges) and tiny files aren't over-split.
    n_chunks = min(len(pool), max(1, size // MIN_PARTIAL_CHUNK_SIZE))
    logger.info(f"[PARTIAL] {url} => size={size} bytes, {n_chunks} chunk(s)")

    chunk_size = size // n_chunks
    ranges = []
    for i in range(n_chunks):
        start = i * chunk_size
        end = size - 1 if i == n_chunks - 1 else start + chunk_size - 1
        ranges.append((start, end))

    progress_queue = queue.Queue()
    stop_flag = threading.Event()

    def progress_manager():
        with tqdm(total=size, desc=f"Partial: {os.path.basename(final_path)}", unit="B", unit_scale=True) as pbar:
            while not stop_flag.is_set() or not progress_queue.empty():
                try:
                    chunk_len = progress_queue.get(timeout=0.2)
                    pbar.update(chunk_len)
                except queue.Empty:
                    pass

    pm_thread = threading.Thread(target=progress_manager, daemon=True)
    pm_thread.start()

    chunk_paths = [None] * n_chunks

    def chunk_worker(i):
        start, end = ranges[i]
        part_file = f"{final_path}.part{i}"
        last = None
        for attempt in range(DEFAULT_RETRIES):
            # Draw a fresh proxy each attempt so a dead endpoint doesn't fail the
            # whole file — the chunk simply retries through a different proxy.
            proxy = pool.acquire(exclude=last)
            ua = random_user_agent(user_agents)
            ok = partial_download_chunk_to_file(url, start, end, proxy, ua, progress_queue, part_file)
            if ok:
                pool.mark_alive(proxy)
                chunk_paths[i] = part_file
                return
            pool.mark_dead(proxy)
            last = proxy
            if attempt < DEFAULT_RETRIES - 1:
                logger.warning(f"Chunk {i} retry {attempt + 2}/{DEFAULT_RETRIES} on a different proxy")

    threads = []
    for i in range(n_chunks):
        t = threading.Thread(target=chunk_worker, args=(i,))
        t.start()
        threads.append(t)

    for t in threads:
        t.join()

    stop_flag.set()
    pm_thread.join()

    if any(cp is None for cp in chunk_paths):
        logger.error(f"[FAIL] Some chunk failed for {url}. Removing partials.")
        for cp in chunk_paths:
            if cp and os.path.exists(cp):
                os.remove(cp)
        if os.path.exists(final_path):
            os.remove(final_path)
        return False

    total_merged = 0
    try:
        with open(final_path, "wb") as outf:
            for cp in chunk_paths:
                with open(cp, "rb") as cf:
                    data = cf.read()
                    outf.write(data)
                    total_merged += len(data)
    except Exception as e:
        logger.error(f"Merge failed: {e}")
        return False

    for cp in chunk_paths:
        if os.path.exists(cp):
            os.remove(cp)

    if total_merged != size:
        logger.error(f"Merged size mismatch: {total_merged} != {size}, removing final.")
        if os.path.exists(final_path):
            os.remove(final_path)
        return False

    logger.info(f"[OK] Partial done: {final_path} ({total_merged} bytes)")
    return True


def partial_download_mode(urls, proxies, retries=DEFAULT_RETRIES):
    logger.info(f"partial_download_mode: {len(urls)} URLs, {len(proxies)} proxies, retries={retries}")
    user_agents = load_user_agents()
    pool = ProxyPool(proxies)

    for idx, url in enumerate(urls, start=1):
        logger.info(f"\n[{idx}/{len(urls)}] Partial: {url}")
        success = False
        attempts_left = retries
        while attempts_left > 0 and not success:
            success = partial_download_file(url, pool, user_agents)
            if not success:
                attempts_left -= 1
                if attempts_left > 0:
                    logger.warning(f"Retrying partial for {url}, left={attempts_left}")
                    time.sleep(2)
        if not success:
            logger.error(f"[FAIL] partial-download {url} after {retries} attempts.")

    logger.info("partial_download_mode: completed")


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
    HEAD request to see if available; if size known and speed known, estimate ETA.
    Returns (is_ok, size, eta_seconds).
    """
    proxies = make_proxies(proxy)
    headers = {"User-Agent": ua} if ua else {}
    try:
        r = requests.head(url, proxies=proxies, timeout=REQUEST_TIMEOUT,
                          verify=False, headers=headers)
        r.raise_for_status()
    except Exception as e:
        logger.error(f"URL not available: {url}, err={e}")
        return (False, None, None)
    cl_str = r.headers.get("Content-Length")
    if not cl_str:
        logger.info(f"URL available but no size info: {url}")
        return (True, None, None)
    try:
        size = int(cl_str)
    except ValueError:
        size = None

    eta_sec = None
    if size and speed_bps and speed_bps > 0:
        eta_sec = size / speed_bps

    return (True, size, eta_sec)


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


def render_torrc(name, port):
    """Client-only torrc for a farm instance bound to 127.0.0.1:<port>."""
    return (
        "# MANAGED BY OnionAccelerator (--farm). Regenerated on every 'up'; edits are lost.\n"
        f"# Farm instance {name}: client-only SOCKS proxy, no relay descriptor published.\n"
        f"SocksPort 127.0.0.1:{port}\n"
        "ClientOnly 1\n"
        "AvoidDiskWrites 1\n"
        "SafeLogging 1\n"
        f"Log notice file {TOR_LIB_DIR}/{name}/notices.log\n"
    )


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


def farm_up(count, base_port, timeout=BOOTSTRAP_TIMEOUT):
    farm_preflight()
    if count > MAX_WORKERS:
        logger.warning(f"count={count} exceeds MAX_WORKERS={MAX_WORKERS}; "
                       f"the download modes will only use the first {MAX_WORKERS}.")
    logger.info(f"Bringing up {count} Tor instance(s) on 127.0.0.1:{base_port}-{base_port + count - 1}")
    names = []
    for i in range(count):
        name = assert_farm_name(farm_instance_name(i))
        port = base_port + i
        inst_dir = os.path.join(TOR_INSTANCES_DIR, name)
        if not os.path.isdir(inst_dir):
            logger.info(f"[{name}] creating instance")
            _run(["tor-instance-create", name])
        else:
            logger.info(f"[{name}] already exists, refreshing config")
        _write_root_file(os.path.join(inst_dir, "torrc"), render_torrc(name, port))
        _run(["systemctl", "enable", "--now", f"tor@{name}"])
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
        mark = "✅" if (state == "active" and listening and pct >= 100) else "⚠️"
        logger.info(f"{mark} {name}: systemd={state}, socks=127.0.0.1:{port} "
                    f"({'listening' if listening else 'down'}), bootstrap={pct}%")


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


def run_farm_action(action, count, base_port, timeout):
    if action == "up":
        farm_up(count, base_port, timeout=timeout)
    elif action == "status":
        farm_status(base_port)
    elif action == "down":
        farm_down()
    elif action == "destroy":
        farm_destroy()


# =========== MAIN ===========

def main():
    setup_logging()
    logger.info(f"Starting OnionAccelerator with job_id={JOB_ID}")

    parser = argparse.ArgumentParser(
        description="OnionAccelerator: multi/partial download with Tor proxies, plus "
                    "speedtest & availability check, and a native Tor-instance farm."
    )
    parser.add_argument("--mode", choices=["multi", "partial", "speedtest"], default=None,
                        help="Download mode: multi, partial, or speedtest. "
                             "Mutually exclusive with --farm.")
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
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES,
                        help="Number of retries for download failures.")
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
                             "unset, a random URL from URLs.txt is used instead.")
    args = parser.parse_args()

    if bool(args.farm) == bool(args.mode):
        parser.error("specify exactly one of --farm or --mode")

    # Farm management needs no URL list; run the action and exit.
    if args.farm:
        run_farm_action(args.farm, args.count, args.base_port, args.bootstrap_timeout)
        logger.info("OnionAccelerator farm action finished.")
        return

    if not os.path.exists(URLS_FILE):
        logger.error(f"{URLS_FILE} not found.")
        sys.exit(1)

    with open(URLS_FILE, "r", encoding="utf-8") as f:
        urls = [line.strip() for line in f if line.strip()]

    if not urls:
        logger.warning(f"{URLS_FILE} is empty. Nothing to do.")
        sys.exit(0)

    logger.info(f"Mode={args.mode}, total URLs={len(urls)}, job_id={JOB_ID}")

    proxies = resolve_proxies(args.external, urls, test_url=args.test_url)
    if not proxies:
        logger.error("No usable proxies available. Aborting.")
        sys.exit(1)
    logger.info(f"Using {len(proxies)} proxies "
                f"({'external list' if args.external else 'local docker'}).")

    if args.mode == "multi":
        multi_download_mode(urls, proxies, retries=args.retries)
    elif args.mode == "partial":
        partial_download_mode(urls, proxies, retries=args.retries)
    elif args.mode == "speedtest":
        speedtest_mode(urls, proxies)

    logger.info("OnionAccelerator finished successfully.")


if __name__ == "__main__":
    main()
