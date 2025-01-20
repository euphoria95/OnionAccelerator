#!/usr/bin/env python3
import argparse
import os
import sys
import time
import random
import string
import math
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

# =========== LOGGER & JOB_ID SETUP ============
def generate_job_id():
    """
    Generate a unique ID based on timestamp + random chars
    """
    ts = time.strftime("%Y%m%d_%H%M%S")
    rnd = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"{ts}_{rnd}"

JOB_ID = generate_job_id()

logger = logging.getLogger("OnionAccelerator")
logger.setLevel(logging.DEBUG)

os.makedirs(LOGS_DIR, exist_ok=True)
log_filename = os.path.join(LOGS_DIR, f"OnionAccelerator_{JOB_ID}.log")

fh = logging.FileHandler(log_filename)
fh.setLevel(logging.DEBUG)
fh_formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
fh.setFormatter(fh_formatter)
logger.addHandler(fh)

ch = logging.StreamHandler(sys.stdout)
ch.setLevel(logging.INFO)
ch_formatter = logging.Formatter('[%(levelname)s] %(message)s')
ch.setFormatter(ch_formatter)
logger.addHandler(ch)

logger.info(f"Starting OnionAccelerator with job_id={JOB_ID}")

# =========== UTILS ===========

def load_user_agents(filepath=USERAGENTS_FILE):
    """
    Load user-agent strings from a file. Fallback if missing.
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
            ua = line.strip()
            if ua:
                agents.append(ua)
    if not agents:
        agents = ["Mozilla/5.0 (Windows NT 10.0; Win64; x64)"]
    return agents

def random_user_agent(uas):
    return random.choice(uas)

def parse_filename(url: str) -> str:
    parsed = urlparse(url)
    filename = os.path.basename(parsed.path)
    if not filename:
        filename = "index.html"
    return filename

def make_proxies_for_port(port: int) -> dict:
    """
    Accept any cert (verify=False), resolve DNS via Tor (socks5h).
    """
    return {
        "http": f"socks5h://127.0.0.1:{port}",
        "https": f"socks5h://127.0.0.1:{port}",
    }

# =========== MULTI-DOWNLOAD ===========

def download_file(url, port, user_agents, progress_queue=None):
    """
    Download a single file with requests, streaming, ignoring HTTPS cert.
    """
    proxies = make_proxies_for_port(port)
    ua = random_user_agent(user_agents)
    filename = parse_filename(url)
    out_path = os.path.join(DOWNLOAD_DIR, filename)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

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
        logger.debug(f"[OK] {url} -> {out_path} (port={port})")
        return True
    except Exception as e:
        logger.error(f"[ERR] Download failed for {url} on port={port}: {e}")
        if os.path.exists(out_path):
            os.remove(out_path)
        return False

def multi_download_mode(urls, retries=DEFAULT_RETRIES):
    logger.info(f"multi_download_mode: {len(urls)} URLs, retries={retries}")
    user_agents = load_user_agents()
    ports = [BASE_PORT + i for i in range(MAX_WORKERS)]

    # queue of (url, attempts_left)
    q = queue.Queue()
    for u in urls:
        q.put((u, retries))

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
        port = ports[thread_id]
        while True:
            try:
                url, attempts_left = q.get_nowait()
            except queue.Empty:
                break
            success = download_file(url, port, user_agents, progress_queue=progress_queue)
            if not success:
                attempts_left -= 1
                if attempts_left > 0:
                    logger.warning(f"Retrying {url} (left={attempts_left})")
                    q.put((url, attempts_left))
                else:
                    logger.error(f"[FAIL] Giving up on {url} after {retries} attempts.")
            else:
                logger.info(f"[OK] {url} (port={port})")
            q.task_done()

    threads = []
    for i in range(MAX_WORKERS):
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

def get_content_length(url, port, ua):
    proxies = make_proxies_for_port(port)
    try:
        r = requests.head(url, proxies=proxies, timeout=REQUEST_TIMEOUT,
                          verify=False, headers={"User-Agent": ua})
        r.raise_for_status()
        cl = r.headers.get("Content-Length")
        if cl is not None:
            return int(cl)
    except Exception as e:
        logger.debug(f"HEAD request for {url} (port={port}) failed: {e}")
    return None

def partial_download_chunk_to_file(url, start, end, port, ua, progress_queue, part_path):
    proxies = make_proxies_for_port(port)
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
        logger.error(f"Chunk {start}-{end} failed on port={port}: {e}")
        if os.path.exists(part_path):
            os.remove(part_path)
        return False

def fallback_sequential_download(url, out_path, user_agent, progress_queue):
    proxies = make_proxies_for_port(BASE_PORT)
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

def partial_download_file(url, user_agents):
    filename = parse_filename(url)
    final_path = os.path.join(PARTIAL_DOWNLOAD_DIR, filename)
    os.makedirs(PARTIAL_DOWNLOAD_DIR, exist_ok=True)

    # HEAD for size
    port_for_head = BASE_PORT
    ua_head = random_user_agent(user_agents)
    size = get_content_length(url, port_for_head, ua_head)
    if not size or size < 1:
        logger.warning(f"No Content-Length for {url}, fallback to sequential.")
        return fallback_sequential_download(url, final_path, ua_head, progress_queue=None)

    logger.info(f"[PARTIAL] {url} => size={size} bytes")

    chunk_size = size // MAX_WORKERS
    ranges = []
    for i in range(MAX_WORKERS):
        start = i * chunk_size
        if i == MAX_WORKERS - 1:
            end = size - 1
        else:
            end = (start + chunk_size) - 1
        ranges.append((start, end))

    progress_queue = queue.Queue()
    stop_flag = threading.Event()

    def progress_manager():
        with tqdm(total=size, desc=f"Partial: {filename}", unit="B", unit_scale=True) as pbar:
            while not stop_flag.is_set() or not progress_queue.empty():
                try:
                    chunk_len = progress_queue.get(timeout=0.2)
                    pbar.update(chunk_len)
                except queue.Empty:
                    pass

    pm_thread = threading.Thread(target=progress_manager, daemon=True)
    pm_thread.start()

    chunk_paths = [None] * MAX_WORKERS
    def chunk_worker(i):
        start, end = ranges[i]
        port = BASE_PORT + i
        ua = random_user_agent(user_agents)
        part_file = os.path.join(PARTIAL_DOWNLOAD_DIR, f"{filename}.part{i}")
        chunk_paths[i] = part_file
        ok = partial_download_chunk_to_file(url, start, end, port, ua, progress_queue, part_file)
        if not ok:
            chunk_paths[i] = None

    threads = []
    for i in range(MAX_WORKERS):
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

def partial_download_mode(urls, retries=DEFAULT_RETRIES):
    logger.info(f"partial_download_mode: {len(urls)} URLs, retries={retries}")
    user_agents = load_user_agents()

    for idx, url in enumerate(urls, start=1):
        logger.info(f"\n[{idx}/{len(urls)}] Partial: {url}")
        success = False
        attempts_left = retries
        while attempts_left > 0 and not success:
            success = partial_download_file(url, user_agents)
            if not success:
                attempts_left -= 1
                if attempts_left > 0:
                    logger.warning(f"Retrying partial for {url}, left={attempts_left}")
                    time.sleep(2)
        if not success:
            logger.error(f"[FAIL] partial-download {url} after {retries} attempts.")

    logger.info("partial_download_mode: completed")

# =========== SPEEDTEST + availability check ===========

def test_proxy_speed(url: str, port: int, ua) -> dict:
    proxies = make_proxies_for_port(port)
    result = {
        "port": port,
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

def check_url_availability(url, speed_bps=None):
    """
    HEAD request to see if available; if size known and speed known, estimate ETA.
    Returns (is_ok, size, eta_seconds).
    """
    proxies = make_proxies_for_port(BASE_PORT)  # or random
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

def speedtest_mode(urls):
    if not urls:
        logger.warning("No URLs for speedtest.")
        return
    test_url = urls[0]
    logger.info(f"[Speedtest] Testing first URL: {test_url}")

    user_agents = load_user_agents()
    results = []
    for i in range(MAX_WORKERS):
        port = BASE_PORT + i
        ua = random_user_agent(user_agents)
        logger.info(f"Testing port={port} ...")
        res = test_proxy_speed(test_url, port, ua)
        results.append(res)

    # Summaries
    successful_speeds = []
    logger.info("\n=== SPEEDTEST SUMMARY ===")
    for r in results:
        if r["success"]:
            sp_kbps = r["speed_bps"] / 1024
            successful_speeds.append(r["speed_bps"])
            logger.info(f"✅ Port {r['port']} => downloaded={r['downloaded_bytes']} B, speed={sp_kbps:.2f} KB/s")
        else:
            logger.error(f"❌ Port {r['port']} => err={r['error']}")
    logger.info("[DONE] Speedtest for ports.\n")

    if successful_speeds:
        avg_speed_bps = sum(successful_speeds) / len(successful_speeds)
        logger.info(f"[INFO] Average speed among working ports: {avg_speed_bps/1024:.2f} KB/s")
    else:
        avg_speed_bps = None
        logger.warning("[WARN] No successful ports => no average speed.")

    # Now check HEAD for each URL in the list, estimate ETA
    logger.info("[INFO] Checking availability for all URLs, computing ETA if possible:")
    for u in urls:
        is_ok, size, eta_sec = check_url_availability(u, speed_bps=avg_speed_bps)
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
    parser = argparse.ArgumentParser(
        description="OnionAccelerator: multi/partial download with Tor proxies, plus speedtest & availability check."
    )
    parser.add_argument("--mode", choices=["multi","partial","speedtest"], required=True,
                        help="Download mode: multi, partial, or speedtest.")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES,
                        help="Number of retries for download failures.")
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

    if args.mode == "multi":
        multi_download_mode(urls, retries=args.retries)
    elif args.mode == "partial":
        partial_download_mode(urls, retries=args.retries)
    elif args.mode == "speedtest":
        speedtest_mode(urls)

    logger.info("OnionAccelerator finished successfully.")

if __name__ == "__main__":
    main()