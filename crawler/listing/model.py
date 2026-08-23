"""What a listing is, independent of how it was obtained or read.

Three types, and the reason there are exactly three:

  * `PageRequest` -- a fetch, not a URL. Everything in the engine used to assume a
    directory was something you could GET; the file managers that serve a tree from a
    JSON API are not, and one extra field per request is the whole cost of supporting
    them. It is frozen and hashable because it rides on a frozen `Job`.
  * `Entry` -- one row of a listing. Its `request` is how to list it (directories only),
    which is what lets a template say "the child is a query parameter, not a path".
  * `Listing` -- the result of reading one page: the entries, whether this was a listing
    at all, and any further requests needed to finish *this* directory (pagination).

Nothing here knows about HTML, JSON, XML, or any web server. The strategies produce
these; the engine consumes them.
"""

from __future__ import annotations

import dataclasses
import hashlib
from typing import Any, Iterable, Iterator, Mapping, Optional

DEFAULT_METHOD = "GET"


@dataclasses.dataclass(frozen=True)
class PageRequest:
    """One HTTP request that is expected to answer with a listing.

    `headers` is a tuple of pairs rather than a dict so the whole object stays hashable:
    the frontier deduplicates on it, and a dict would make that impossible.
    """

    url: str
    method: str = DEFAULT_METHOD
    headers: tuple[tuple[str, str], ...] = ()
    body: Optional[str] = None
    profile: Optional[str] = None
    # The logical directory this request asks for, in the target's own terms. A POST to
    # one endpoint carries its path in the body, so the URL alone cannot say where the
    # answer came from -- and the next level down is built from exactly this.
    path: Optional[str] = None
    # True when `url` is the target's own listing endpoint rather than the directory's
    # address. Everything that rewrites a URL has to leave those alone: the frontier
    # queues a job under the directory it stands for, and pointing the request at that
    # would send the crawl to a URL the target does not serve.
    endpoint: bool = False

    @staticmethod
    def get(url: str, *, profile: Optional[str] = None) -> "PageRequest":
        """The overwhelmingly common case, spelled short."""
        return PageRequest(url=url, profile=profile)

    def with_url(self, url: str) -> "PageRequest":
        """The same request pointed somewhere else -- used when following a redirect."""
        return dataclasses.replace(self, url=url)

    def header_dict(self) -> dict[str, str]:
        return {name: value for name, value in self.headers}

    @property
    def identity(self) -> str:
        """What makes this request distinct from another, for deduplication.

        The URL half is deliberately *not* computed here: `urlnorm.dedup_key()` owns that,
        and it folds the encoded-separator spellings one file manager hands out for a
        single directory. This adds only what a URL cannot express -- the method, the body,
        and the directory an endpoint request asks for -- so that two calls to one endpoint
        for different directories are two jobs, and two spellings of one GET remain one.

        An endpoint request contributes an identity even when it is a plain GET, because
        that is exactly the case where the URL is shared across directories and the path
        is the only thing telling them apart.
        """
        if self.method == DEFAULT_METHOD and not self.body and not self.endpoint:
            return ""
        material = f"{self.path or ''}\x00{self.body or ''}"
        digest = hashlib.sha1(material.encode("utf-8")).hexdigest()[:16]
        return f"{self.method}:{digest}"

    def __str__(self) -> str:                       # pragma: no cover - logging aid
        return self.url if self.method == DEFAULT_METHOD else f"{self.method} {self.url}"


@dataclasses.dataclass(frozen=True)
class Entry:
    """One row of a directory listing.

    `url` is the entry's own address: for a file, what gets downloaded; for a directory,
    where it lives. `request` is how to *list* a directory, which on a path-addressed
    server is just a GET of `url` and on a file manager is something else entirely.
    `download_url` is set only when a target serves a file's bytes from a different URL
    than the one its listing links (a `?dl=` route, an `/api/raw/` endpoint).
    """

    url: str
    name: str
    is_dir: bool
    size_bytes: Optional[int] = None
    mtime_text: Optional[str] = None
    request: Optional[PageRequest] = None
    download_url: Optional[str] = None

    @property
    def fetch_url(self) -> str:
        """The URL to actually pull the bytes from. Differs only where a target says so."""
        return self.download_url or self.url

    def listing_request(self) -> PageRequest:
        """How to fetch this entry's own listing. Directories only."""
        return self.request or PageRequest.get(self.url)

    def as_record(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "url": self.url,
            "name": self.name,
            "is_dir": self.is_dir,
            "size_bytes": self.size_bytes,
            "mtime_text": self.mtime_text,
        }
        if self.download_url:
            record["download_url"] = self.download_url
        return record


@dataclasses.dataclass(frozen=True)
class Listing:
    """The result of reading one page.

    `is_index` is the question the crawler actually asks -- may I expand this? For the
    generic structural reading it is a score against a threshold; for a page a named
    profile matched it is simply true, because the match *is* the evidence.

    `more` carries requests that belong to this same directory: the next page of a
    paginated manager, the next continuation token of a bucket listing. They are queued at
    the same depth, not one below, or the tree would grow a level per page.
    """

    base_url: str
    directories: tuple[Entry, ...]
    files: tuple[Entry, ...]
    is_index: bool
    confidence: float
    title: Optional[str] = None
    generator: Optional[str] = None
    profile: Optional[str] = None
    more: tuple[PageRequest, ...] = ()
    # False when the entries already *are* the whole subtree, as a published manifest's
    # are. Its directories still belong in the report; fetching them would spend one
    # request each to re-learn what one request already said.
    expandable: bool = True

    @property
    def total(self) -> int:
        return len(self.directories) + len(self.files)

    @property
    def entries(self) -> tuple[Entry, ...]:
        return self.directories + self.files


def split_entries(entries: Iterable[Entry]) -> tuple[tuple[Entry, ...], tuple[Entry, ...]]:
    """Directories and files, in one pass, preserving order within each."""
    dirs, files = [], []
    for entry in entries:
        (dirs if entry.is_dir else files).append(entry)
    return tuple(dirs), tuple(files)


def dedupe(entries: Iterable[Entry]) -> Iterator[Entry]:
    """Collapse entries that address the same thing.

    Listings routinely link one target twice -- once from its icon, once from its name --
    and the second link carries the better text, so the last one wins.
    """
    seen: dict[str, Entry] = {}
    for entry in entries:
        seen[entry.url] = entry
    return iter(seen.values())


def headers_tuple(headers: Optional[Mapping[str, str]]) -> tuple[tuple[str, str], ...]:
    """A header mapping as the sorted pair-tuple `PageRequest` stores.

    Sorted so that two requests differing only in the order a template happened to write
    its headers in are one request, not two.
    """
    if not headers:
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in headers.items()))
