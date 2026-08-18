"""The structural reader: every `<a href>`, filtered by where it points.

This is the strategy that needs no template, and it is deliberately the default and the
fallback. The temptation with open directories is to write one regex per server --
Apache's `<pre>` block, Apache's `<table>` block, nginx's `<pre>`, lighttpd's table,
Caddy's template, h5ai's JavaScript shell. That approach breaks on the seventh server,
and on Tor the seventh server is common: onion operators run whatever was easiest to
install, often with a custom `autoindex` template on top.

So this module ignores the markup entirely. It takes every `<a href>` on the page and
filters *structurally*:

  * a link that resolves above the page it is on is navigation, not content --
    that single rule kills `../`, `/`, "Parent Directory" and every breadcrumb;
  * a link to the page's own path carrying only a query is a sort control --
    that kills `?C=N;O=D`, `?sort=size`, `?N=D`, `?view=list` without naming one of them;
  * a link off-host, or with a non-http scheme, is not part of this tree.

What survives is the listing. Nothing here knows what server produced it -- which is why
the profiles for Apache, nginx, lighttpd and Caddy all point at this one strategy and
differ only in how they *recognise* the target and what they know about its addressing.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Mapping, Optional
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from bs4.element import Tag

from ...config import INDEX_CONFIDENCE_THRESHOLD, JUNK_LINK_TEXT
from ...urlnorm import basename, dir_url, is_parent_dir, is_same_dir, is_within, normalize_url
from ..page import Page
from ..rowtext import parse_date, parse_size
from . import ExtractContext, ExtractResult, RawEntry, Strategy, register

logger = logging.getLogger("OnionAccelerator.crawl.listing.anchors")

# Schemes that are never a directory entry.
_DEAD_SCHEMES = frozenset({"javascript", "mailto", "data", "tel", "ftp", "magnet"})

# A file extension means "this is a file" even without a size column. Used only as a
# tiebreaker when the href has no trailing slash.
_HAS_EXTENSION = re.compile(r"\.[A-Za-z0-9]{1,8}$")


def read(page: Page, spec: Mapping[str, Any], ctx: ExtractContext) -> ExtractResult:
    """Extract the listing from `page`, structurally."""
    junk = set(JUNK_LINK_TEXT) | {
        str(t).strip().lower() for t in (spec.get("junk_text") or ())
    }
    threshold = float(spec.get("min_confidence", INDEX_CONFIDENCE_THRESHOLD))

    soup = page.soup
    base = _effective_base(soup, page.url)
    pagedir = _page_dir(page.url)

    entries: dict[str, RawEntry] = {}
    total_links = 0
    sort_links = 0
    has_parent_link = False

    for anchor in soup.find_all("a", href=True):
        total_links += 1
        href = str(anchor["href"]).strip()
        verdict, url = _classify_href(
            href, base, pagedir=pagedir, allow_offsite=ctx.allow_offsite)
        if verdict == "sort":
            sort_links += 1
            continue
        if verdict == "parent":
            has_parent_link = True
            continue
        if verdict != "keep" or url is None:
            continue

        text = _link_text(anchor)
        if text.strip().lower().rstrip("/") in junk:
            continue

        row = _row_text(anchor)
        # Listings routinely link the same target twice (icon + name). Last one wins:
        # the text link carries the better name.
        entries[url] = _build(url, text, row, anchor)

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

    logger.debug(
        "anchors url=%s links=%d kept=%d sort=%d parent=%s conf=%.2f server=%s",
        base, total_links, len(entries), sort_links, has_parent_link, confidence, generator,
    )
    return ExtractResult(
        entries=list(entries.values()),
        is_index=confidence >= threshold,
        confidence=confidence,
        title=title,
        generator=generator,
        base_url=base,
    )


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

    A page that a *named* profile matched never reaches this function: a template's match
    is stronger evidence than any of these signals, and the guard exists precisely for
    the case where there is no template to trust.
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

    if not allow_offsite and parts.netloc != base_parts.netloc:
        return "drop", None

    # The up-link to the immediate parent directory. Caught here, before the scope test
    # below drops it for resolving above the page: its *presence* is a strong signal the
    # page is a real listing, even though the link itself is navigation, not an entry.
    #
    # Tested before the self-link rule, not after: when the page is a directory served
    # without a trailing slash, `base` has already been collapsed to that directory's
    # parent, so the up-link and `base` are the same URL and a self-link test run first
    # would swallow the signal it exists to detect.
    if is_parent_dir(absolute, pagedir):
        return "parent", None

    # A link to the page's own directory is a self-link: the last breadcrumb, or a
    # slash-less spelling of this very URL.
    if is_same_dir(absolute, pagedir):
        return "drop", None

    # The rule that does most of the work: anything resolving above this directory is
    # navigation. On any server, in any markup.
    if not is_within(base, absolute):
        return "drop", None

    return "keep", absolute


def _build(url: str, text: str, row: str, anchor: Tag) -> RawEntry:
    """Assemble one row, inferring the name, the kind, and any row metadata."""
    name = text.strip().rstrip("/") or (basename(url) or url)
    name = unquote(name)
    is_dir = _looks_like_dir(url, text, row, anchor)
    return RawEntry(
        name=name,
        is_dir=is_dir,
        href=url,
        size_bytes=None if is_dir else parse_size(row, exclude=text),
        mtime_text=parse_date(row),
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
    if not _HAS_EXTENSION.search(name) and parse_size(row, exclude=text) is None:
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


def _title(soup) -> Optional[str]:
    """The page's title, preferring <h1> because templates often only set that one."""
    for tag in ("h1", "title"):
        node = soup.find(tag)
        if node is not None:
            text = node.get_text(" ", strip=True)
            if text:
                return text
    return None


def _generator(soup) -> Optional[str]:
    """The server signature an autoindex footer leaves behind, if any."""
    address = soup.find("address")
    if address is not None:
        text = address.get_text(" ", strip=True)
        if text:
            return text[:200]
    return None


def _effective_base(soup, base_url: str) -> str:
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


register(Strategy(
    name="anchors",
    options=frozenset({"junk_text", "min_confidence"}),
    read=read,
    summary="every <a href>, filtered by where it points. Needs no template.",
))
