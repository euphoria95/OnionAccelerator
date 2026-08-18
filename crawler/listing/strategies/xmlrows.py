"""Listings that arrive as XML: WebDAV's PROPFIND and S3-compatible bucket listings.

Both are worth having because both are common behind an onion and neither is readable by
any of the other strategies: a `D:multistatus` has no anchors, and an S3 bucket listing
addresses its objects by full key rather than by link.

Namespace prefixes are matched loosely (`D:response`, `d:response` and `response` are the
same element) because which prefix a server picks is arbitrary, and a template that had to
know would break on the next server that picked a different letter.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional
from urllib.parse import unquote

from bs4.element import Tag

from ..page import Page
from . import ExtractContext, ExtractResult, RawEntry, Strategy, register

logger = logging.getLogger("OnionAccelerator.crawl.listing.xml")


def read(page: Page, spec: Mapping[str, Any], ctx: ExtractContext) -> ExtractResult:
    soup = page.xml
    row_name = str(spec.get("rows") or "response")
    name_tag = spec.get("name")
    href_tag = spec.get("href")
    size_tag = spec.get("size")
    date_tag = spec.get("date")
    dir_when = spec.get("dir_when")
    path_is_name = bool(spec.get("path_is_name"))

    entries: list[RawEntry] = []
    for row in _find_all(soup, row_name):
        href = _text(row, href_tag) if href_tag else None
        raw_name = _text(row, name_tag) if name_tag else None
        name = raw_name or _basename(href or "")
        if not name:
            continue
        is_dir = None
        if dir_when:
            is_dir = _find_one(row, dir_when) is not None
        entries.append(RawEntry(
            name=_basename(name),
            is_dir=is_dir,
            href=href,
            path=(raw_name or href) if path_is_name else None,
            size_bytes=_as_int(_text(row, size_tag)) if size_tag else None,
            mtime_text=_text(row, date_tag) if date_tag else None,
        ))

    # A bucket listing keeps its "directories" in a separate element entirely.
    dir_rows = spec.get("dir_rows")
    if dir_rows:
        dir_name_tag = str(spec.get("dir_name") or "prefix")
        for row in _find_all(soup, str(dir_rows)):
            value = _text(row, dir_name_tag)
            if not value:
                continue
            entries.append(RawEntry(
                name=_basename(value.rstrip("/")),
                is_dir=True,
                path=value if path_is_name else None,
                href=None if path_is_name else value,
            ))

    cursor = _text(soup, str(spec["cursor_field"])) if spec.get("cursor_field") else None
    more = _text(soup, str(spec["more_field"])) if spec.get("more_field") else ""

    logger.debug("xml url=%s rows=%d cursor=%s", page.url, len(entries), cursor)
    return ExtractResult(
        entries=entries,
        is_index=bool(entries) or _find_one(soup, row_name) is not None,
        confidence=1.0,
        generator="xml",
        cursor=cursor or None,
        has_more=more.strip().lower() == "true" or bool(cursor),
    )


def _find_all(node, name: str) -> list[Tag]:
    """Every descendant with this local name, whatever namespace prefix it carries."""
    wanted = name.split(":")[-1].lower()
    return [
        tag for tag in node.find_all(True)
        if str(tag.name).split(":")[-1].lower() == wanted
    ]


def _find_one(node, name: str) -> Optional[Tag]:
    found = _find_all(node, name)
    return found[0] if found else None


def _text(node, name: Optional[str]) -> str:
    if not name:
        return ""
    tag = _find_one(node, name)
    return tag.get_text(strip=True) if tag is not None else ""


def _basename(value: str) -> str:
    return unquote(value.rstrip("/").rsplit("/", 1)[-1])


def _as_int(value: str) -> Optional[int]:
    value = (value or "").strip()
    return int(value) if value.isdigit() else None


register(Strategy(
    name="xml",
    options=frozenset({
        "rows", "name", "href", "size", "date", "dir_when",
        "dir_rows", "dir_name", "path_is_name", "cursor_field", "more_field",
    }),
    read=read,
    summary="XML elements: WebDAV PROPFIND responses, S3 bucket listings.",
))
