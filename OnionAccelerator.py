#!/usr/bin/env python3
import argparse
import os
import sys
import math
import time
import random
import threading
import queue
from urllib.parse import urlparse

import requests
from tqdm import tqdm

# ==================== CONFIG ======================
MAX_WORKERS = 20             # number of threads / ports, i.e. 5000..5019
BASE_PORT = 5000
CHUNK_SIZE = 1024 * 64       # 64 KB per read
REQUEST_TIMEOUT = 20         # seconds
USERAGENTS_FILE = "UserAgents.tsv"
URLS_FILE = "URLs.txt"

# Download directories
DOWNLOAD_DIR = "downloads"         # for multi-download
PARTIAL_DOWNLOAD_DIR = "partials"  # for partial-download

# Retry logic
DEFAULT_RETRIES = 3

# Speedtest config
MAX_SPEEDTEST_BYTES = 5 * 1024 * 1024  # 5 MB limit for speedtest
SPEEDTEST_CHUNK_SIZE = 1024 * 64

# ================== UTILITIES =====================

def load_user_agents(filepath=USERAGENTS_FILE):
    """
    Loads user-agent strings from a TSV/text file (one per line).
    If the file doesn't exist or is empty, returns a default list.
    """
    if not os.path.isfile(filepath):
        print(f"[WARN] UserAgents file not found: {filepath}. Using a fallback list.")
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
    """
    Returns one random user agent from the given list.
    """
    return random.choice(uas)

def parse_filename(url: str) -> str:
    """
    Returns the last segment of the URL path as filename.
    If empty, returns 'index.html'.
    """
    parsed = urlparse(url)
    filename = os.path.basename(parsed.path)
    if not filename:
        filename = "index.html"
    return filename

def make_proxies_for_port(port: int) -> dict:
    """
    Returns a dict suitable for requests to use socks5h on given port.
    """
    return {
        "http": f"socks5h://127.0.0.1:{port}",
        "https": f"socks5h://127.0.0.1:{port}",
    }

# =======================================================
#                  MULTI-DOWNLOAD
# =======================================================

def download_file(url, port, user_agents, progress_queue=None):
    """
    Downloads ONE file from url using the assigned port, picking a random user-agent.
    If 'progress_queue' is provided, each chunk read will push len(chunk) to that queue,
    so a manager thread can update a single TQDM aggregator.

    Returns True if success, False otherwise.
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
                            progress_queue.put(len(chunk))  # notify aggregator
        return True
    except Exception as e:
        # If partial file was created, remove it to avoid confusion
        if os.path.exists(out_path):
            os.remove(out_path)
        return False

def multi_download_mode(urls, retries=DEFAULT_RETRIES):
    """
    Multi-download mode using up to MAX_WORKERS parallel threads.
    We have a global aggregator TQDM progress bar that sums all bytes downloaded.
    Each thread uses a dedicated port: 5000 + thread_index.
    If a download fails, we decrement attempts_left and re-queue if attempts_left>0.
    """
    user_agents = load_user_agents()
    ports = [BASE_PORT + i for i in range(MAX_WORKERS)]

    # Build a queue of (url, attempts_left)
    q = queue.Queue()
    for u in urls:
        q.put((u, retries))

    # This queue will gather the number of bytes for each chunk read
    progress_queue = queue.Queue()
    progress_stop_flag = threading.Event()

    # A manager thread that updates a single TQDM bar
    def progress_manager():
        total_downloaded = 0
        with tqdm(desc="Multi-Download", unit="B", unit_scale=True, total=None) as pbar:
            while not progress_stop_flag.is_set() or not progress_queue.empty():
                try:
                    chunk_size = progress_queue.get(timeout=0.2)
                    total_downloaded += chunk_size
                    pbar.update(chunk_size)
                except queue.Empty:
                    pass

    pm_thread = threading.Thread(target=progress_manager, daemon=True)
    pm_thread.start()

    def worker_thread(thread_id: int):
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
                    q.put((url, attempts_left))
                else:
                    print(f"[FAIL] Giving up on {url} after {retries} attempts.")
            else:
                print(f"[OK] {url} (port={port})")
            q.task_done()

    threads = []
    for i in range(MAX_WORKERS):
        t = threading.Thread(target=worker_thread, args=(i,))
        t.start()
        threads.append(t)

    q.join()
    for t in threads:
        t.join()

    # Stop progress manager
    progress_stop_flag.set()
    pm_thread.join()

# =======================================================
#                  PARTIAL-DOWNLOAD
# =======================================================

def get_content_length(url, port, ua):
    """
    Uses HEAD request to get Content-Length for partial download.
    Returns an integer or None if not found/error.
    """
    proxies = make_proxies_for_port(port)
    try:
        r = requests.head(url, proxies=proxies, timeout=REQUEST_TIMEOUT,
                          verify=False, headers={"User-Agent": ua})
        r.raise_for_status()
        cl = r.headers.get("Content-Length")
        if cl is not None:
            return int(cl)
    except Exception:
        pass
    return None

def partial_download_chunk_to_file(url, start, end, port, ua, progress_queue, part_path):
    """
    Downloads a chunk [start, end] from 'url' using 'port'.
    Writes data to 'part_path' file on disk, instead of in-memory.
    For each chunk read, pushes len(chunk) to 'progress_queue'.

    Returns True if success, False otherwise.
    """
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
        if os.path.exists(part_path):
            os.remove(part_path)  # remove partial chunk
        return False

def partial_download_file(url, user_agents):
    """
    Partial-downloads a single file from URL, splitted into MAX_WORKERS chunks.
    Each chunk is downloaded in a separate thread & port, but chunk data is
    written to a .partX file on disk (temp). Then we merge them.

    Returns True if success, False otherwise.
    """
    filename = parse_filename(url)
    final_path = os.path.join(PARTIAL_DOWNLOAD_DIR, filename)
    os.makedirs(PARTIAL_DOWNLOAD_DIR, exist_ok=True)

    # 1) get total size via HEAD on e.g. port BASE_PORT
    port_for_head = BASE_PORT
    ua_head = random_user_agent(user_agents)
    size = get_content_length(url, port_for_head, ua_head)
    if not size or size < 1:
        print(f"[ERR] Cannot get valid size for {url}.")
        return False
    print(f"[PARTIAL] {url} -> size={size} bytes")

    # 2) build chunk ranges
    chunk_size = size // MAX_WORKERS
    ranges = []
    for i in range(MAX_WORKERS):
        start = i * chunk_size
        if i == (MAX_WORKERS - 1):
            end = size - 1
        else:
            end = (start + chunk_size) - 1
        ranges.append((start, end))

    # We'll have one progress bar for the entire file
    progress_queue = queue.Queue()
    stop_flag = threading.Event()

    def progress_manager():
        downloaded_so_far = 0
        with tqdm(total=size, desc=f"Partial: {filename}", unit="B", unit_scale=True) as pbar:
            while not stop_flag.is_set() or not progress_queue.empty():
                try:
                    chunk_len = progress_queue.get(timeout=0.2)
                    downloaded_so_far += chunk_len
                    pbar.update(chunk_len)
                except queue.Empty:
                    pass

    pm_thread = threading.Thread(target=progress_manager, daemon=True)
    pm_thread.start()

    # chunk worker function
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

    # spawn chunk threads
    threads = []
    for i in range(MAX_WORKERS):
        t = threading.Thread(target=chunk_worker, args=(i,))
        t.start()
        threads.append(t)

    # wait for all
    for t in threads:
        t.join()

    # stop progress manager
    stop_flag.set()
    pm_thread.join()

    # check if any chunk failed => remove partials
    if any(cp is None for cp in chunk_paths):
        print(f"[FAIL] Some chunk failed for {url}. Removing partial files.")
        for cp in chunk_paths:
            if cp and os.path.exists(cp):
                os.remove(cp)
        if os.path.exists(final_path):
            os.remove(final_path)
        return False

    # 3) merge chunks
    total_merged = 0
    try:
        with open(final_path, "wb") as outf:
            for cp in chunk_paths:
                with open(cp, "rb") as cf:
                    data = cf.read()
                    outf.write(data)
                    total_merged += len(data)
    except Exception as e:
        print(f"[ERR] Merge failed: {e}")
        return False

    # remove part files
    for cp in chunk_paths:
        if os.path.exists(cp):
            os.remove(cp)

    if total_merged != size:
        print(f"[ERR] Merged size mismatch: {total_merged} != {size}. Removing final file.")
        if os.path.exists(final_path):
            os.remove(final_path)
        return False

    print(f"[OK] Partial download done: {final_path} ({total_merged} bytes)")
    return True

def partial_download_mode(urls, retries=DEFAULT_RETRIES):
    """
    Partial-download mode for all URLs in `urls`.
    Each file is tried up to `retries` times if there's a chunk failure.
    We do them sequentially (one by one),
    but each file is internally split into MAX_WORKERS chunks in parallel.
    """
    user_agents = load_user_agents()

    for idx, url in enumerate(urls, start=1):
        print(f"\n[{idx}/{len(urls)}] Partial-downloading: {url}")
        success = False
        attempts_left = retries
        while attempts_left > 0 and not success:
            success = partial_download_file(url, user_agents)
            if not success:
                attempts_left -= 1
                if attempts_left > 0:
                    print(f"[WARN] Retrying partial for {url} (left={attempts_left})")
                    time.sleep(2)  # small delay
        if not success:
            print(f"[FAIL] Could not partial-download {url} after {retries} attempts.")

# =======================================================
#               SPEEDTEST & HEALTHCHECK
# =======================================================

def test_proxy_speed(url: str, port: int, ua) -> dict:
    """
    Downloads up to MAX_SPEEDTEST_BYTES from the given URL
    using the specified port with DNS via socks5h.
    Returns a dict with info about success, speed, etc.
    """
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

def speedtest_mode(urls):
    """
    Speedtest & healthcheck mode.
    We'll just take the FIRST URL from URLs.txt (for demonstration).
    Then we test each port 5000..(5000+MAX_WORKERS-1),
    measure how many bytes we can download quickly,
    compute average speed, and show a summary with emojis.
    """
    if not urls:
        print("[WARN] No URLs found in URLs.txt for speedtest.")
        return
    test_url = urls[0]  # take the first line as test
    print(f"\n[Speedtest] We'll test the first URL: {test_url}")

    user_agents = load_user_agents()
    results = []
    for i in range(MAX_WORKERS):
        port = BASE_PORT + i
        ua = random_user_agent(user_agents)
        print(f"Testing port={port} ...")
        res = test_proxy_speed(test_url, port, ua)
        results.append(res)

    # Show summary
    print("\n=== SPEEDTEST SUMMARY ===")
    for r in results:
        if r["success"]:
            speed_kbps = r["speed_bps"] / 1024
            print(f"✅ Port {r['port']} => downloaded={r['downloaded_bytes']} B, speed={speed_kbps:.2f} KB/s")
        else:
            print(f"❌ Port {r['port']} => error={r['error']}")
    print("[DONE] Speedtest complete.\n")

# =======================================================
#                  MAIN SCRIPT
# =======================================================

def main():
    parser = argparse.ArgumentParser(
        description="OnionAccelerator: multi/partial download with Tor proxies, plus speedtest."
    )
    parser.add_argument("--mode", choices=["multi", "partial", "speedtest"], required=True,
                        help="Select mode: 'multi', 'partial', or 'speedtest'.")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES,
                        help="Number of retries if a download fails. Default=3.")
    args = parser.parse_args()

    # Load URLs always from URLs_FILE
    if not os.path.exists(URLS_FILE):
        print(f"[ERROR] {URLS_FILE} not found. Please create it and put your URLs inside.")
        sys.exit(1)

    with open(URLS_FILE, "r", encoding="utf-8") as f:
        urls = [line.strip() for line in f if line.strip()]

    if not urls:
        print(f"[WARN] {URLS_FILE} is empty. Nothing to process.")
        sys.exit(0)

    if args.mode == "multi":
        print(f"Running MULTI-DOWNLOAD for {len(urls)} URLs with up to {MAX_WORKERS} threads (ports). Retries={args.retries}")
        multi_download_mode(urls, retries=args.retries)

    elif args.mode == "partial":
        print(f"Running PARTIAL-DOWNLOAD for {len(urls)} URLs. Each file has up to {MAX_WORKERS} chunks. Retries={args.retries}")
        partial_download_mode(urls, retries=args.retries)

    elif args.mode == "speedtest":
        print("Running SPEEDTEST mode.")
        speedtest_mode(urls)

if __name__ == "__main__":
    main()
