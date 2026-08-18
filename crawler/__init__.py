"""Recursive "Index of /" crawling over a pool of independent Tor circuits.

Two halves, deliberately kept apart. The **engine** -- frontier, circuit pool, fetcher,
report -- moves bytes and knows nothing about what a listing looks like. The **listing
layer** (`crawler.listing`) decides what a fetched page means, driven by templates: one
small TOML file per known open-directory logic, matched against the target.

The public surface stays small: OnionAccelerator.py builds a `CrawlConfig`, calls
`crawl()`, and gets back the run statistics plus every file URL that was found.

    from crawler import CrawlConfig, crawl

    stats, urls = crawl(config, endpoints, user_agents)

Adding support for a new file manager is a file in `crawler/listing/templates/`, or a
directory of your own passed as `--templates`. It is not a change to any of this.
"""

from .config import CrawlConfig, ORDER_BFS, ORDER_DFS
from .crawl import crawl, detect_targets, run_crawl, run_detect
from .indexparse import parse_index
from .listing import (
    Entry,
    Listing,
    ListingEngine,
    Page,
    PageRequest,
    Profile,
    TemplateError,
    detect,
    load_profiles,
)
from .report import CrawlReport

# The listing layer's name for a parsed page. `IndexListing` was this package's original
# name for the same thing and is kept because callers and tests use it.
IndexListing = Listing

__all__ = [
    "CrawlConfig",
    "CrawlReport",
    "Entry",
    "IndexListing",
    "Listing",
    "ListingEngine",
    "ORDER_BFS",
    "ORDER_DFS",
    "Page",
    "PageRequest",
    "Profile",
    "TemplateError",
    "crawl",
    "detect",
    "detect_targets",
    "load_profiles",
    "parse_index",
    "run_crawl",
    "run_detect",
]
