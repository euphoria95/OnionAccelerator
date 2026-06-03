#!/usr/bin/env python3
import argparse
import os
import sys
import time
import random
import string
import threading
import queue
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
            next(r.iter_content(SPEEDTEST_CHUNK_SIZE), None)
        return True
    except Exception as e:
        logger.debug(f"[proxy down] {proxy} via {test_url}: {e}")
        return False


def verify_proxies(candidates, urls, user_agents):
    """
    Check each candidate proxy in parallel by fetching a random URL from the
    list. Return only the proxies that pass the connectivity check.
    """
    logger.info(f"Verifying connectivity of {len(candidates)} proxies ...")
    working = []
    lock = threading.Lock()

    with tqdm(total=len(candidates), desc="Verifying proxies", unit="proxy") as pbar:
        def check(proxy):
            ok = verify_proxy(proxy, random.choice(urls), user_agents)
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


def resolve_proxies(use_external, urls):
    """
    Return the list of 'host:port' SOCKS5 endpoints to use for this run.

    Default: local Docker Tor instances on ports BASE_PORT..BASE_PORT+MAX_WORKERS-1.
    External (--external): fetched fresh from EXTERNAL_PROXIES_URL (never persisted),
    capped at MAX_EXTERNAL_PROXIES and filtered down to proxies that pass a live
    connectivity check.
    """
    if not use_external:
        return [f"127.0.0.1:{BASE_PORT + i}" for i in range(MAX_WORKERS)]

    candidates = fetch_external_proxies()
    if len(candidates) > MAX_EXTERNAL_PROXIES:
        candidates = random.sample(candidates, MAX_EXTERNAL_PROXIES)
        logger.info(f"Capped to {MAX_EXTERNAL_PROXIES} candidate proxies.")
    if not candidates:
        return []

    user_agents = load_user_agents()
    return verify_proxies(candidates, urls, user_agents)


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

    def worker_thread(thread_id):
        proxy = proxies[thread_id]
        while True:
            try:
                url = q.get_nowait()
            except queue.Empty:
                break
            # Inline retries — no re-queuing, which would deadlock if all workers
            # exit with an empty queue before the retried item can be picked up.
            for attempt in range(retries):
                success = download_file(url, proxy, user_agents, progress_queue=progress_queue)
                if success:
                    logger.info(f"[OK] {url} (proxy={proxy})")
                    break
                if attempt < retries - 1:
                    logger.warning(f"Retrying {url} ({attempt + 2}/{retries})")
            else:
                logger.error(f"[FAIL] Giving up on {url} after {retries} attempts.")
            q.task_done()

    # One worker per proxy, but never more workers than there are URLs to fetch
    # (extra proxies would just spawn threads that exit on an empty queue).
    n_workers = min(len(proxies), len(urls))
    threads = []
    for i in range(n_workers):
        t = threading.Thread(target=worker_thread, args=(i,))
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


def partial_download_file(url, proxies, user_agents):
    final_path = url_to_local_path(url, PARTIAL_DOWNLOAD_DIR)
    os.makedirs(os.path.dirname(final_path), exist_ok=True)

    # HEAD for size
    ua_head = random_user_agent(user_agents)
    size = get_content_length(url, proxies[0], ua_head)
    if not size or size < 1:
        logger.warning(f"No Content-Length for {url}, fallback to sequential.")
        return fallback_sequential_download(url, final_path, ua_head, proxies[0], progress_queue=None)

    # Cap chunks at the proxy count, and don't split finer than
    # MIN_PARTIAL_CHUNK_SIZE. Guarantees 1 <= n_chunks <= size, so chunk_size is
    # always >= 1 (no degenerate ranges) and tiny files aren't over-split.
    n_chunks = min(len(proxies), max(1, size // MIN_PARTIAL_CHUNK_SIZE))
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
        proxy = proxies[i]
        ua = random_user_agent(user_agents)
        part_file = f"{final_path}.part{i}"
        for attempt in range(DEFAULT_RETRIES):
            ok = partial_download_chunk_to_file(url, start, end, proxy, ua, progress_queue, part_file)
            if ok:
                chunk_paths[i] = part_file
                return
            if attempt < DEFAULT_RETRIES - 1:
                logger.warning(f"Chunk {i} retry {attempt + 2}/{DEFAULT_RETRIES}")

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

    for idx, url in enumerate(urls, start=1):
        logger.info(f"\n[{idx}/{len(urls)}] Partial: {url}")
        success = False
        attempts_left = retries
        while attempts_left > 0 and not success:
            success = partial_download_file(url, proxies, user_agents)
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


def check_url_availability(url, proxy, speed_bps=None):
    """
    HEAD request to see if available; if size known and speed known, estimate ETA.
    Returns (is_ok, size, eta_seconds).
    """
    proxies = make_proxies(proxy)
    try:
        r = requests.head(url, proxies=proxies, timeout=REQUEST_TIMEOUT, verify=False)
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
        is_ok, size, eta_sec = check_url_availability(u, proxy, speed_bps=avg_speed_bps)
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


# =========== MAIN ===========

def main():
    setup_logging()
    logger.info(f"Starting OnionAccelerator with job_id={JOB_ID}")

    parser = argparse.ArgumentParser(
        description="OnionAccelerator: multi/partial download with Tor proxies, plus speedtest & availability check."
    )
    parser.add_argument("--mode", choices=["multi", "partial", "speedtest"], required=True,
                        help="Download mode: multi, partial, or speedtest.")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES,
                        help="Number of retries for download failures.")
    parser.add_argument("--external", action="store_true",
                        help=f"Fetch ip:port SOCKS5 proxies from the external list "
                             f"instead of local Docker Tor instances. The list is "
                             f"re-fetched every run (never persisted), capped at "
                             f"{MAX_EXTERNAL_PROXIES} proxies, and connectivity-checked.")
    args = parser.parse_args()

    if not os.path.exists(URLS_FILE):
        logger.error(f"{URLS_FILE} not found.")
        sys.exit(1)

    with open(URLS_FILE, "r", encoding="utf-8") as f:
        urls = [line.strip() for line in f if line.strip()]

    if not urls:
        logger.warning(f"{URLS_FILE} is empty. Nothing to do.")
        sys.exit(0)

    logger.info(f"Mode={args.mode}, total URLs={len(urls)}, job_id={JOB_ID}")

    proxies = resolve_proxies(args.external, urls)
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
