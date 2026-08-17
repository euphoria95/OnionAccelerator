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
from email.message import Message
from typing import NamedTuple, Optional
from urllib.parse import urlparse, unquote

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


def url_to_local_path(url: str, base_dir: str, preserve_path: bool = False) -> str:
    """
    Derive a collision-free local path from a URL using hostname as a subdirectory.
    e.g. http://example.onion/file.zip -> base_dir/example.onion/file.zip

    With `preserve_path`, the URL's directories are mirrored under the hostname:
    http://example.onion/a/b/file.zip -> base_dir/example.onion/a/b/file.zip. The
    hostname alone is enough for a hand-written URL list, where two entries rarely
    share a basename, but not for a crawl: an open directory routinely holds a
    README.txt in every subdirectory, and flattening those would have each overwrite
    the last. Crawl mode passes True; every other caller keeps the flat layout.
    """
    parsed = urlparse(url)
    host = parsed.netloc.replace(":", "_") or "unknown"
    filename = os.path.basename(parsed.path) or "index.html"
    if not preserve_path:
        return os.path.join(base_dir, host, filename)

    # Sanitise every component: a path traversal in a URL served by a hostile onion
    # must not be able to write outside base_dir.
    parts = [_safe_path_component(unquote(p)) for p in parsed.path.split("/") if p]
    parts = [p for p in parts if p]
    if not parts:
        parts = ["index.html"]
    return os.path.join(base_dir, host, *parts)


def _safe_path_component(component: str) -> str:
    """One URL path segment, made safe to use as a filename."""
    if component in (".", ".."):
        return ""
    cleaned = re.sub(r'[\x00-\x1f/\\:*?"<>|]', "_", component).strip(". ")
    return cleaned[:120]


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

def download_file(url, proxy, user_agents, progress_queue=None, preserve_path=False):
    """
    Download a single file with requests, streaming, ignoring HTTPS cert.
    """
    proxies = make_proxies(proxy)
    ua = random_user_agent(user_agents)
    out_path = url_to_local_path(url, DOWNLOAD_DIR, preserve_path=preserve_path)
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


def multi_download_mode(urls, proxies, retries=DEFAULT_RETRIES, preserve_path=False):
    """Download every URL in parallel, one worker per proxy.

    `preserve_path` mirrors each URL's directories under downloads/<host>/ instead of
    flattening to the basename; crawl mode's --download phase needs it, because a
    crawled tree is full of same-named files in different directories.
    """
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
                success = download_file(url, proxy, user_agents, progress_queue=progress_queue,
                                        preserve_path=preserve_path)
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


# =========== REMOTE FILE SIZE PROBE ===========

# The lengths a response advertises only describe the file when the body is sent
# as-is. Under any other Content-Encoding they describe the *compressed* transfer,
# and feeding one of those to the range splitter would cut the file at the wrong
# offsets. See _size_from_response().
IDENTITY_ENCODINGS = {"", "identity"}


class SizeProbe(NamedTuple):
    """What a size probe learned about a URL.

    `size is None` means undetermined, which is not the same as unreachable:
    `reachable` says whether any probe got an answer out of the server at all.
    """
    size: Optional[int] = None
    reachable: bool = False
    source: str = ""
    error: Optional[str] = None


def _positive_int(value) -> Optional[int]:
    """A header value as a positive int, or None if it isn't one.

    Zero counts as undetermined on purpose: a HEAD-hostile server answering
    "Content-Length: 0" is far more common than a genuinely empty file, and
    treating it as unknown keeps the ladder walking instead of stopping there.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text.isdigit():
        return None
    n = int(text)
    return n if n > 0 else None


def _size_from_content_length(headers) -> Optional[int]:
    """Content-Length, which on a non-206 response is the size of the whole entity."""
    return _positive_int(headers.get("content-length"))


def _size_from_content_disposition(headers) -> Optional[int]:
    """The optional `size` parameter of Content-Disposition.

    `Message.get_param` is the stdlib's header parser, so quoting and the RFC 2231
    form come for free and, unlike a "size=(digits)" regex, it will not read the 99
    out of `filename="report-size=99.pdf"`. (`cgi.parse_header` would be the obvious
    alternative and is gone in Python 3.13.)
    """
    raw = headers.get("content-disposition")
    if not raw:
        return None
    msg = Message()
    msg["Content-Disposition"] = raw
    param = msg.get_param("size", header="content-disposition")
    if isinstance(param, tuple):    # RFC 2231 form: (charset, language, value)
        param = param[2]
    return _positive_int(param)


def _size_from_content_range(headers) -> Optional[int]:
    """The total after the slash in `Content-Range: bytes 0-0/1048576`.

    An unsatisfied range answers `bytes */0`, whose "*" is not a digit and is
    rejected by _positive_int.
    """
    return _positive_int(headers.get("content-range", "").rsplit("/", 1)[-1])


def _size_from_response(status, headers):
    """The size a single response advertises, as (size, source); (None, "") if none.

    A non-identity Content-Encoding disqualifies the whole response: every number it
    carries then describes the compressed transfer rather than the file, and a body
    that arrives encoded cannot be reassembled out of parallel ranges anyway.
    Answering "unknown" routes the file to fallback_sequential_download(), which
    handles it correctly, instead of letting the merge fail after every byte has
    already been pulled over Tor.

    On a 206 the Content-Length is the length of the returned range -- one byte for
    our probe -- so the entity size has to come from Content-Range instead. Reading
    Content-Length there would report every file as 1 byte.
    """
    encoding = headers.get("content-encoding", "").strip().lower()
    if encoding not in IDENTITY_ENCODINGS:
        logger.debug(f"Ignoring the sizes on a response sent with Content-Encoding: {encoding}")
        return None, ""
    if status == 206:
        size = _size_from_content_range(headers)
        if size:
            return size, "Content-Range"
    else:
        size = _size_from_content_length(headers)
        if size:
            return size, "Content-Length"
    size = _size_from_content_disposition(headers)
    if size:
        return size, "Content-Disposition size="
    return None, ""


def _probe(session, url, method, extra_headers=None, stream=False):
    """Issue one probe request and return (status, lower-cased headers).

    allow_redirects is passed explicitly because requests.head() defaults it to
    False, and a redirect is not a 4xx -- so an unguarded HEAD happily reports the
    redirect's empty body as the size of a file that downloads perfectly well.

    The body is never touched on the streaming probes: closing an unconsumed
    response tears the socket down instead of returning it to the pool, and that is
    what stops the payload from being downloaded. The close sits in a finally so it
    happens on every path out.
    """
    r = session.request(method, url, headers=extra_headers, stream=stream,
                        timeout=REQUEST_TIMEOUT, allow_redirects=True)
    try:
        return r.status_code, {k.lower(): v for k, v in r.headers.items()}
    finally:
        r.close()


def probe_remote_size(url, proxy, ua=None) -> SizeProbe:
    """Determine the size of `url` without downloading it.

    Four sources of evidence, tried in this order and stopping at the first that
    answers. They cost at most three requests, because the first two read the same
    response:

      1. Content-Length on a HEAD.
      2. The `size=` parameter of that same response's Content-Disposition -- free,
         since it shares the round trip.
      3. Content-Range on a one-byte `Range: bytes=0-0` GET. A 206 gives the total
         after the slash; a server that ignores the Range answers 200, and then its
         Content-Length is the whole entity.
      4. A plain streamed GET, aborted as soon as the headers are in. It earns its
         place because a server that rejects *any* Range header with 501 never gets
         an answer out of probe 3.

    One Session covers all of them, so the proxy is dialled once rather than three
    times, and its close() releases every socket on every exit path.
    """
    last_error = None
    reachable = False

    with requests.Session() as session:
        session.proxies.update(make_proxies(proxy))
        session.verify = False
        if ua:
            session.headers["User-Agent"] = ua

        probes = (
            ("HEAD", "HEAD", None, False),
            ("range GET", "GET", {"Range": "bytes=0-0"}, True),
            ("streamed GET", "GET", None, True),
        )
        for label, method, extra_headers, stream in probes:
            try:
                status, headers = _probe(session, url, method, extra_headers, stream)
            except requests.exceptions.ConnectionError as e:
                # Covers ProxyError and ConnectTimeout: the proxy or the endpoint is
                # unreachable, so the remaining probes would fail the same way on the
                # same proxy. Stop rather than spend another 2 x REQUEST_TIMEOUT.
                logger.debug(f"{label} for {url} (proxy={proxy}) could not connect: {e}")
                return SizeProbe(reachable=reachable, error=str(e))
            except requests.RequestException as e:
                # Read timeout, too many redirects, malformed response: this probe is
                # out, but another method may still get an answer.
                last_error = str(e)
                logger.debug(f"{label} for {url} (proxy={proxy}) failed: {e}")
                continue

            if status >= 400:
                # A 405 on HEAD or a 501/416 on Range is the very reason the later
                # probes exist, so it isn't an error -- just move on.
                last_error = f"HTTP {status}"
                logger.debug(f"{label} for {url} returned HTTP {status}, trying the next probe")
                continue

            reachable = True
            size, source = _size_from_response(status, headers)
            if size:
                logger.debug(f"{url}: size={size} B via {source} ({label})")
                return SizeProbe(size=size, reachable=True, source=source)

    logger.debug(f"No size for {url} after all probes (last error: {last_error})")
    return SizeProbe(reachable=reachable, error=last_error)


def get_remote_file_size(url, proxy, ua) -> Optional[int]:
    """The size of `url` in bytes, or None if no probe could determine it."""
    return probe_remote_size(url, proxy, ua).size


# =========== PARTIAL-DOWNLOAD ===========


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

    # Probe for the size on a single pooled proxy. A server that won't disclose one
    # is a server trait, not a proxy fault, so we don't mark the proxy dead here.
    ua_probe = random_user_agent(user_agents)
    size = get_remote_file_size(url, pool.acquire(), ua_probe)
    if not size or size < 1:
        logger.warning(f"Could not determine the size of {url}, fallback to sequential.")
        return fallback_sequential_download(url, final_path, ua_probe, pool.acquire(), progress_queue=None)

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
TREE_CIRCUITS_PER_ENDPOINT = 1
TREE_HEDGE = 3                 # copies of a small range raced across endpoints
LOCAL_TOR_SOCKS = "127.0.0.1:9150"   # a personal Tor client, used when no farm is up


def _import_rvtree():
    """Import rvtree from remote_viewer/, or explain why it could not be used."""
    here = os.path.dirname(os.path.abspath(__file__))
    pkg_dir = os.path.join(here, RVTREE_DIR)
    if os.path.isdir(os.path.join(pkg_dir, "rvtree")) and pkg_dir not in sys.path:
        sys.path.insert(0, pkg_dir)
    try:
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
    why --test-url exists: see the warning resolve_proxies() emits.
    """
    return [a for a in rv_args if "://" in a]


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


def tree_mode(rv_args, proxies, retries=DEFAULT_RETRIES):
    """Run one rvtree invocation over the resolved endpoints. Returns rvtree's exit code."""
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
    try:
        return rvtree_cli.main(rv_args, transport=transport)
    except Exception as e:
        logger.error(f"rvtree failed: {e}")
        return 1
    finally:
        transport.close()


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
                     f"({e}). Install them with 'pip install -r requirements.txt' "
                     f"(aiohttp, aiohttp-socks, beautifulsoup4, lxml).")
        return None


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


def crawl_mode(urls, proxies, args):
    """Run one crawl, then optionally download everything it found. Returns an exit code."""
    pkg = _import_crawler()
    if pkg is None:
        return 1

    out_dir = os.path.join(pkg.config.CRAWLS_DIR, JOB_ID)
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
            allow_offsite=args.allow_offsite,
            include=pkg.CrawlConfig.compile_filter(args.include),
            exclude=pkg.CrawlConfig.compile_filter(args.exclude),
            switch_after=args.switch_after,
            download=args.download,
            out_dir=out_dir,
            job_id=JOB_ID,
        )
    except re.error as e:
        logger.error(f"Bad --include/--exclude regex: {e}")
        return 1

    stats, file_urls = pkg.crawl(config, proxies, load_user_agents())
    logger.info(f"Crawl manifest: {os.path.abspath(out_dir)} "
                f"({len(file_urls)} file URL(s) in {pkg.config.URLS_FILE})")

    if not args.download:
        if file_urls:
            logger.info(f"Feed them to a download run with: cp "
                        f"{os.path.join(out_dir, pkg.config.URLS_FILE)} {URLS_FILE} && "
                        f"python3 OnionAccelerator.py --mode multi")
        return 0 if stats["totals"]["directories"] else 1

    if not file_urls:
        logger.warning("--download was given but the crawl found no files.")
        return 0

    # A separate, sequential phase on purpose: the download stack is threads over
    # `requests`, and running it alongside the event loop would put two unrelated
    # concurrency models on the same Tor daemons at the same time.
    logger.info(f"Downloading {len(file_urls)} discovered file(s) into {DOWNLOAD_DIR}/ ...")
    multi_download_mode(file_urls, proxies, retries=args.retries, preserve_path=True)
    return 0


# =========== MAIN ===========

def main():
    setup_logging()
    logger.info(f"Starting OnionAccelerator with job_id={JOB_ID}")

    parser = argparse.ArgumentParser(
        description="OnionAccelerator: multi/partial download with Tor proxies, plus "
                    "speedtest & availability check, and a native Tor-instance farm."
    )
    parser.add_argument("--mode", choices=["multi", "partial", "speedtest", "tree", "crawl"],
                        default=None,
                        help="Mode: multi, partial, speedtest, tree, or crawl. 'tree' "
                             "lists/extracts members of a huge remote archive over the "
                             "proxy pool without downloading it; everything after '--' is "
                             "passed to rvtree. 'crawl' recursively harvests 'Index of /' "
                             "open directories starting from the URLs in URLs.txt. "
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
                            default=crawler_default("MAX_PAGE_BYTES", 4 * 1024 * 1024),
                            help="Abandon a body larger than this instead of parsing it; "
                                 "the URL is recorded as a file (default 4 MiB).")
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

    # Everything after '--' belongs to rvtree, so unknown arguments are collected rather
    # than rejected. Only tree mode may have any; for every other mode a stray argument
    # is still a typo and still an error.
    args, rv_args = parser.parse_known_args()
    # argparse only consumes '--' when it has positionals to feed; this parser has none,
    # so the separator survives into the leftovers and would reach rvtree as a mode name.
    if "--" in rv_args:
        rv_args.remove("--")
    if rv_args and args.mode != "tree":
        parser.error(f"unrecognized arguments: {' '.join(rv_args)}")

    if bool(args.farm) == bool(args.mode):
        parser.error("specify exactly one of --farm or --mode")

    # Farm management needs no URL list; run the action and exit.
    if args.farm:
        run_farm_action(args.farm, args.count, args.base_port, args.bootstrap_timeout)
        logger.info("OnionAccelerator farm action finished.")
        return

    # Tree mode takes its target from the passed-through arguments, not from URLs.txt.
    if args.mode == "tree":
        if not rv_args:
            parser.error("--mode tree needs rvtree arguments after '--', e.g. "
                         "--mode tree -- list https://example.onion/backup.rar")
        proxies = resolve_tree_endpoints(args, tree_urls(rv_args))
        if not proxies:
            sys.exit(1)
        code = tree_mode(rv_args, proxies, retries=args.retries)
        logger.info(f"OnionAccelerator tree mode finished (exit {code}).")
        sys.exit(code)

    if not os.path.exists(URLS_FILE):
        logger.error(f"{URLS_FILE} not found.")
        sys.exit(1)


    with open(URLS_FILE, "r", encoding="utf-8") as f:
        urls = [line.strip() for line in f if line.strip()]

    if not urls:
        logger.warning(f"{URLS_FILE} is empty. Nothing to do.")
        sys.exit(0)

    logger.info(f"Mode={args.mode}, total URLs={len(urls)}, job_id={JOB_ID}")

    # Crawl resolves its own endpoints: --socks may override, and its fallback ladder
    # differs from the download modes' (see resolve_crawl_endpoints).
    if args.mode == "crawl":
        proxies = resolve_crawl_endpoints(args, urls)
        if not proxies:
            logger.error("No usable proxies available. Aborting.")
            sys.exit(1)
        logger.info(f"Using {len(proxies)} SOCKS endpoint(s) x "
                    f"{args.circuits_per_endpoint} circuit(s).")
        code = crawl_mode(urls, proxies, args)
        logger.info(f"OnionAccelerator crawl mode finished (exit {code}).")
        sys.exit(code)

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
