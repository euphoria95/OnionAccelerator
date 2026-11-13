"""--mode multi and --mode partial: the proxy pool and both download paths.

Moved out of OnionAccelerator.py unchanged; this is the code Phase 1 of
docs/ROADMAP.md replaces with a piece-based engine.
"""

import os
import queue
import tempfile
import threading
import time
from typing import Optional
from urllib.parse import urlparse

import requests
from tqdm import tqdm

from .common import (explain_error, load_user_agents, logger, make_proxies,
                     random_user_agent, url_to_local_path)
from .config import (CHUNK_SIZE, DEFAULT_RETRIES, DOWNLOAD_DIR, MIN_PARTIAL_CHUNK_SIZE,
                     PARTIAL_DOWNLOAD_DIR, TRANSFER_TIMEOUT)
from .sizeprobe import get_remote_file_size


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

# Markup content types, and the target extensions for which markup is a plausible
# answer. Anything else answering with markup is an error page wearing the filename.
# tempfile.mkstemp() hardcodes 0600, but a download is an ordinary file and used to be
# created by open(), i.e. at the process umask. Sampled once here, while the process is
# still single-threaded, because reading the umask means temporarily setting it.
_UMASK = os.umask(0)
os.umask(_UMASK)
_DOWNLOAD_MODE = 0o666 & ~_UMASK

_MARKUP_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_MARKUP_EXTENSIONS = frozenset({
    "", ".html", ".htm", ".xhtml", ".shtml", ".php", ".asp", ".aspx", ".jsp", ".cgi",
})


def _error_page_reason(url: str, content_type: str) -> Optional[str]:
    """Why this response body is a server error page and not the requested file.

    Returns None when the body should be believed.

    The status line cannot be used for this. The file managers behind these onions
    answer 404 while sending the complete file -- a 16-byte note arrives with the
    right length, the right `text/plain`, and a 404 -- and they answer *200* with a
    9.6 KB "not available" HTML page in place of a 166 MB CAD model. Trusting the
    code both throws away good files and saves rubbish under their names, and the
    second failure is silent: the page is written to `Auftragslayout....STEP`,
    reported [OK], and only found much later by whoever tries to open it.

    What does separate the two is the content type against the target's extension.
    A `.STEP`, `.zip` or `.xlsx` request answered with markup is an error page on any
    server, whatever the status line claims; a `.txt` answered with `text/plain` is
    the file, whatever the status line claims. Extensions that could legitimately be
    served as markup are exempt, so this can only reject a body that contradicts the
    name it was asked for.
    """
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype not in _MARKUP_TYPES:
        return None
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    if ext in _MARKUP_EXTENSIONS:
        return None
    return f"server sent {ctype} for a '{ext}' target -- an error page, not the file"


def _check_response(url, response) -> None:
    """Raise unless `response` carries the requested file. See _error_page_reason()."""
    reason = _error_page_reason(url, response.headers.get("content-type", ""))
    if reason is not None:
        raise RuntimeError(f"HTTP {response.status_code}: {reason}")
    if not response.ok:
        if not response.headers.get("content-type"):
            # A bad status with nothing to identify the body is just a bad status.
            response.raise_for_status()
        # A body that survived the check above is the file, mislabelled. Say so once,
        # loudly, because "downloaded 404s" is otherwise indistinguishable from a bug.
        logger.warning(
            f"HTTP {response.status_code} for {url}, but the body is "
            f"{response.headers.get('content-type')} and not an error page -- keeping it."
        )


def download_file(url, proxy, user_agents, progress_queue=None, preserve_path=False):
    """
    Download a single file with requests, streaming, ignoring HTTPS cert.
    """
    proxies = make_proxies(proxy)
    ua = random_user_agent(user_agents)
    out_path = url_to_local_path(url, DOWNLOAD_DIR, preserve_path=preserve_path)
    out_dir = os.path.dirname(out_path)
    os.makedirs(out_dir, exist_ok=True)

    # The body streams into a private temp file in the destination directory and is
    # only moved into place once it has arrived complete. Writing straight to
    # `out_path` was wrong in two ways that both silently destroyed finished work:
    # the open() truncated any existing file before a single byte had been read, so a
    # mid-body timeout left a stub where a good file used to be, and the failure
    # handler then unlinked `out_path` unconditionally -- deleting whatever was there
    # even when this attempt had never written to it. Under the flat naming layout
    # those are not hypothetical: several distinct URLs map to one path, so a retry
    # that times out on `.../v3/Thumbs.db` would wipe the `.../v1/Thumbs.db` that had
    # already downloaded cleanly. os.replace() is atomic, so concurrent writers to one
    # path now leave a whole file from one of them rather than the interleaved bytes
    # of both.
    tmp_path = None
    try:
        with requests.get(url, proxies=proxies, timeout=TRANSFER_TIMEOUT,
                          verify=False, stream=True, headers={"User-Agent": ua}) as r:
            _check_response(url, r)
            fd, tmp_path = tempfile.mkstemp(dir=out_dir, prefix=".oa-", suffix=".part")
            os.chmod(tmp_path, _DOWNLOAD_MODE)
            with os.fdopen(fd, "wb") as f:
                for chunk in r.iter_content(CHUNK_SIZE):
                    if chunk:
                        f.write(chunk)
                        if progress_queue:
                            progress_queue.put(len(chunk))
            os.replace(tmp_path, out_path)
            tmp_path = None
        logger.debug(f"[OK] {url} -> {out_path} (proxy={proxy})")
        return True
    except Exception as e:
        logger.error(f"[ERR] Download failed for {url} on proxy={proxy}: {explain_error(e)}")
        # Only ever removes this attempt's own partial file, never the destination.
        if tmp_path is not None:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        return False


def _warn_on_path_collisions(urls, *, preserve_path: bool, base_dir: str = DOWNLOAD_DIR) -> int:
    """Say up front how many URLs are about to overwrite each other, and why.

    The flat layout names a download after its basename alone, so a list holding
    `.../v1/Thumbs.db` and `.../v2/Thumbs.db` writes one file and keeps whichever
    finished last. Every download still reports [OK], nothing reports an error, and
    the only trace is that downloads/ ends up smaller than the URL list -- which is
    exactly the shape of a bug that gets blamed on the URLs being wrong. A crawl
    manifest is full of these (README.txt, Thumbs.db, Bild1.PNG in every directory),
    so the count is worth a line in the log before the run rather than an
    investigation after it.

    Returns the number of URLs that would be lost.
    """
    mapping = {}
    for url in urls:
        mapping.setdefault(url_to_local_path(url, base_dir,
                                             preserve_path=preserve_path), []).append(url)
    clashes = {path: group for path, group in mapping.items() if len(group) > 1}
    if not clashes:
        return 0

    lost = sum(len(group) - 1 for group in clashes.values())
    logger.warning(
        f"{lost} of {len(urls)} URL(s) map onto {len(clashes)} already-taken local "
        f"path(s) and will overwrite each other; only {len(mapping)} file(s) can survive."
    )
    if not preserve_path:
        logger.warning(
            "Pass --preserve-path to mirror the remote directory tree under "
            f"{base_dir}/<host>/ and keep all of them."
        )
    for path, group in list(clashes.items())[:5]:
        logger.warning(f"  {os.path.relpath(path)} <- {len(group)} URLs, e.g. {group[0]}")
    if len(clashes) > 5:
        logger.warning(f"  ... and {len(clashes) - 5} more collision(s).")
    return lost


def multi_download_mode(urls, proxies, retries=DEFAULT_RETRIES, preserve_path=False):
    """Download every URL in parallel, one worker per proxy.

    `preserve_path` mirrors each URL's directories under downloads/<host>/ instead of
    flattening to the basename; crawl mode's --download phase needs it, because a
    crawled tree is full of same-named files in different directories.
    """
    logger.info(f"multi_download_mode: {len(urls)} URLs, {len(proxies)} proxies, retries={retries}")
    _warn_on_path_collisions(urls, preserve_path=preserve_path)
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



# =========== PARTIAL-DOWNLOAD ===========


def partial_download_chunk_to_file(url, start, end, proxy, ua, progress_queue, part_path):
    proxies = make_proxies(proxy)
    headers = {
        "User-Agent": ua,
        "Range": f"bytes={start}-{end}"
    }
    try:
        with requests.get(url, proxies=proxies, timeout=TRANSFER_TIMEOUT,
                          verify=False, stream=True, headers=headers) as r:
            # Same check as the whole-file path: an error page accepted here is worse,
            # because it is merged into the middle of the assembled file rather than
            # sitting alone under a wrong name.
            _check_response(url, r)
            with open(part_path, "wb") as f:
                for chunk in r.iter_content(CHUNK_SIZE):
                    if chunk:
                        f.write(chunk)
                        progress_queue.put(len(chunk))
        return True
    except Exception as e:
        logger.error(f"Chunk {start}-{end} failed on proxy={proxy}: {explain_error(e)}")
        if os.path.exists(part_path):
            os.remove(part_path)
        return False


def fallback_sequential_download(url, out_path, user_agent, proxy, progress_queue):
    # Staged through a temp file and moved into place on success, for the reasons
    # spelled out in download_file(): this is the retry path, so `out_path` may
    # already hold a complete file from an earlier attempt, and truncating it up
    # front means a failure here destroys that instead of merely not improving on it.
    proxies = make_proxies(proxy)
    out_dir = os.path.dirname(out_path) or "."
    os.makedirs(out_dir, exist_ok=True)
    tmp_path = None
    try:
        with requests.get(url, proxies=proxies, timeout=TRANSFER_TIMEOUT,
                          verify=False, stream=True, headers={"User-Agent": user_agent}) as r:
            _check_response(url, r)
            total_downloaded = 0
            fd, tmp_path = tempfile.mkstemp(dir=out_dir, prefix=".oa-", suffix=".part")
            os.chmod(tmp_path, _DOWNLOAD_MODE)
            with os.fdopen(fd, "wb") as f, tqdm(
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
            os.replace(tmp_path, out_path)
            tmp_path = None
        logger.info(f"[SEQUENTIAL OK] {url} -> {out_path}, size={total_downloaded} B")
        return True
    except Exception as e:
        logger.error(f"Fallback sequential download failed for {url}: {explain_error(e)}")
        if tmp_path is not None:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        return False


def partial_download_file(url, pool, user_agents, preserve_path=False):
    final_path = url_to_local_path(url, PARTIAL_DOWNLOAD_DIR, preserve_path=preserve_path)
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


def partial_download_mode(urls, proxies, retries=DEFAULT_RETRIES, preserve_path=False):
    logger.info(f"partial_download_mode: {len(urls)} URLs, {len(proxies)} proxies, retries={retries}")
    _warn_on_path_collisions(urls, preserve_path=preserve_path, base_dir=PARTIAL_DOWNLOAD_DIR)
    user_agents = load_user_agents()
    pool = ProxyPool(proxies)

    for idx, url in enumerate(urls, start=1):
        logger.info(f"\n[{idx}/{len(urls)}] Partial: {url}")
        success = False
        attempts_left = retries
        while attempts_left > 0 and not success:
            success = partial_download_file(url, pool, user_agents,
                                            preserve_path=preserve_path)
            if not success:
                attempts_left -= 1
                if attempts_left > 0:
                    logger.warning(f"Retrying partial for {url}, left={attempts_left}")
                    time.sleep(2)
        if not success:
            logger.error(f"[FAIL] partial-download {url} after {retries} attempts.")

    logger.info("partial_download_mode: completed")
