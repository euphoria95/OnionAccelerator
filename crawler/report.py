"""The artefacts a crawl leaves behind.

A crawl of an open directory is evidence, so the output is written for a reader who
was not there: append-only JSONL streamed to disk as the crawl runs (so a run killed
at hour three still has everything it found by hour three), plus a rendered tree for
eyeballing and a bare URL list that feeds straight back into `--mode multi`.

Every record carries where it came from -- parent directory, depth, the SOCKS endpoint
that fetched it, the HTTP status, and the size and timestamp exactly as the server
printed them.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import time
from typing import Any, Iterable, Optional, TextIO

from .config import (
    DIRS_FILE,
    FAILED_FILE,
    LISTING_FILE,
    STATS_FILE,
    TREE_FILE,
    URLS_FILE,
)
from .frontier import Job
from .listing import Entry, Listing
from .urlnorm import dedup_key, host_of, path_segments

logger = logging.getLogger("OnionAccelerator.crawl.report")


@dataclasses.dataclass
class Totals:
    """Run-level counters, mirrored into stats.json."""

    directories: int = 0
    files: int = 0
    skipped: int = 0           # fetched, read, and not a directory listing
    bytes_seen: int = 0        # sum of the sizes the *listings* advertised
    bytes_fetched: int = 0     # what the crawler actually pulled over Tor
    requests: int = 0
    retries: int = 0
    rotations: int = 0
    failures: int = 0


class CrawlReport:
    """Streams crawl output into `out_dir`, then renders the summaries on close."""

    def __init__(self, out_dir: str, job_id: str, seeds: Iterable[str]) -> None:
        self.out_dir = out_dir
        self.job_id = job_id
        self.seeds = list(seeds)
        self.totals = Totals()
        self.started = time.time()

        self._listing: Optional[TextIO] = None
        self._dirs: Optional[TextIO] = None
        self._failed: Optional[TextIO] = None
        # File URLs are deduplicated here, not in the frontier: the frontier only ever
        # queues directories, but the same file can legitimately be linked from two of
        # them, and the manifest should list it once. Keyed by identity rather than by
        # URL so two spellings of one file collapse; the value is the spelling to fetch.
        self._file_urls: dict[str, str] = {}
        self._tree: dict[str, dict[str, Any]] = {}

    def __enter__(self) -> "CrawlReport":
        os.makedirs(self.out_dir, exist_ok=True)
        self._listing = open(os.path.join(self.out_dir, LISTING_FILE), "a", encoding="utf-8")
        self._dirs = open(os.path.join(self.out_dir, DIRS_FILE), "a", encoding="utf-8")
        self._failed = open(os.path.join(self.out_dir, FAILED_FILE), "a", encoding="utf-8")
        logger.info("crawl output: %s", os.path.abspath(self.out_dir))
        return self

    def __exit__(self, *exc: object) -> None:
        for handle in (self._listing, self._dirs, self._failed):
            if handle is not None:
                handle.close()

    # ------------------------------------------------------------ recording

    def record_listing(self, job: Job, listing: Listing, result_record: dict[str, Any]) -> None:
        """One successfully parsed directory, plus every file it contained."""
        self.totals.directories += 1
        record = dict(result_record)
        record.update({
            "depth": job.depth,
            "parent": job.parent,
            "attempts": job.attempt + 1,
            "n_dirs": len(listing.directories),
            "n_files": len(listing.files),
            "is_index": listing.is_index,
            "confidence": round(listing.confidence, 2),
            "profile": listing.profile,
            "server": listing.generator,
            "title": listing.title,
            "discovered_at": _now(),
        })
        self._write(self._dirs, record)
        self._add_to_tree(job.url, is_dir=True)

        for entry in listing.files:
            self.record_file(entry, parent=job.url, depth=job.depth + 1,
                             endpoint=record.get("endpoint"))

        logger.info(
            "[DIR ] depth=%d %d dir(s) %d file(s) %s",
            job.depth, len(listing.directories), len(listing.files), job.url,
        )

    def record_file(
        self,
        entry: Entry,
        *,
        parent: Optional[str],
        depth: int,
        endpoint: Optional[str] = None,
        status: Optional[int] = None,
        content_type: Optional[str] = None,
    ) -> None:
        """One discovered file. Silently ignored if it was already recorded."""
        key = dedup_key(entry.url)
        if key in self._file_urls:
            return
        # What goes in urls.txt is what `--download` will fetch, which on a target that
        # browses at one URL and serves bytes at another is not the URL that identifies
        # the file. The identity stays the browse URL; only the spelling handed on differs.
        self._file_urls[key] = entry.fetch_url
        self.totals.files += 1
        if entry.size_bytes:
            self.totals.bytes_seen += entry.size_bytes
        self._write(self._listing, {
            "url": entry.url,
            "host": host_of(entry.url),
            "path": "/" + "/".join(path_segments(entry.url)),
            "name": entry.name,
            "size_bytes": entry.size_bytes,
            "mtime_text": entry.mtime_text,
            "download_url": entry.download_url,
            "depth": depth,
            "parent": parent,
            "http_status": status,
            "content_type": content_type,
            "endpoint": endpoint,
            "discovered_at": _now(),
        })
        self._add_to_tree(entry.url, is_dir=False, size=entry.size_bytes,
                          mtime=entry.mtime_text)

    def record_skipped(self, job: Job, listing: Listing,
                       result_record: dict[str, Any]) -> None:
        """A page that was fetched and read but is not a directory listing.

        Kept out of the file manifest, which is the difference that matters: this URL
        was queued because a listing put it in its *directory* column, and answering
        with HTML does not make it a file. Recording it as one puts a directory into
        urls.txt, where `--download` fetches its markup and saves that under the
        directory's name. It goes into dirs.jsonl with the score that disqualified it,
        so "why was this not crawled" has an answer on disk and not only in the log.
        """
        self.totals.skipped += 1
        record = dict(result_record)
        record.update({
            "depth": job.depth,
            "parent": job.parent,
            "attempts": job.attempt + 1,
            "n_dirs": len(listing.directories),
            "n_files": len(listing.files),
            "is_index": False,
            "confidence": round(listing.confidence, 2),
            "profile": listing.profile,
            "server": listing.generator,
            "title": listing.title,
            "discovered_at": _now(),
        })
        self._write(self._dirs, record)
        self._add_to_tree(job.url, is_dir=True)

    def record_leaf(self, job: Job, result_record: dict[str, Any]) -> None:
        """A URL that was queued as a directory but turned out to be a file.

        Happens when a listing strips its trailing slashes: the entry looked like a
        directory in the markup, and the server answered with a file. Not the same thing
        as a page that parsed but did not look like a listing -- see record_skipped.
        """
        entry = Entry(
            url=result_record.get("final_url") or job.url,
            name=(path_segments(job.url) or ["/"])[-1],
            is_dir=False,
            size_bytes=result_record.get("bytes") or None,
        )
        self.record_file(
            entry, parent=job.parent, depth=job.depth,
            endpoint=result_record.get("endpoint"),
            status=result_record.get("status"),
            content_type=result_record.get("content_type"),
        )

    def record_failure(self, job: Job, result_record: dict[str, Any], *, final: bool) -> None:
        """A failed attempt. `final` marks the one that exhausted the retry budget."""
        self.totals.failures += 1
        record = dict(result_record)
        record.update({
            "depth": job.depth,
            "parent": job.parent,
            "attempts": job.attempt + 1,
            "final": final,
            "at": _now(),
        })
        self._write(self._failed, record)
        if final:
            logger.error("[FAIL] gave up on %s after %d attempt(s): %s",
                         job.url, job.attempt + 1, record.get("error"))

    # ------------------------------------------------------------ summaries

    def finalize(self, *, layers: dict[int, dict[str, int]],
                 endpoints: list[dict[str, Any]],
                 config: dict[str, Any],
                 stopped_because: str) -> dict[str, Any]:
        """Write stats.json, tree.txt and urls.txt. Returns the stats it wrote."""
        elapsed = time.time() - self.started
        stats = {
            "job_id": self.job_id,
            "seeds": self.seeds,
            "started_at": _iso(self.started),
            "finished_at": _iso(time.time()),
            "elapsed_s": round(elapsed, 1),
            "stopped_because": stopped_because,
            "totals": dataclasses.asdict(self.totals),
            "layers": layers,
            "endpoints": endpoints,
            "config": config,
        }
        _write_json(os.path.join(self.out_dir, STATS_FILE), stats)

        with open(os.path.join(self.out_dir, URLS_FILE), "w", encoding="utf-8") as fh:
            for url in self.file_urls:
                fh.write(url + "\n")

        with open(os.path.join(self.out_dir, TREE_FILE), "w", encoding="utf-8") as fh:
            for line in self.render_tree():
                fh.write(line + "\n")

        logger.info(
            "crawl finished (%s): %d directories, %d files, %s advertised, %s fetched, %.1fs",
            stopped_because, self.totals.directories, self.totals.files,
            human_size(self.totals.bytes_seen), human_size(self.totals.bytes_fetched), elapsed,
        )
        return stats

    @property
    def file_urls(self) -> list[str]:
        """The discovered files, in a stable order -- what `--download` is handed."""
        return sorted(self._file_urls.values())

    # ------------------------------------------------------------ tree building

    def _add_to_tree(self, url: str, *, is_dir: bool,
                     size: Optional[int] = None, mtime: Optional[str] = None) -> None:
        host = host_of(url)
        node = self._tree.setdefault(host, {"children": {}, "is_dir": True})
        for segment in path_segments(url):
            children = node["children"]
            node = children.setdefault(segment, {"children": {}, "is_dir": True})
        node["is_dir"] = is_dir
        if size is not None:
            node["size"] = size
        if mtime:
            node["mtime"] = mtime

    def render_tree(self) -> list[str]:
        """The crawl as an indented tree, one host per block."""
        lines: list[str] = []
        for host in sorted(self._tree):
            lines.append(host)
            lines.extend(self._render_node(self._tree[host], prefix=""))
            lines.append("")
        return lines

    def _render_node(self, node: dict[str, Any], prefix: str) -> list[str]:
        lines: list[str] = []
        children = node["children"]
        names = sorted(children, key=lambda n: (not children[n]["is_dir"], n.lower()))
        for i, name in enumerate(names):
            child = children[name]
            last = i == len(names) - 1
            branch = "`-- " if last else "|-- "
            label = name + ("/" if child["is_dir"] else "")
            if not child["is_dir"] and child.get("size") is not None:
                label = f"{label}  ({human_size(child['size'])})"
            lines.append(prefix + branch + label)
            if child["children"]:
                lines.extend(self._render_node(child, prefix + ("    " if last else "|   ")))
        return lines

    # ------------------------------------------------------------ internals

    @staticmethod
    def _write(handle: Optional[TextIO], record: dict[str, Any]) -> None:
        """Append one JSONL record and flush it.

        Flushing every line costs nothing next to a Tor round trip, and it is what makes
        the output of an interrupted run complete up to the interruption.
        """
        if handle is None:
            return
        handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        handle.flush()


def human_size(nbytes: Optional[int]) -> str:
    """Bytes as a human-readable string. '-' for unknown, matching listing convention."""
    if not nbytes:
        return "-" if nbytes is None else "0 B"
    value = float(nbytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if value < 1024 or unit == "PiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} PiB"


def _write_json(path: str, payload: dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False, default=str)
        fh.write("\n")


def _now() -> str:
    return _iso(time.time())


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))
