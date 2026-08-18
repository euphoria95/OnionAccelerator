"""Rows located by CSS selector, for pages the structural reader cannot read.

Two shapes need this. The first is a file manager whose links do not resolve into the
tree at all -- `?p=dumps/raw`, `#/dumps/raw`, `javascript:open('dumps/raw')` -- so
"points below this page" is not a filter that can be applied. The second is a listing
whose size and date live in columns the row-text scan reads wrongly, which is worth
pinning down for a target that is going to be crawled repeatedly.

The selectors are the template's whole contribution. Everything else -- what a size
string means, what counts as junk, how a child is addressed -- is shared code.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional
from urllib.parse import urljoin

from bs4.element import Tag

from ...config import JUNK_LINK_TEXT
from ...urlnorm import normalize_url
from ..page import Page
from ..profile import NAV_HREF
from ..rowtext import parse_date, parse_size
from . import ExtractContext, ExtractResult, RawEntry, Strategy, register
from .anchors import _classify_href, _page_dir

logger = logging.getLogger("OnionAccelerator.crawl.listing.rows")


def read(page: Page, spec: Mapping[str, Any], ctx: ExtractContext) -> ExtractResult:
    row_selector = str(spec.get("row") or "tr")
    link_selector = spec.get("link")
    name_selector = spec.get("name")
    size_selector = spec.get("size")
    date_selector = spec.get("date")
    dir_when = spec.get("dir_when")
    file_when = spec.get("file_when")
    skip_when = spec.get("skip")
    href_attr = str(spec.get("href_attr") or "href")

    soup = page.soup
    base = normalize_url(page.url)
    pagedir = _page_dir(page.url)
    # The structural scope test is only meaningful when the link *is* the address. On a
    # manager that keeps the path in a query parameter every row would resolve to the
    # page itself and be discarded as a sort control.
    structural = ctx.address_kind == NAV_HREF

    entries: list[RawEntry] = []
    seen: set[str] = set()
    for row in soup.select(row_selector):
        if skip_when and row.select_one(skip_when) is not None:
            continue
        link = row.select_one(link_selector) if link_selector else row.find("a", href=True)
        if link is None or not link.get(href_attr):
            continue

        href = str(link[href_attr]).strip()
        name = _text(row.select_one(name_selector)) if name_selector else _text(link)
        name = name.strip().rstrip("/")
        if not name or name.lower() in JUNK_LINK_TEXT:
            continue

        if structural:
            verdict, absolute = _classify_href(
                href, base, pagedir=pagedir, allow_offsite=ctx.allow_offsite)
            if verdict != "keep" or absolute is None:
                continue
            href = absolute
        else:
            href = urljoin(base, href)

        if href in seen:
            continue
        seen.add(href)

        row_text = row.get_text(" ", strip=True)
        size_text = _text(row.select_one(size_selector)) if size_selector else row_text
        date_text = _text(row.select_one(date_selector)) if date_selector else row_text
        entries.append(RawEntry(
            name=name,
            is_dir=_kind(row, dir_when, file_when),
            href=href,
            size_bytes=parse_size(size_text, exclude="" if size_selector else name),
            mtime_text=parse_date(date_text),
        ))

    logger.debug("rows url=%s selector=%r kept=%d", page.url, row_selector, len(entries))
    return ExtractResult(entries=entries, base_url=base, title=_page_title(soup))


def _kind(row: Tag, dir_when: Optional[str], file_when: Optional[str]) -> Optional[bool]:
    """True, False, or None for "the row does not say" -- navigation decides that case."""
    if dir_when and row.select_one(dir_when) is not None:
        return True
    if file_when and row.select_one(file_when) is not None:
        return False
    if dir_when:
        # The template named the directory marker, so its absence is an answer.
        return False
    return None


def _text(node: Optional[Tag]) -> str:
    return node.get_text(" ", strip=True) if node is not None else ""


def _page_title(soup) -> Optional[str]:
    node = soup.find("title")
    return node.get_text(" ", strip=True) if node is not None else None


register(Strategy(
    name="rows",
    options=frozenset({
        "row", "link", "name", "size", "date", "dir_when", "file_when", "skip", "href_attr",
    }),
    read=read,
    summary="table or list rows located by CSS selector.",
))
