"""URL normalisation and scope tests.

Split out of the parser because the frontier's deduplication and the parser's
filtering have to agree exactly on what "the same URL" means -- if they disagree, the
crawler either revisits directories forever or silently drops them. One
implementation, used by both.
"""

from __future__ import annotations

import posixpath
from typing import Optional
from urllib.parse import urlsplit, urlunsplit, unquote

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
    """
    base_parts = urlsplit(dir_url(base))
    cand_parts = urlsplit(normalize_url(candidate))
    if base_parts.netloc != cand_parts.netloc:
        return False
    return cand_parts.path.startswith(base_parts.path)


def path_segments(url: str) -> list[str]:
    """Decoded, non-empty path segments -- the pieces of the on-disk tree."""
    return [unquote(seg) for seg in urlsplit(url).path.split("/") if seg]


def basename(url: str) -> Optional[str]:
    """The last path segment, decoded. None for a bare host root."""
    segs = path_segments(url)
    return segs[-1] if segs else None


def depth_below(base: str, candidate: str) -> int:
    """How many path levels `candidate` sits below `base`. Negative if it is above."""
    return len(path_segments(candidate)) - len(path_segments(base))
