"""A web-server-agnostic parser for "Index of /" pages.

The temptation with open directories is to write one regex per server -- Apache's
`<pre>` block, Apache's `<table>` block, nginx's `<pre>`, lighttpd's table, Caddy's
template, h5ai's JavaScript shell. That approach breaks on the seventh server, and on
Tor the seventh server is common: onion operators run whatever was easiest to install,
often with a custom `autoindex` template on top.

So this module ignores the markup entirely. It takes every `<a href>` on the page and
filters *structurally*:

  * a link that resolves above the page it is on is navigation, not content --
    that single rule kills `../`, `/`, "Parent Directory" and every breadcrumb;
  * a link to the page's own path carrying only a query is a sort control --
    that kills `?C=N;O=D`, `?sort=size`, `?N=D`, `?view=list` without naming one of them;
  * a link off-host, or with a non-http scheme, is not part of this tree.

What survives is the listing. Nothing here knows what server produced it.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import warnings
from typing import Any, Iterable, Optional
from urllib.parse import urljoin, urlsplit, urlunsplit, unquote

from bs4 import BeautifulSoup
from bs4.element import Tag

try:  # pragma: no cover - bs4 moved this class between releases
    from bs4 import XMLParsedAsHTMLWarning
except ImportError:  # pragma: no cover
    class XMLParsedAsHTMLWarning(UserWarning):  # type: ignore[no-redef]
        """Stand-in for older bs4 releases that don't define it."""

from .config import INDEX_CONFIDENCE_THRESHOLD, JUNK_LINK_TEXT
from .urlnorm import basename, dir_url, is_parent_dir, is_within, normalize_url

logger = logging.getLogger("OnionAccelerator.crawl.parse")

# Which parser BeautifulSoup gets. lxml is both faster and far more forgiving of the
# unclosed tags that hand-rolled autoindex templates are full of; html.parser is the
# fallback so the module still imports on a machine without lxml.
try:  # pragma: no cover - trivial import guard
    import lxml  # noqa: F401
    _BS_PARSER = "lxml"
except ImportError:  # pragma: no cover
    _BS_PARSER = "html.parser"

# 4.1K / 12M / 1.2 GiB / 4096 / 4096 bytes. Anchored on a word boundary so it doesn't
# pick digits out of a filename.
_SIZE_RE = re.compile(
    r"(?<![\w.])(\d+(?:[.,]\d+)?)\s*(K|M|G|T|P|KB|MB|GB|TB|PB|KIB|MIB|GIB|TIB|PIB|B|BYTES)?(?![\w.])",
    re.IGNORECASE,
)
_SIZE_UNITS = {
    None: 1, "B": 1, "BYTES": 1,
    "K": 1024, "KB": 1024, "KIB": 1024,
    "M": 1024 ** 2, "MB": 1024 ** 2, "MIB": 1024 ** 2,
    "G": 1024 ** 3, "GB": 1024 ** 3, "GIB": 1024 ** 3,
    "T": 1024 ** 4, "TB": 1024 ** 4, "TIB": 1024 ** 4,
    "P": 1024 ** 5, "PB": 1024 ** 5, "PIB": 1024 ** 5,
}

# The three date shapes the common autoindex implementations emit.
_DATE_RES = (
    re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?"),           # nginx, Caddy
    re.compile(r"\d{2}-[A-Za-z]{3}-\d{4}\s+\d{2}:\d{2}(?::\d{2})?"),      # Apache
    re.compile(r"\d{4}-[A-Za-z]{3}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?"),      # lighttpd
    re.compile(r"[A-Za-z]{3}\s+\d{1,2}\s+(?:\d{4}|\d{2}:\d{2})"),         # ls -l style
)

# Schemes that are never a directory entry.
_DEAD_SCHEMES = frozenset({"javascript", "mailto", "data", "tel", "ftp", "magnet"})

# File extensions that mean "this is a file" even without a size column, used only as a
# tiebreaker when the href has no trailing slash.
_NO_EXTENSION_IS_DIR_HINT = re.compile(r"\.[A-Za-z0-9]{1,8}$")


@dataclasses.dataclass(frozen=True)
class Entry:
    """One row of a directory listing."""

    url: str
    name: str
    is_dir: bool
    size_bytes: Optional[int] = None
    mtime_text: Optional[str] = None

    def as_record(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "name": self.name,
            "is_dir": self.is_dir,
            "size_bytes": self.size_bytes,
            "mtime_text": self.mtime_text,
        }


@dataclasses.dataclass(frozen=True)
class IndexListing:
    """The result of parsing one page."""

    base_url: str
    directories: tuple[Entry, ...]
    files: tuple[Entry, ...]
    is_index: bool
    confidence: float
    title: Optional[str] = None
    generator: Optional[str] = None

    @property
    def total(self) -> int:
        return len(self.directories) + len(self.files)


# ---------------------------------------------------------------- public entry point


def parse_index(
    html: str,
    base_url: str,
    *,
    content_type: str = "",
    allow_offsite: bool = False,
) -> IndexListing:
    """Extract the directory listing from `html`, which was served at `base_url`.

    `content_type` only decides HTML vs JSON: nginx's `autoindex_format json` and
    Caddy's JSON browse emit the same information without any markup at all, and it
    would be perverse to run a HTML parser over it.
    """
    if "json" in content_type.lower():
        parsed = _parse_json_listing(html, base_url)
        if parsed is not None:
            return parsed
        # Not the listing shape we know: fall through and treat it as markup rather
        # than losing the page.

    soup = _soup(html)
    base = _effective_base(soup, base_url)
    pagedir = _page_dir(base_url)

    entries: dict[str, Entry] = {}
    total_links = 0
    sort_links = 0
    has_parent_link = False

    for anchor in soup.find_all("a", href=True):
        total_links += 1
        href = str(anchor["href"]).strip()
        verdict, url = _classify_href(href, base, pagedir=pagedir, allow_offsite=allow_offsite)
        if verdict == "sort":
            sort_links += 1
            continue
        if verdict == "parent":
            has_parent_link = True
            continue
        if verdict != "keep" or url is None:
            continue

        text = _link_text(anchor)
        if text.strip().lower().rstrip("/") in JUNK_LINK_TEXT:
            continue

        row = _row_text(anchor)
        entry = _build_entry(url, text, row, anchor)
        # Listings routinely link the same target twice (icon + name). Last one wins:
        # the text link carries the better name.
        entries[entry.url] = entry

    directories = tuple(e for e in entries.values() if e.is_dir)
    files = tuple(e for e in entries.values() if not e.is_dir)
    title = _title(soup)
    generator = _generator(soup)
    confidence = score_index(
        title=title,
        n_entries=len(entries),
        n_links=total_links,
        n_sort_links=sort_links,
        has_form=soup.find("form") is not None,
        generator=generator,
        has_parent_link=has_parent_link,
    )

    listing = IndexListing(
        base_url=base,
        directories=directories,
        files=files,
        is_index=confidence >= INDEX_CONFIDENCE_THRESHOLD,
        confidence=confidence,
        title=title,
        generator=generator,
    )
    logger.debug(
        "parsed url=%s links=%d kept=%d dirs=%d files=%d sort=%d parent=%s conf=%.2f "
        "index=%s server=%s",
        base, total_links, len(entries), len(directories), len(files),
        sort_links, has_parent_link, confidence, listing.is_index, generator,
    )
    return listing


def _soup(html: str) -> BeautifulSoup:
    """Parse `html` as HTML, whatever it claims to be.

    lighttpd and a few custom templates serve XHTML with an XML declaration, which makes
    bs4 warn that an XML document is being parsed as HTML. It is the right call here --
    an autoindex page is HTML in practice, and the strict XML parser would reject the
    unclosed tags that hand-rolled templates are full of -- so the warning is suppressed
    at exactly this call rather than globally.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
        return BeautifulSoup(html, _BS_PARSER)


def score_index(
    *,
    title: Optional[str],
    n_entries: int,
    n_links: int,
    n_sort_links: int,
    has_form: bool,
    generator: Optional[str] = None,
    has_parent_link: bool = False,
) -> float:
    """How confident we are that this page is a directory index, in [0, 1].

    This exists to stop the crawler walking into an *application*. A forum, a shop or a
    wiki has hundreds of in-scope links too, and recursing into one turns a
    twenty-request directory walk into an unbounded site crawl over Tor. The signals
    are deliberately independent of any one server's markup.

    The failure mode this guards against is a false *positive* -- crawling an app -- but
    the count-based signals (`n_entries >= 2`, the entry/link ratio) also produce false
    *negatives*: a real directory holding a single file or a single subdirectory scores
    below the bar and is abandoned as a leaf, silently pruning whatever is under it. The
    parent-directory up-link is the structural signal that rescues those cases without
    lowering the guard against apps, because a listing has one and an application does
    not.
    """
    score = 0.0
    if title and title.strip().lower().startswith(("index of", "directory listing")):
        score += 0.5
    if generator:
        # An <address> footer naming the daemon is what serves a *generated* page; a
        # hand-built site rarely has one.
        score += 0.2
    if n_sort_links:
        # Column-sorting links are the fingerprint of an autoindex and of nothing else.
        score += 0.3
    if has_parent_link:
        # An up-link to the immediate parent directory is what a filesystem listing has
        # and an application does not. On a custom autoindex with no "Index of" title and
        # no server footer it is often the only positive signal a near-empty directory
        # carries, so without it a directory holding one entry falls below the threshold
        # and is wrongly recorded as a leaf.
        score += 0.3
    if n_entries >= 2:
        score += 0.2
    if not has_form:
        score += 0.1
    if n_links and n_entries / n_links >= 0.6:
        score += 0.2
    return min(score, 1.0)


# ---------------------------------------------------------------- href classification


def _page_dir(url: str) -> str:
    """The directory the *page itself* represents, as a URL ending in '/'.

    Distinct from _effective_base(): when a directory is served at a URL without a
    trailing slash and without a redirect -- as some onion file managers do -- the page
    still *is* that directory. _effective_base() collapses such a URL to its parent so
    relative hrefs resolve the way a browser resolves them, but the parent-link test has
    to measure against `/a/b/`, not the `/a/` that resolution happens to use, or it would
    read the page's own up-link as pointing at the page itself.
    """
    parts = urlsplit(normalize_url(url))
    path = parts.path if parts.path.endswith("/") else parts.path + "/"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _classify_href(
    href: str, base: str, *, pagedir: str, allow_offsite: bool
) -> tuple[str, Optional[str]]:
    """Decide what one href is: 'keep', 'sort', 'parent', or 'drop'.

    Returns the normalised absolute URL alongside 'keep'; 'parent' and 'sort' are
    signals for the index score rather than entries, so they carry no URL.
    """
    if not href or href.startswith("#"):
        return "drop", None

    scheme = href.split(":", 1)[0].lower() if ":" in href.split("/", 1)[0] else ""
    if scheme in _DEAD_SCHEMES:
        return "drop", None

    try:
        absolute = normalize_url(urljoin(base, href))
    except ValueError:
        return "drop", None

    parts = urlsplit(absolute)
    if parts.scheme not in ("http", "https"):
        return "drop", None

    base_parts = urlsplit(base)
    # A link to this very page that differs only by its query string is a control, not
    # an entry: sorting, view switching, paging within the same directory.
    if parts.netloc == base_parts.netloc and parts.path == base_parts.path and parts.query:
        return "sort", None
    # ...and one with neither a query nor a difference is a self-link.
    if absolute == base:
        return "drop", None

    if not allow_offsite and parts.netloc != base_parts.netloc:
        return "drop", None

    # The up-link to the immediate parent directory. Caught here, before the scope test
    # below drops it for resolving above the page: its *presence* is a strong signal the
    # page is a real listing, even though the link itself is navigation, not an entry.
    if is_parent_dir(absolute, pagedir):
        return "parent", None

    # The rule that does most of the work: anything resolving above this directory is
    # navigation. On any server, in any markup.
    if not is_within(base, absolute):
        return "drop", None

    return "keep", absolute


def _build_entry(url: str, text: str, row: str, anchor: Tag) -> Entry:
    """Assemble one Entry, inferring the name, the kind, and any row metadata."""
    name = text.strip().rstrip("/") or (basename(url) or url)
    name = unquote(name)
    is_dir = _looks_like_dir(url, text, row, anchor)
    size = None if is_dir else _parse_size(row, exclude=text)
    return Entry(
        url=url,
        name=name,
        is_dir=is_dir,
        size_bytes=size,
        mtime_text=_parse_date(row),
    )


def _looks_like_dir(url: str, text: str, row: str, anchor: Tag) -> bool:
    """Is this entry a directory?

    The trailing slash is authoritative when present -- every autoindex emits it,
    because a directory link without one costs the browser a redirect. The fallbacks
    exist for hand-written templates that strip it.
    """
    if urlsplit(url).path.endswith("/"):
        return True
    if text.strip().endswith("/"):
        return True
    if _icon_says_dir(anchor):
        return True
    # No extension and no size in the row: a file with neither is possible but rare,
    # while a directory with neither is the normal case for a stripped-slash template.
    name = basename(url) or ""
    if not _NO_EXTENSION_IS_DIR_HINT.search(name) and _parse_size(row, exclude=text) is None:
        return True
    return False


def _icon_says_dir(anchor: Tag) -> bool:
    """Apache-style `[DIR]` / folder-icon markers, wherever the template put them."""
    row = anchor.find_parent(["tr", "li"]) or anchor.parent
    if row is None:
        return False
    for img in row.find_all("img", limit=4):
        alt = str(img.get("alt", "")).strip().upper()
        src = str(img.get("src", "")).lower()
        if alt in ("[DIR]", "[   ]", "DIR") or "folder" in src or "back.gif" in src:
            return alt in ("[DIR]", "DIR") or "folder" in src
    classes = " ".join(anchor.get("class", []) or []) + " " + " ".join(
        (row.get("class", []) or []) if isinstance(row, Tag) else []
    )
    return "dir" in classes.lower() or "folder" in classes.lower()


# ---------------------------------------------------------------- text extraction


def _link_text(anchor: Tag) -> str:
    """The anchor's visible text, or its title/alt if it renders as an icon only."""
    text = anchor.get_text(" ", strip=True)
    if text:
        return text
    for attr in ("title", "aria-label"):
        value = anchor.get(attr)
        if value:
            return str(value).strip()
    img = anchor.find("img")
    if img is not None and img.get("alt"):
        return str(img["alt"]).strip()
    return ""


def _row_text(anchor: Tag) -> str:
    """The listing row this anchor belongs to, as flat text.

    Table and list layouts have a real container. Apache's and nginx's `<pre>` layouts
    do not -- the size and date are bare text siblings, terminated by the newline that
    starts the next row -- so those are walked by hand.
    """
    container = anchor.find_parent(["tr", "li"])
    if container is not None:
        return container.get_text(" ", strip=True)

    pieces: list[str] = []
    for sibling in anchor.next_siblings:
        if isinstance(sibling, Tag):
            if sibling.name in ("br", "a", "hr"):
                break
            pieces.append(sibling.get_text(" ", strip=True))
            continue
        chunk = str(sibling)
        if "\n" in chunk:
            pieces.append(chunk.split("\n", 1)[0])
            break
        pieces.append(chunk)
    return " ".join(p for p in pieces if p).strip()


def _title(soup: BeautifulSoup) -> Optional[str]:
    """The page's title, preferring <h1> because templates often only set that one."""
    for tag in ("h1", "title"):
        node = soup.find(tag)
        if node is not None:
            text = node.get_text(" ", strip=True)
            if text:
                return text
    return None


def _generator(soup: BeautifulSoup) -> Optional[str]:
    """The server signature an autoindex footer leaves behind, if any."""
    address = soup.find("address")
    if address is not None:
        text = address.get_text(" ", strip=True)
        if text:
            return text[:200]
    return None


def _effective_base(soup: BeautifulSoup, base_url: str) -> str:
    """The URL relative hrefs resolve against: `<base href>` if present, else the page.

    Normalised to a directory so that a listing served at `/a/b` (no trailing slash,
    which some servers allow) still resolves its children into `/a/b/` rather than
    `/a/`.
    """
    tag = soup.find("base", href=True)
    if tag is not None:
        try:
            return dir_url(urljoin(base_url, str(tag["href"])))
        except ValueError:
            pass
    normalized = normalize_url(base_url)
    if urlsplit(normalized).path.endswith("/"):
        return normalized
    return dir_url(normalized)


# ---------------------------------------------------------------- metadata scraping


def _parse_size(row: str, *, exclude: str = "") -> Optional[int]:
    """Best-effort byte count from a listing row.

    Two things are stripped before anything is matched, and both are load-bearing:

      * `exclude`, the entry's own name, so `leak-2024.tar.gz` doesn't donate its year;
      * the timestamp, because `2023-11-02 14:29` is nothing but digits and a naive
        scan reads the year as the file size -- which then makes every directory look
        like a file, since "has a size" is what distinguishes the two.

    Directories are conventionally `-`, which matches nothing and correctly yields None.
    """
    if not row:
        return None
    haystack = row.replace(exclude, " ", 1) if exclude else row
    for pattern in _DATE_RES:
        haystack = pattern.sub(" ", haystack)
    best: Optional[int] = None
    for match in _SIZE_RE.finditer(haystack):
        number, unit = match.group(1), match.group(2)
        key = unit.upper() if unit else None
        if key not in _SIZE_UNITS:
            continue
        if key is None and "." in number:
            continue                      # a bare decimal is a version, not a size
        try:
            value = float(number.replace(",", "."))
        except ValueError:
            continue
        candidate = int(value * _SIZE_UNITS[key])
        # A unitless number could be anything on the row (a date part, a permission
        # mask). One carrying a unit is unambiguous, so it wins outright.
        if key not in (None, "B", "BYTES"):
            return candidate
        if best is None:
            best = candidate
    return best


def _parse_date(row: str) -> Optional[str]:
    """The modification timestamp as it appears in the row, verbatim.

    Kept as text on purpose: the listing carries no timezone, so parsing it into a
    datetime would invent precision that isn't there. For CTI, what the server said is
    the evidence.
    """
    for pattern in _DATE_RES:
        match = pattern.search(row)
        if match:
            return match.group(0)
    return None


# ---------------------------------------------------------------- JSON listings


def _parse_json_listing(body: str, base_url: str) -> Optional[IndexListing]:
    """nginx `autoindex_format json` / Caddy JSON browse.

    Returns None -- rather than an empty listing -- if the payload isn't a listing, so
    the caller can fall back to the HTML path instead of concluding the directory is
    empty.
    """
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
        return None

    base = dir_url(base_url)
    entries: list[Entry] = []
    for row in data:
        name = str(row.get("name") or row.get("Name") or "").strip()
        if not name or name in ("..", "."):
            continue
        kind = str(row.get("type") or row.get("Type") or "").lower()
        is_dir = kind in ("directory", "dir") or bool(row.get("IsDir"))
        url = normalize_url(urljoin(base, name + ("/" if is_dir and not name.endswith("/") else "")))
        if not is_within(base, url):
            continue
        size = row.get("size", row.get("Size"))
        entries.append(Entry(
            url=url,
            name=name.rstrip("/"),
            is_dir=is_dir,
            size_bytes=int(size) if isinstance(size, (int, float)) and not is_dir else None,
            mtime_text=str(row["mtime"]) if row.get("mtime") else (
                str(row["ModTime"]) if row.get("ModTime") else None
            ),
        ))

    return IndexListing(
        base_url=base,
        directories=tuple(e for e in entries if e.is_dir),
        files=tuple(e for e in entries if not e.is_dir),
        is_index=True,          # a JSON listing is unambiguous; nothing else emits one
        confidence=1.0,
        title=None,
        generator="json-autoindex",
    )


def iter_entries(listing: IndexListing) -> Iterable[Entry]:
    """Directories first, then files -- the order the report renders them in."""
    yield from listing.directories
    yield from listing.files
