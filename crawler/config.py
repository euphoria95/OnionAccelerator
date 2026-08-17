"""Tunables and the run configuration for crawl mode.

Everything a crawl can be steered by lives here, so the orchestrator reads as flow
control rather than as a pile of magic numbers, and so the CLI in OnionAccelerator.py
has exactly one place to write its arguments into.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Optional, Pattern

# ---------------------------------------------------------------- output layout

CRAWLS_DIR = "crawls"
LISTING_FILE = "listing.jsonl"
DIRS_FILE = "dirs.jsonl"
FAILED_FILE = "failed.jsonl"
STATS_FILE = "stats.json"
TREE_FILE = "tree.txt"
URLS_FILE = "urls.txt"

# ---------------------------------------------------------------- network

# Onion services are slow; a connect that hasn't landed in 30s is usually a dead
# descriptor rather than a slow one, and waiting longer just parks a circuit.
CONNECT_TIMEOUT = 30.0
READ_TIMEOUT = 60.0
TOTAL_TIMEOUT = 120.0

# A directory listing is text, but not necessarily small text: one directory of a leaked
# fileshare routinely holds thousands of entries, and at the ~600 bytes of markup a
# file-manager template spends per row, ten thousand of them is 6 MB. The cap is high
# enough that hitting it means "not a listing" rather than "a big listing", because a
# listing that hits it is a whole subtree lost. Past it the body is abandoned and the
# fetch recorded as a failure, with the cap named in the error.
MAX_PAGE_BYTES = 16 * 1024 * 1024
READ_CHUNK = 64 * 1024

# Backoff: min(cap, base * 2**attempt), then full jitter. The cap matters more than the
# base on Tor, where a 503 from an overloaded onion often clears in under a minute but a
# geometric series would otherwise push the retry past the end of the run.
BACKOFF_BASE = 2.0
BACKOFF_CAP = 60.0
# Retry-After can legally be a date far in the future; anything past this is treated as
# the server saying "not today" and the job is dropped rather than parked forever.
MAX_RETRY_AFTER = 300.0

# Consecutive failures on one endpoint before it is parked as dead. One failure is
# noise on Tor; three in a row on the same daemon is the daemon.
ENDPOINT_DEATH_STREAK = 3

# ---------------------------------------------------------------- concurrency

DEFAULT_CIRCUITS_PER_ENDPOINT = 2
DEFAULT_PER_HOST = 8
# None means "no depth cap": crawl the whole tree under each seed. That is the point of
# the mode -- an open directory is harvested to exhaustion -- and it is bounded anyway by
# exact URL deduplication (a tree cannot recurse into itself), the index-confidence guard
# (it will not wander into an app), and the --max-pages / --time-budget brakes. Pass
# --max-depth N to reinstate a finite cap.
DEFAULT_MAX_DEPTH: Optional[int] = None
LANE_WAIT_TIMEOUT = 300.0

# How often the run logs a progress/endpoint-balance line.
STATS_INTERVAL = 30.0

# ---------------------------------------------------------------- parsing

# Below this, a page is not treated as a directory index and is not expanded. See
# indexparse.score_index() for what feeds the score.
INDEX_CONFIDENCE_THRESHOLD = 0.5

# Link text that no directory entry ever legitimately has. This is a *secondary* net:
# the structural rules in indexparse.py already remove the column headers of every
# server tested, and this only catches layouts that render them as real links.
JUNK_LINK_TEXT = frozenset({
    "name", "last modified", "size", "description", "date", "type",
    "parent directory", "parent", "up", "..", "../", "↑", "back",
    "sort by name", "sort by date", "sort by size",
})

ORDER_BFS = "bfs"
ORDER_DFS = "dfs"


@dataclasses.dataclass
class CrawlConfig:
    """One crawl run's parameters.

    Built once from the CLI and then read-only, except for `order`, which the layer
    controller may flip mid-run (see Frontier.set_order).
    """

    seeds: list[str]
    max_depth: Optional[int] = DEFAULT_MAX_DEPTH
    order: str = ORDER_BFS
    workers: Optional[int] = None
    circuits_per_endpoint: int = DEFAULT_CIRCUITS_PER_ENDPOINT
    per_host: int = DEFAULT_PER_HOST
    retries: int = 3
    max_pages: Optional[int] = None
    time_budget: Optional[float] = None
    max_page_bytes: int = MAX_PAGE_BYTES
    allow_offsite: bool = False
    include: Optional[Pattern[str]] = None
    exclude: Optional[Pattern[str]] = None
    switch_after: Optional[int] = None
    download: bool = False
    out_dir: str = ""
    job_id: str = ""

    @staticmethod
    def compile_filter(pattern: Optional[str]) -> Optional[Pattern[str]]:
        """Compile a --include/--exclude regex, or None if it wasn't given."""
        return re.compile(pattern) if pattern else None
