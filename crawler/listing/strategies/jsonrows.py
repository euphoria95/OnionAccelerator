"""Listings that arrive as JSON: nginx's `autoindex_format json`, Caddy's browse, and
every file-manager API.

The defaults here are the two no-template cases -- an array of
`{name, type, size, mtime}` is what nginx and Caddy emit, and a template that names no
fields reads exactly that. Everything else is one line per field: where the array lives
(`rows = "data.content"`), which key holds the name, which one says it is a directory.

Returning no entries and `is_index = False` for a body that is not a listing is
deliberate and load-bearing: a JSON error page must not be read as "this directory is
empty", because empty is a fact the crawler would record and move on from.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Mapping, Optional

from ..page import Page
from . import ExtractContext, ExtractResult, RawEntry, Strategy, register

logger = logging.getLogger("OnionAccelerator.crawl.listing.json")

# What nginx and Caddy call things, so the no-options case works on both.
_NAME_KEYS = ("name", "Name", "filename")
_SIZE_KEYS = ("size", "Size", "bytes")
_DATE_KEYS = ("mtime", "ModTime", "modified", "mod_time")
_TYPE_KEYS = ("type", "Type", "mime")
_DIR_FLAG_KEYS = ("is_dir", "isDir", "IsDir", "dir", "directory")
_DIR_TYPES = ("directory", "dir", "folder")


def read(page: Page, spec: Mapping[str, Any], ctx: ExtractContext) -> ExtractResult:
    data = page.json
    if data is None:
        logger.debug("json url=%s body is not JSON", page.url)
        return ExtractResult(entries=[], is_index=False, confidence=0.0)

    rows = _dig(data, str(spec.get("rows") or ""))
    if not isinstance(rows, list):
        logger.debug("json url=%s no row array at %r", page.url, spec.get("rows") or "<root>")
        return ExtractResult(entries=[], is_index=False, confidence=0.0)

    name_key = spec.get("name")
    size_key = spec.get("size")
    date_key = spec.get("date")
    path_key = spec.get("path")
    href_key = spec.get("href")
    dir_field = spec.get("dir_field")
    dir_value = spec.get("dir_value")
    type_field = spec.get("type_field")

    entries: list[RawEntry] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        name = str(_first(row, [name_key] if name_key else _NAME_KEYS) or "").strip()
        if not name or name in (".", ".."):
            continue
        entries.append(RawEntry(
            name=name.rstrip("/"),
            is_dir=_is_dir(row, dir_field, dir_value, type_field),
            href=_maybe_str(_first(row, [href_key])) if href_key else None,
            path=_maybe_str(_first(row, [path_key])) if path_key else None,
            size_bytes=_as_int(_first(row, [size_key] if size_key else _SIZE_KEYS)),
            mtime_text=_maybe_str(_first(row, [date_key] if date_key else _DATE_KEYS)),
        ))

    cursor = _maybe_str(_dig(data, str(spec.get("cursor_field") or ""))) \
        if spec.get("cursor_field") else None
    more = bool(_dig(data, str(spec.get("more_field")))) if spec.get("more_field") else False

    logger.debug("json url=%s rows=%d cursor=%s", page.url, len(entries), cursor)
    return ExtractResult(
        entries=entries,
        # A JSON listing is unambiguous: nothing but a listing endpoint emits one.
        is_index=True,
        confidence=1.0,
        generator="json",
        cursor=cursor,
        has_more=more or bool(cursor),
    )


def _dig(data: Any, path: str) -> Any:
    """Follow a dotted key path. An empty path is the document itself."""
    if not path:
        return data
    current = data
    for key in path.split("."):
        if isinstance(current, Mapping) and key in current:
            current = current[key]
        else:
            return None
    return current


def _first(row: Mapping[str, Any], keys: Iterable[Optional[str]]) -> Any:
    for key in keys:
        if key and key in row:
            return row[key]
    return None


def _is_dir(row: Mapping[str, Any], dir_field: Optional[str],
            dir_value: Optional[str], type_field: Optional[str]) -> Optional[bool]:
    if dir_field:
        value = row.get(dir_field)
        if dir_value is not None:
            return str(value).lower() == str(dir_value).lower()
        return bool(value)
    for key in _DIR_FLAG_KEYS:
        if key in row and isinstance(row[key], bool):
            return row[key]
    kind = _first(row, [type_field] if type_field else _TYPE_KEYS)
    if kind is not None:
        return str(kind).lower() in _DIR_TYPES
    return None


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _maybe_str(value: Any) -> Optional[str]:
    return None if value is None else str(value)


register(Strategy(
    name="json",
    options=frozenset({
        "rows", "name", "size", "date", "path", "href",
        "dir_field", "dir_value", "type_field", "cursor_field", "more_field",
    }),
    read=read,
    summary="a JSON array of rows, at the document root or at a dotted path.",
))
