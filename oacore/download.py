"""--mode multi, --mode partial and `crawl --download`: URLs in, files out.

All three run on oacore.engine: sized files are cut into pieces and fetched by lanes that
steal work from one another, written at their offsets into one file beside a journal, and
resumed from that journal if the run is interrupted. What distinguishes the modes is only
how many files are in flight: multi runs many at once over the shared lanes, partial runs
one at a time with every lane on it.
"""

import os

from tqdm import tqdm

from .common import load_user_agents, logger, url_to_local_path
from .config import DEFAULT_RETRIES, DOWNLOAD_DIR, PARTIAL_DOWNLOAD_DIR
from .engine import (DEFAULT_PIECE_SIZE, EVENT_DOWNLOAD_START, Downloader,
                     EngineConfig)

# tempfile.mkstemp() hardcodes 0600, but a download is an ordinary file and used to be
# created by open(), i.e. at the process umask. Sampled once here, while the process is
# still single-threaded, because reading the umask means temporarily setting it.
_UMASK = os.umask(0)
os.umask(_UMASK)
_DOWNLOAD_MODE = 0o666 & ~_UMASK


# =========== PATH COLLISIONS ===========

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


# =========== THE MODES ===========

def _make_downloader(proxies, *, retries, piece_size, fresh, circuits_per_endpoint, sink,
                     job_id, progress):
    config = EngineConfig(piece_size=piece_size or DEFAULT_PIECE_SIZE, retries=retries,
                          fresh=fresh, file_mode=_DOWNLOAD_MODE)
    return Downloader(proxies, circuits_per_endpoint=circuits_per_endpoint,
                      user_agents=load_user_agents(), config=config, sink=sink,
                      job_id=job_id, progress=progress)


def _summarise(name, results):
    failed = [r for r in results if not r.ok]
    logger.info(f"{name}: {len(results) - len(failed)} of {len(results)} file(s) complete"
                + (f", {len(failed)} failed" if failed else ""))
    for r in failed:
        resumable = os.path.exists(r.path + ".oa-state")
        logger.warning(f"[FAIL] {r.url}: {r.reason}"
                       + ("  (partial; rerun the same command to resume)" if resumable else ""))


def multi_download_mode(urls, proxies, retries=DEFAULT_RETRIES, preserve_path=False, *,
                        piece_size=None, fresh=False, circuits_per_endpoint=1, sink=None,
                        job_id=None):
    """Download every URL, many at once, over lanes shared by all of them.

    `preserve_path` mirrors each URL's directories under downloads/<host>/ instead of
    flattening to the basename; crawl mode's --download phase needs it, because a
    crawled tree is full of same-named files in different directories.

    A file that exhausts its retries is left resumable rather than deleted, and the run
    carries on with the rest. Returns one DownloadResult per URL, in order.
    """
    logger.info(f"multi_download_mode: {len(urls)} URLs, {len(proxies)} endpoint(s) x "
                f"{circuits_per_endpoint} circuit(s), retries={retries}")
    _warn_on_path_collisions(urls, preserve_path=preserve_path)
    items = [(url, url_to_local_path(url, DOWNLOAD_DIR, preserve_path=preserve_path))
             for url in urls]
    with tqdm(desc="Multi-Download", unit="B", unit_scale=True, total=None) as bar:
        downloader = _make_downloader(proxies, retries=retries, piece_size=piece_size,
                                      fresh=fresh, circuits_per_endpoint=circuits_per_endpoint,
                                      sink=sink, job_id=job_id, progress=bar.update)
        try:
            results = downloader.download_many(items, file_workers=len(downloader.lanes))
        finally:
            downloader.close()
    _summarise("multi_download_mode", results)
    return results


def partial_download_mode(urls, proxies, retries=DEFAULT_RETRIES, preserve_path=False, *,
                          piece_size=None, fresh=False, circuits_per_endpoint=1, sink=None,
                          job_id=None):
    """Download the URLs one after another, every lane working on the current file."""
    logger.info(f"partial_download_mode: {len(urls)} URLs, {len(proxies)} endpoint(s) x "
                f"{circuits_per_endpoint} circuit(s), retries={retries}")
    _warn_on_path_collisions(urls, preserve_path=preserve_path, base_dir=PARTIAL_DOWNLOAD_DIR)
    results = []
    for idx, url in enumerate(urls, start=1):
        logger.info(f"\n[{idx}/{len(urls)}] Partial: {url}")
        out_path = url_to_local_path(url, PARTIAL_DOWNLOAD_DIR, preserve_path=preserve_path)
        with tqdm(desc=f"Partial: {os.path.basename(out_path)}", unit="B", unit_scale=True,
                  total=None) as bar:
            def bar_sink(kind, data, _bar=bar):
                # The size is only known once the probe has run; the start event carries it.
                if kind == EVENT_DOWNLOAD_START and data.get("size"):
                    _bar.total = data["size"]
                    _bar.refresh()
                if sink is not None:
                    sink(kind, data)

            downloader = _make_downloader(proxies, retries=retries, piece_size=piece_size,
                                          fresh=fresh,
                                          circuits_per_endpoint=circuits_per_endpoint,
                                          sink=bar_sink, job_id=job_id, progress=bar.update)
            try:
                results.append(downloader.download(url, out_path))
            finally:
                downloader.close()
    _summarise("partial_download_mode", results)
    return results


def download_file(url, proxy, user_agents, progress_queue=None, preserve_path=False):
    """Download one URL through one endpoint into DOWNLOAD_DIR. True on success.

    The engine's entry point for a caller that has a single URL and a single proxy and
    wants a yes or no. Kept under its old name and signature: it is what the single-file
    tests, and anything that imported it, call.
    """
    out_path = url_to_local_path(url, DOWNLOAD_DIR, preserve_path=preserve_path)
    progress = (lambda n: progress_queue.put(n)) if progress_queue is not None else None
    downloader = Downloader([proxy], user_agents=user_agents, progress=progress,
                            config=EngineConfig(file_mode=_DOWNLOAD_MODE))
    try:
        return downloader.download(url, out_path).ok
    finally:
        downloader.close()
