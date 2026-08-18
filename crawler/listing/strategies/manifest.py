"""The target's own index, read instead of crawled.

Leak sites publish these. The onion this crawler was built against serves an 11 MB
`tree -h -f` dump in its root -- 94,228 entries, "7237 directories, 86992 files" -- and
reading it is one request where walking the same tree is seven and a half thousand over
Tor, each of them a fresh circuit's worth of latency. Where a dump exists it is also the
only honest way to measure recall: what the crawl found against what the server says it
has.

Four shapes, because these files are whatever somebody's shell produced:

    tree -f      ├── [ 12K]  ./dumps/raw/part.bin
    find         ./dumps/raw/part.bin
    ls -lR       ./dumps/raw:  /  -rw-r--r-- 1 u g 12288 Nov  2 14:29 part.bin
    sitemap      <loc>https://host/dumps/raw/part.bin</loc>

Directories are not taken from the decoration at all -- a `tree` dump gives a directory
the same `[4.0K]` size it gives a file -- but from the paths themselves: anything that is
another entry's parent is a directory. That rule is exact, works on all four formats, and
leaves exactly one gap, which `_entry()` explains: an *empty* directory is a leaf, and no
dump format marks it.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterator, Mapping, Optional
from urllib.parse import unquote, urlsplit

from ..page import Page
from . import ExtractContext, ExtractResult, RawEntry, Strategy, register

logger = logging.getLogger("OnionAccelerator.crawl.listing.manifest")

# The box-drawing scaffold `tree` draws down the left of every line, plus its ASCII
# fallback (`tree --charset=ascii`).
_TREE_PREFIX = re.compile(r"^[\s│|`+\-├└─]*")
# `tree -h` / `tree -s` put the size in brackets before the path.
_TREE_SIZE = re.compile(r"^\[\s*([0-9.,]+[KMGTP]?)\s*\]\s+")
# The summary line every tree dump ends with.
_TREE_SUMMARY = re.compile(r"^\d+ directories?(, \d+ files?)?$", re.IGNORECASE)
# `ls -lR`: a block header, and a long-format entry.
_LS_HEADER = re.compile(r"^(\S.*):$")
# `-rw-r--r-- 1 root root 536870912 Nov  2 14:29 dump.sql.gz`. The date is matched
# explicitly rather than as "some words": a lazy `(.+?)` between the size and the name
# stops at the first space and takes half the timestamp into the filename.
_LS_ENTRY = re.compile(
    r"^([-dlbcps])[rwxsStT-]{9}[.+]?\s+\d+\s+\S+\s+\S+\s+(\d+)\s+"
    r"(\w{3}\s+\d{1,2}\s+(?:\d{2}:\d{2}|\d{4}))\s+(\S.*)$")
_SITEMAP_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)

_HAS_EXTENSION = re.compile(r"\.[A-Za-z0-9]{1,8}$")

_SIZE_SUFFIX = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4, "P": 1024 ** 5}

FORMATS = ("auto", "tree", "find", "ls", "sitemap")


def read(page: Page, spec: Mapping[str, Any], ctx: ExtractContext) -> ExtractResult:
    fmt = str(spec.get("format") or "auto")
    if fmt not in FORMATS:
        fmt = "auto"
    if fmt == "auto":
        fmt = detect_format(page.body)

    strip = str(spec.get("strip_prefix") or "")
    max_entries = int(spec.get("max_entries") or 500_000)

    sizes: dict[str, Optional[int]] = {}
    order: list[str] = []
    for path, size in _parse(page.body, fmt, strip):
        if path not in sizes:
            order.append(path)
            if len(order) >= max_entries:
                logger.warning("manifest %s: stopping at max_entries=%d", page.url, max_entries)
                break
        if size is not None or path not in sizes:
            sizes[path] = size

    parents = _parents(order)
    entries = [_entry(path, path in parents, sizes.get(path)) for path in order]

    logger.info("manifest %s: %s format, %d entries (%d directories)",
                page.url, fmt, len(entries), len(parents))
    return ExtractResult(
        entries=entries,
        is_index=bool(entries),
        confidence=1.0 if entries else 0.0,
        generator=f"manifest:{fmt}",
        # The dump *is* the whole subtree. Queueing its directories would fetch every one
        # of them again, which is the cost the manifest exists to avoid.
        expandable=False,
    )


def _entry(path: str, is_parent: bool, size: Optional[int]) -> RawEntry:
    """One path as a row, deciding what it is.

    Anything another entry lives under is a directory, and that rule is exact. A *leaf* is
    ambiguous and cannot be made otherwise: `tree` gives an empty directory the same
    `[4.0K]` block size it gives a file, and `find` gives neither a size nor a marker. So
    a leaf falls back to the rule the rest of the crawler uses -- an extension means a
    file -- and an empty directory whose name looks like `notes.txt` will be recorded as a
    file. That is the one thing a dump genuinely cannot tell you.
    """
    name = path.rsplit("/", 1)[-1]
    if is_parent:
        return RawEntry(name=name, is_dir=True, path=path)
    if _HAS_EXTENSION.search(name):
        return RawEntry(name=name, is_dir=False, path=path, size_bytes=size)
    # No extension and no children: read as an empty directory, and drop the size, which
    # for a directory is the block size rather than anything about its contents.
    return RawEntry(name=name, is_dir=True, path=path)


def detect_format(body: str) -> str:
    """Guess which shell produced this. Cheap: the first few hundred lines decide."""
    head = body[:20000]
    if _SITEMAP_LOC.search(head):
        return "sitemap"
    if "├──" in head or "└──" in head or "|--" in head or "`--" in head:
        return "tree"
    for line in head.splitlines()[:200]:
        if _LS_ENTRY.match(line.strip()):
            return "ls"
    return "find"


def _parse(body: str, fmt: str, strip: str) -> Iterator[tuple[str, Optional[int]]]:
    if fmt == "sitemap":
        yield from _parse_sitemap(body, strip)
    elif fmt == "ls":
        yield from _parse_ls(body, strip)
    elif fmt == "tree":
        yield from _parse_tree(body, strip)
    else:
        yield from _parse_find(body, strip)


def _parse_tree(body: str, strip: str) -> Iterator[tuple[str, Optional[int]]]:
    for raw in body.splitlines():
        line = _TREE_PREFIX.sub("", raw).strip()
        if not line or _TREE_SUMMARY.match(line):
            continue
        size = None
        match = _TREE_SIZE.match(line)
        if match:
            size = _human_bytes(match.group(1))
            line = line[match.end():].strip()
        # `tree -f` prints full paths; without -f it prints bare names, which cannot be
        # assembled without tracking indentation. Those lines are skipped rather than
        # guessed at -- a wrong path is worse than a missing one in a manifest.
        if "/" not in line and not line.startswith("."):
            continue
        path = _clean(line, strip)
        if path:
            yield path, size


def _parse_find(body: str, strip: str) -> Iterator[tuple[str, Optional[int]]]:
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        path = _clean(line, strip)
        if path:
            yield path, None


def _parse_ls(body: str, strip: str) -> Iterator[tuple[str, Optional[int]]]:
    current = ""
    for raw in body.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        header = _LS_HEADER.match(line)
        if header:
            current = _clean(header.group(1), strip)
            continue
        entry = _LS_ENTRY.match(line.strip())
        if not entry:
            continue
        kind, size, _date, name = entry.groups()
        name = name.split(" -> ", 1)[0].strip()
        if name in (".", ".."):
            continue
        path = _clean(f"{current}/{name}" if current else name, strip)
        if path:
            yield path, None if kind == "d" else int(size)


def _parse_sitemap(body: str, strip: str) -> Iterator[tuple[str, Optional[int]]]:
    for match in _SITEMAP_LOC.finditer(body):
        path = _clean(urlsplit(match.group(1)).path, strip)
        if path:
            yield path, None


def _clean(path: str, strip: str) -> str:
    """One entry's path, as a relative path with no decoration."""
    path = unquote(path.strip().strip('"'))
    if strip and path.startswith(strip):
        path = path[len(strip):]
    path = path.lstrip("./").lstrip("/")
    while path.startswith("./"):
        path = path[2:]
    return path.rstrip("/")


def _parents(paths: list[str]) -> set[str]:
    """Every path that some other path lives under. That is the directory set."""
    parents: set[str] = set()
    for path in paths:
        parts = path.split("/")
        for i in range(1, len(parts)):
            parents.add("/".join(parts[:i]))
    return parents


def _human_bytes(value: str) -> Optional[int]:
    value = value.strip().replace(",", "")
    if not value:
        return None
    suffix = value[-1].upper()
    if suffix in _SIZE_SUFFIX:
        try:
            return int(float(value[:-1]) * _SIZE_SUFFIX[suffix])
        except ValueError:
            return None
    try:
        return int(float(value))
    except ValueError:
        return None


register(Strategy(
    name="manifest",
    options=frozenset({"format", "strip_prefix", "max_entries"}),
    read=read,
    summary="a published tree/find/ls-lR/sitemap dump: one request for a whole subtree.",
))
