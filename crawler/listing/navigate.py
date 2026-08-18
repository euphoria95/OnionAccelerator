"""How a listed row becomes the next request -- the part every target disagrees about.

The strategies answer "where are the rows". This answers "and how do I ask for one of
them", which is the question the old crawler could only answer one way: GET the href. The
four kinds cover everything seen in the wild:

    href      the link is the address           /dumps/raw/
    query     the path is a query parameter     /index.php?p=dumps/raw
    encoded   the path is one escaped segment   /r/fm/TOK/dumps%2Fraw
    api       the path is a field in a request  POST /api/fs/list {"path": "/dumps/raw"}

Everything that differs between two file managers of the same family lives in those four
lines of a template. Everything else -- what a size means, which rows are junk, how the
tree is recorded -- is shared.

Template placeholders are substituted by name rather than through `str.format`, because
an API body is JSON and JSON is made of braces. `{path}` is percent-encoded when it lands
in a URL and JSON-escaped when it lands in a body; `{path_raw}` is neither, for the rare
target that wants it verbatim.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
from typing import Optional
from urllib.parse import (
    parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit,
)

from ..urlnorm import dir_url, match_segments, normalize_url
from .model import Entry, PageRequest
from .profile import NAV_API, NAV_ENCODED, NAV_HREF, NAV_QUERY, PAGE_CURSOR, PAGE_QUERY, Profile
from .strategies import ExtractResult, RawEntry

logger = logging.getLogger("OnionAccelerator.crawl.listing.navigate")

# Extensions that settle "file or directory" when nothing else does.
_HAS_EXTENSION = re.compile(r"\.[A-Za-z0-9]{1,8}$")


@dataclasses.dataclass(frozen=True)
class Location:
    """Where a page sits, in the terms its own addressing scheme uses.

    `path` is the logical directory the page lists -- the thing that goes in `?p=` or in
    the API body. For a path-addressed server it is unused; for everything else it is the
    only way to build a child's address, and it is carried on the request so a POST that
    contains no URL still knows where it was.
    """

    profile: Profile
    page_url: str
    request: PageRequest
    root: str
    path: str

    @staticmethod
    def of(profile: Profile, page_url: str, request: PageRequest) -> "Location":
        page_url = normalize_url(page_url)
        nav = profile.navigate
        parts = urlsplit(page_url)

        if nav.kind == NAV_QUERY:
            query = dict(parse_qsl(parts.query, keep_blank_values=True))
            path = request.path if request.path is not None else query.get(nav.param, "")
            root = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        elif nav.kind == NAV_ENCODED:
            prefix = nav.prefix.strip("/")
            segments = match_segments(page_url)
            keep = prefix.split("/") if prefix else []
            path = "/".join(segments[len(keep):]) if len(segments) > len(keep) else ""
            root = urlunsplit((parts.scheme, parts.netloc, "/" + "/".join(keep), "", ""))
        elif nav.kind == NAV_API:
            # The request is the only thing that knows where an API answer came from:
            # its URL is the endpoint, which is the same for every directory. A request
            # with no path at all is asking for the root -- deriving one from the
            # endpoint's own URL would make `/api/fs/list` a directory called "api".
            path = request.path or ""
            root = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
        else:
            path = ""
            root = dir_url(page_url)

        return Location(profile=profile, page_url=page_url, request=request,
                        root=root, path=path.strip("/"))

    @property
    def origin(self) -> str:
        parts = urlsplit(self.page_url)
        return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def resolve(location: Location, raw: RawEntry) -> Optional[Entry]:
    """One raw row as an addressable entry, or None if it addresses nothing new.

    Returns None for a row that resolves to the page itself or above it. On a
    path-addressed server the structural reader has already removed those; on every other
    kind this is the only place that can, because "above" is a statement about the
    scheme's own path, not about the URL.
    """
    nav = location.profile.navigate
    child_path = _child_path(location, raw)

    if nav.kind != NAV_HREF and child_path is not None:
        if _is_self_or_above(location.path, child_path):
            return None

    is_dir = raw.is_dir if raw.is_dir is not None else _infer_dir(raw)
    url = _entry_url(location, raw, child_path, is_dir)
    if not url:
        return None

    request = _entry_request(location, raw, child_path, url) if is_dir else None
    download = _download_url(location, raw, child_path, url) if not is_dir else None

    return Entry(
        url=url,
        name=raw.name,
        is_dir=is_dir,
        size_bytes=None if is_dir else raw.size_bytes,
        mtime_text=raw.mtime_text,
        request=request,
        download_url=download,
    )


def seed_request(profile: Profile, url: str) -> PageRequest:
    """The first request for a seed URL, in the profile's own scheme.

    Without this an API profile could never start: the crawler would GET the seed, get a
    JavaScript shell, and read nothing. With it, `--profile alist --url http://host/pub/`
    turns straight into the POST that lists `/pub/`.
    """
    if profile.navigate.kind != NAV_API:
        location = Location.of(profile, url, PageRequest.get(url))
        return PageRequest(url=normalize_url(url), path=location.path or None,
                           profile=profile.name)

    # The seed is the one API request whose path can only come from the URL the user
    # typed: `--url http://host/pub/` means list `pub`, and there is no earlier answer
    # to have carried that.
    path = _strip_prefix(urlsplit(normalize_url(url)).path, profile.navigate.prefix)
    seeded = PageRequest(url=normalize_url(url), path=path, profile=profile.name)
    location = Location.of(profile, url, seeded)
    return _api_request(location, _placeholders(location, path=path))


def next_page(location: Location, result: ExtractResult, page_index: int) -> Optional[PageRequest]:
    """The request for the rest of this same directory, if there is one.

    Queued at the page's own depth by the caller: another page of one directory is not a
    level down, and treating it as one would turn a manager with forty pages into a tree
    forty levels deep.
    """
    paginate = location.profile.paginate
    if not paginate.enabled or page_index >= paginate.max_pages:
        return None

    if paginate.kind == PAGE_QUERY:
        # Without an explicit "there is more" flag the only sound stop condition is an
        # empty page: ask for the next one only while the current one had rows.
        if not result.entries:
            return None
        return _requeried(location, {paginate.param: str(page_index + paginate.start + 1)},
                          path=location.path)

    if paginate.kind == PAGE_CURSOR:
        if not result.cursor:
            return None
        values = _placeholders(location, path=location.path, cursor=result.cursor)
        if location.profile.navigate.kind == NAV_API:
            return _api_request(location, values)
        return _requeried(location, {paginate.param: result.cursor}, path=location.path)

    return None


# ---------------------------------------------------------------- addressing


def _child_path(location: Location, raw: RawEntry) -> Optional[str]:
    """The child's path in the scheme's terms, or None when the link is the address."""
    nav = location.profile.navigate
    if raw.path is not None:
        return raw.path.strip("/")
    if nav.kind == NAV_HREF:
        # A JSON autoindex states names and no links at all; the name is then the path
        # relative to the page, which is exactly what a link would have been.
        return None if raw.href else raw.name
    if raw.href and nav.kind == NAV_API:
        # WebDAV's shape: the *request* is a PROPFIND, so navigation is an API call, but
        # each answer states a real href and that href is the path. Assembling one from
        # parent-plus-name instead would flatten `/files/archive/` to `archive` and ask
        # the server for a directory one level too high.
        return _strip_prefix(urlsplit(urljoin(location.page_url, raw.href)).path, nav.prefix)
    parent = location.path
    return f"{parent}{nav.join}{raw.name}".strip(nav.join) if parent else raw.name


def _entry_url(location: Location, raw: RawEntry, child_path: Optional[str],
               is_dir: bool) -> str:
    """The entry's own address: what gets recorded, and what a file is fetched from.

    A directory built from a path rather than a link gets its trailing slash back here.
    Nothing downstream can put it back, and without it every relative href on the page it
    serves resolves one level too high.
    """
    slash = "/" if is_dir else ""

    nav = location.profile.navigate
    if nav.kind == NAV_HREF:
        if raw.href:
            return normalize_url(raw.href)
        if child_path:
            return normalize_url(
                location.root.rstrip("/") + "/" + _quote_path(child_path) + slash)
        return ""

    if nav.kind == NAV_ENCODED:
        if not child_path:
            return ""
        return normalize_url(location.root.rstrip("/") + "/" + quote(child_path, safe=""))

    if nav.kind == NAV_QUERY:
        if child_path is None:
            return ""
        return _with_query(location.root, location.page_url, {nav.param: child_path})

    # api: the browse URL is not something a user can open, so the entry is addressed by
    # its download template where one exists and by a synthetic path otherwise -- it has
    # to be *some* stable URL, because that is the manifest's identity for the file.
    if child_path is None:
        return ""
    return normalize_url(location.origin + "/" + _quote_path(child_path) + slash)


def _entry_request(location: Location, raw: RawEntry,
                   child_path: Optional[str], url: str) -> Optional[PageRequest]:
    """How to list this child. None means "a plain GET of its own URL"."""
    nav = location.profile.navigate
    if nav.kind != NAV_API:
        if child_path is None:
            return PageRequest(url=url, profile=location.profile.name)
        return PageRequest(url=url, path=child_path, profile=location.profile.name)
    return _api_request(location, _placeholders(location, path=child_path or ""))


def _api_request(location: Location, values: dict[str, str]) -> PageRequest:
    nav = location.profile.navigate
    return PageRequest(
        url=normalize_url(_fill(nav.url, values, encode="url")),
        method=nav.method,
        headers=nav.headers,
        body=_fill(nav.body, values, encode="json") if nav.body else None,
        path=values.get("path_raw", ""),
        profile=location.profile.name,
    )


def _download_url(location: Location, raw: RawEntry,
                  child_path: Optional[str], url: str) -> Optional[str]:
    download = location.profile.download
    if not download.enabled:
        return None
    values = _placeholders(location, path=child_path or raw.name, href=raw.href or url,
                           name=raw.name)
    return normalize_url(_fill(download.url, values, encode="url"))


def _requeried(location: Location, extra: dict[str, str], *, path: str) -> PageRequest:
    """The page's own URL with some query parameters replaced."""
    nav = location.profile.navigate
    params = {nav.param: path} if nav.kind == NAV_QUERY else {}
    params.update(extra)
    return PageRequest(
        url=_with_query(location.root, location.page_url, params),
        path=path,
        profile=location.profile.name,
    )


def _with_query(root: str, page_url: str, params: dict[str, str]) -> str:
    """`root` with the page's other query parameters kept and `params` set on top.

    Keeping the others matters: a manager that needs `?token=...&p=dir` loses the session
    the moment a child URL is built from the parameter alone.
    """
    existing = dict(parse_qsl(urlsplit(page_url).query, keep_blank_values=True))
    existing.update(params)
    parts = urlsplit(root)
    return normalize_url(urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(existing), "")))


# ---------------------------------------------------------------- helpers


def _placeholders(location: Location, *, path: str = "", href: str = "",
                  name: str = "", cursor: str = "") -> dict[str, str]:
    return {
        "origin": location.origin,
        "root": location.root.rstrip("/"),
        "base": location.page_url,
        "path": path,
        "path_raw": path,
        "name": name,
        "href": href,
        "cursor": cursor,
    }


def _fill(template: Optional[str], values: dict[str, str], *, encode: str) -> str:
    """Substitute `{name}` placeholders, escaping for where the result is going.

    Not `str.format`: an API body is JSON, JSON is braces, and `str.format` would either
    raise on them or eat them. Only the names this module supplies are replaced; anything
    else in the template is left exactly as written.
    """
    if not template:
        return ""
    out = template
    for key, value in values.items():
        token = "{" + key + "}"
        if token not in out:
            continue
        if key == "path_raw" or encode == "raw":
            replacement = value
        elif encode == "json":
            replacement = json.dumps(value)[1:-1]
        elif key in ("origin", "root", "base", "href"):
            replacement = value
        else:
            replacement = _quote_path(value)
        out = out.replace(token, replacement)
    return out


def _strip_prefix(path: str, prefix: str) -> str:
    """A URL path with the target's route prefix removed, decoded, as a logical path."""
    segments = [s for s in path.split("/") if s]
    keep = [s for s in prefix.split("/") if s]
    if keep and segments[: len(keep)] == keep:
        segments = segments[len(keep):]
    return unquote("/".join(segments))


def _quote_path(path: str) -> str:
    """Percent-encode a path for a URL, keeping the separators as separators."""
    return quote(path, safe="/")


def _infer_dir(raw: RawEntry) -> bool:
    """Decide a kind the page did not state.

    Same rule the structural reader uses, and for the same reason: a trailing slash is
    authoritative, and past that "no extension and no size" is a directory far more often
    than it is a file.
    """
    if raw.href and urlsplit(raw.href).path.endswith("/"):
        return True
    if raw.path and raw.path.endswith("/"):
        return True
    if raw.size_bytes is not None:
        return False
    return not _HAS_EXTENSION.search(raw.name)


def _is_self_or_above(current: str, child: str) -> bool:
    """Is this row the directory we are already in, or one of its ancestors?

    The `..` row of a file manager, spelled as a path rather than as a link. On a
    path-addressed server the structural scope test catches these; nothing else does.
    """
    current_parts = [p for p in current.split("/") if p]
    child_parts = [p for p in child.split("/") if p]
    return len(child_parts) <= len(current_parts) and \
        current_parts[: len(child_parts)] == child_parts
