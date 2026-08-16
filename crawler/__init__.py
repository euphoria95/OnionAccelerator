"""Recursive "Index of /" crawling over a pool of independent Tor circuits.

The public surface is deliberately small: OnionAccelerator.py builds a `CrawlConfig`,
calls `crawl()`, and gets back the run statistics plus every file URL that was found.
Everything else -- the parser, the circuit pool, the frontier, the error taxonomy --
is an implementation detail of that call.

    from crawler import CrawlConfig, crawl

    stats, urls = crawl(config, endpoints, user_agents)
"""

from .config import CrawlConfig, ORDER_BFS, ORDER_DFS
from .crawl import crawl, run_crawl
from .indexparse import Entry, IndexListing, parse_index
from .report import CrawlReport

__all__ = [
    "CrawlConfig",
    "CrawlReport",
    "Entry",
    "IndexListing",
    "ORDER_BFS",
    "ORDER_DFS",
    "crawl",
    "parse_index",
    "run_crawl",
]
