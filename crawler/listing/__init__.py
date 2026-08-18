"""Reading a listing: the scraping half of the crawler, with no crawl in it.

The engine (frontier, pool, fetcher, report) moves bytes; this package decides what they
mean. The split exists because those two things change for different reasons -- the
transport is the same for every onion, and the reading is different for every file
manager -- and because keeping them together is what made "this target is unsupported"
indistinguishable from "this directory is empty".

    from crawler.listing import ListingEngine, Page

    engine = ListingEngine.build(forced=None, templates=["./my-templates"])
    listing = engine.parse(Page(url=url, body=html, content_type="text/html"))

Adding support for a target is a TOML file in `templates/`, not a patch to any of this.
"""

from .detect import Finding, detect, rank, render
from .engine import ListingEngine
from .model import Entry, Listing, PageRequest
from .page import Page
from .profile import Profile, TemplateError
from .registry import FALLBACK, describe, load_profiles

__all__ = [
    "Entry",
    "FALLBACK",
    "Finding",
    "Listing",
    "ListingEngine",
    "Page",
    "PageRequest",
    "Profile",
    "TemplateError",
    "describe",
    "detect",
    "load_profiles",
    "rank",
    "render",
]
