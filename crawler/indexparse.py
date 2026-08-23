"""The universal "Index of /" reading, kept at its old address.

The parser this module used to be now lives in `crawler.listing`, split into the parts
that turned out to vary independently: the structural anchor reader
(`listing/strategies/anchors.py`), the JSON one (`listing/strategies/jsonrows.py`), and
the addressing rules that decide what a listed row's URL actually is
(`listing/navigate.py`).

What stays here is the entry point everything already calls, with exactly its old
behaviour: parse one page, structurally, with no template and no profile. It is the
reading a target gets when nothing knows anything specific about it, and the tests that
pin the structural rules are written against this function.
"""

from __future__ import annotations

from typing import Iterable

from .listing.model import Entry, Listing, PageRequest, dedupe, split_entries
from .listing.navigate import Location, resolve
from .listing.page import Page
from .listing.profile import Extract, Navigate, Profile
from .listing.rowtext import parse_size as _parse_size
from .listing.strategies import ExtractContext
from .listing.strategies.anchors import read as _read_anchors, score_index
from .listing.strategies.jsonrows import read as _read_json

# `IndexListing` was this module's name for what the listing layer calls a Listing. The
# fields are the same and in the same order; the alias keeps every caller working.
IndexListing = Listing

__all__ = [
    "Entry",
    "IndexListing",
    "Listing",
    "iter_entries",
    "parse_index",
    "score_index",
]

# The two readings this entry point can do, as profiles, built once. Neither has match
# rules: which one runs is decided by the content type, exactly as it always was.
_STRUCTURAL = Profile(
    name="generic-structural",
    title="Any open directory, read structurally",
    priority=0,
    extract=Extract(strategy="anchors"),
    navigate=Navigate(),
)
_JSON = Profile(
    name="json-autoindex",
    title="nginx/Caddy JSON autoindex",
    priority=80,
    extract=Extract(strategy="json"),
    navigate=Navigate(),
)


def parse_index(
    html: str,
    base_url: str,
    *,
    content_type: str = "",
    allow_offsite: bool = False,
) -> Listing:
    """Extract the directory listing from `html`, which was served at `base_url`.

    `content_type` only decides HTML vs JSON: nginx's `autoindex_format json` and
    Caddy's JSON browse emit the same information without any markup at all, and it
    would be perverse to run a HTML parser over it. A JSON body that is not a listing
    falls through to the markup path rather than being reported as an empty directory --
    the difference between "unreadable" and "empty" is one the crawler acts on.
    """
    page = Page(url=base_url, body=html, content_type=content_type,
                request=PageRequest.get(base_url))
    ctx = ExtractContext(allow_offsite=allow_offsite)

    if "json" in content_type.lower():
        result = _read_json(page, _JSON.extract.options, ctx)
        if result.is_index:
            return _assemble(_JSON, page, result)

    result = _read_anchors(page, _STRUCTURAL.extract.options, ctx)
    return _assemble(_STRUCTURAL, page, result)


def _assemble(profile: Profile, page: Page, result) -> Listing:
    """Turn one strategy's rows into the Listing this function has always returned."""
    location = Location.of(profile, result.base_url or page.url, page.request)
    entries = [e for e in (resolve(location, raw) for raw in result.entries) if e is not None]
    directories, files = split_entries(dedupe(entries))
    return Listing(
        base_url=result.base_url or page.url,
        directories=directories,
        files=files,
        is_index=result.is_index,
        confidence=result.confidence,
        title=result.title,
        generator=result.generator,
        profile=profile.name,
    )


def iter_entries(listing: Listing) -> Iterable[Entry]:
    """Directories first, then files -- the order the report renders them in."""
    yield from listing.directories
    yield from listing.files
