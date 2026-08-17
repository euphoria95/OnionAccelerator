"""URL normalisation and scope tests.

Split out of the parser because the frontier's deduplication and the parser's
filtering have to agree exactly on what "the same URL" means -- if they disagree, the
crawler either revisits directories forever or silently drops them. One
implementation, used by both.

A URL is read here in three ways, and keeping them straight is most of the work:

  * normalize_url() -- the canonical *spelling*, which stays fetchable. Whatever the
    server published goes back out on the wire; nothing below rewrites a request.
  * path_segments() -- the faithful reading, for what gets written down.
  * match_segments() -- the tolerant reading, for every scope and identity question.

The last two differ because a server may spell one path several ways, and an
application that serves a whole tree from one route reliably does.
"""

from __future__ import annotations

import posixpath
from typing import Optional
from urllib.parse import urlsplit, urlunsplit, unquote, unquote_plus

DEFAULT_PORTS = {"http": 80, "https": 443}


def normalize_url(url: str) -> str:
    """Canonical form of `url` for comparison, deduplication and storage.

    Drops the fragment (never addresses a distinct resource on the wire), lowercases
    scheme and host, removes a redundant default port, and collapses `.`/`..` and
    duplicate slashes in the path. The query is *kept*: `?id=1` and `?id=2` really are
    two pages. Sorting links are removed structurally by the parser instead.
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if ":" in host:                      # IPv6 literal, put the brackets back
        host = f"[{host}]"
    netloc = host
    if parts.port is not None and parts.port != DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{parts.port}"
    if parts.username:
        cred = parts.username
        if parts.password:
            cred = f"{cred}:{parts.password}"
        netloc = f"{cred}@{netloc}"

    path = _normalize_path(parts.path)
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def _normalize_path(path: str) -> str:
    """Collapse `//`, `.` and `..` while preserving a trailing slash.

    The trailing slash is load-bearing: it is the primary signal that an entry is a
    directory, and posixpath.normpath() throws it away.
    """
    if not path:
        return "/"
    trailing = path.endswith("/")
    collapsed = posixpath.normpath(path)
    if collapsed == ".":
        collapsed = "/"
    if trailing and not collapsed.endswith("/"):
        collapsed += "/"
    if not collapsed.startswith("/"):
        collapsed = "/" + collapsed
    return collapsed


def host_of(url: str) -> str:
    """The lowercase host of `url`, without the port. Used for per-host politeness."""
    return (urlsplit(url).hostname or "").lower()


def netloc_of(url: str) -> str:
    """Host *and* port, as normalize_url() renders it. Used for scope tests."""
    return urlsplit(normalize_url(url)).netloc


def dir_url(url: str) -> str:
    """The directory `url` lives in, as an absolute URL ending in '/'.

    For `http://h/a/b/` that is itself; for `http://h/a/b` it is `http://h/a/`. This is
    the base every relative href on the page resolves against, and the prefix the scope
    test uses.
    """
    parts = urlsplit(normalize_url(url))
    path = parts.path
    if not path.endswith("/"):
        path = path[: path.rfind("/") + 1] or "/"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def is_within(base: str, candidate: str) -> bool:
    """True if `candidate` sits at or below `base`'s directory, on the same host.

    This one test is what removes `../`, `/`, "Parent Directory" and every breadcrumb
    link from a listing, on any web server, without matching a single byte of
    server-specific markup: those links all resolve *above* the page that carries them.

    Compared segment by segment on the decoded path (see match_segments), not as a
    string prefix, so that `/files-old/` is not read as living under `/files` and a
    percent-encoded separator is read as the separator it is.
    """
    base_url = dir_url(base)
    cand_url = normalize_url(candidate)
    if urlsplit(base_url).netloc != urlsplit(cand_url).netloc:
        return False
    base_segs = match_segments(base_url)
    return match_segments(cand_url)[: len(base_segs)] == base_segs


def is_parent_dir(candidate: str, base: str) -> bool:
    """True if `candidate` is the immediate parent directory of `base`, same host.

    Segment-aligned on purpose: `/a/b` is the parent of `/a/b/c/`, but `/x` is *not* a
    parent of `/a/` merely because both descend from the root, and `/rules` is not a
    parent of `/files/sub/`. `candidate` is read as a directory in its own right -- its
    last segment is kept, not stripped -- because the up-link a listing carries
    (`/files/`, or a slash-less `/r/filemanager/TOK`) points *at* a directory.

    This is what identifies the "Parent Directory" / "../" up-link, the single most
    reliable "this page is a filesystem listing" signal after an "Index of" title and
    the one a web application essentially never emits.
    """
    if netloc_of(candidate) != netloc_of(base):
        return False
    base_segs = match_segments(normalize_url(base))
    cand_segs = match_segments(normalize_url(candidate))
    return len(cand_segs) == len(base_segs) - 1 and base_segs[: len(cand_segs)] == cand_segs


def is_same_dir(a: str, b: str) -> bool:
    """True if two URLs name the same directory, trailing slash and encoding aside.

    The self-link test. A listing that links to itself -- as a breadcrumb's last
    element, or as a slash-less spelling of its own URL -- must not record itself as one
    of its own entries.
    """
    return netloc_of(a) == netloc_of(b) and match_segments(a) == match_segments(b)


def path_segments(url: str) -> list[str]:
    """Decoded, non-empty path segments -- the pieces of the on-disk tree.

    The decode happens *before* the split, which is what makes `%2F` a level separator
    rather than a character sitting inside one segment. Some file managers address a
    nested directory by encoding its whole relative path into a single segment --
    `/r/filemanager/TOK/dumps%2Fraw/archive` is `dumps/raw/archive`, three levels
    deep. Read literally it is a directory called "dumps/raw", which is below neither
    the page that linked it nor the crawl's scope root, so every such link is discarded
    as off-tree navigation and the crawl stops dead at the first level that uses the
    encoding. Decoding first is what keeps the depth and the rendered tree agreeing with
    the filesystem the listing is describing.

    This is the *faithful* reading, and it is the one that gets written down: a `+` is
    left as the plus RFC 3986 says it is. Scope and identity go through match_segments()
    instead, which is deliberately more forgiving.
    """
    return [seg for seg in unquote(urlsplit(url).path).split("/") if seg]


def match_segments(url: str) -> list[str]:
    """Path segments as a *comparison* reads them.

    Every scope and identity test in the crawler goes through this one function, so none
    of them can end up disagreeing about what "the same path" means.

    It parts company with path_segments() in one place: `+` is read as the space a
    form-style encoder meant by it. An application serving a whole tree from a single
    route encodes the relative path as a parameter -- and does not encode it the same
    way in every link. The observed case spells one directory `Q1+2019` in the URL it
    serves the page at and `Q1%202019` in that same page's up-link. Read strictly those
    are two different directories, the up-link stops looking like a parent, and a
    listing carrying no other signal drops under the index-confidence threshold and is
    abandoned as a leaf with its whole subtree unread.

    The cost is a server that means a literal plus and does not escape it: `a+b` and
    `a b` then compare equal. That merges anything only if both spellings exist side by
    side in one parent, which is why the faithful reading stays the one on disk.
    """
    return [seg for seg in unquote_plus(urlsplit(url).path).split("/") if seg]


def dedup_key(url: str) -> str:
    """The identity two URLs share when they address the same thing.

    Coarser than normalize_url(), which is a canonical *spelling* of one URL and has to
    stay fetchable. This is a comparison key and nothing else, so it may also fold away
    the trailing slash and every encoding difference match_segments() covers -- the ways
    one directory picks up two spellings. Without it a file manager that hands out
    `/TOK/a/b` on one page and `/TOK/a%2Fb` on another gets that subtree crawled and
    downloaded twice.
    """
    normalized = normalize_url(url)
    parts = urlsplit(normalized)
    path = "/" + "/".join(match_segments(normalized))
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def basename(url: str) -> Optional[str]:
    """The last path segment, decoded. None for a bare host root."""
    segs = path_segments(url)
    return segs[-1] if segs else None


def depth_below(base: str, candidate: str) -> int:
    """How many path levels `candidate` sits below `base`. Negative if it is above."""
    return len(path_segments(candidate)) - len(path_segments(base))
