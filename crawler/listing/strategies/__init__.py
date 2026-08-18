"""The strategies: five ways of finding the rows in a page, and nothing else.

A strategy answers one question -- *where are the entries* -- and answers it in the
target's own terms: a link, a name, a relative path. It does not decide how a child is
addressed, does not build requests, and does not know what a crawl is. That separation is
what lets one strategy serve a dozen targets: `rows` reads any table, and the template
says whether the child lives in the href, in `?p=`, or in a POST body.

Registration is by name so a template can say `strategy = "json"` and so
`--list-profiles` can report which strategies exist without importing the engine.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Callable, Mapping, Optional

from ..page import Page


@dataclasses.dataclass
class RawEntry:
    """One row, in whatever terms the page expressed it.

    A strategy fills in what the page actually said. `href` is set when the page links
    the entry (every HTML listing does); `path` is set when the page states a full path
    from the service root (JSON APIs and manifests do). `is_dir=None` means the page did
    not say and the reader could not tell -- navigation decides, from the trailing slash
    or the absence of a size.
    """

    name: str
    is_dir: Optional[bool] = None
    href: Optional[str] = None
    path: Optional[str] = None
    size_bytes: Optional[int] = None
    mtime_text: Optional[str] = None
    extra: Mapping[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class ExtractResult:
    """Everything one strategy learned from one page.

    `is_index` and `confidence` only vary for the structural reader, which has to guess;
    a strategy that found the rows a template pointed it at knows the answer.

    `expandable=False` says the entries are already the whole subtree -- a manifest
    reports thousands of directories that must be recorded but must never be fetched,
    which is the difference between one request and seven thousand.
    """

    entries: list[RawEntry] = dataclasses.field(default_factory=list)
    is_index: bool = True
    confidence: float = 1.0
    title: Optional[str] = None
    generator: Optional[str] = None
    base_url: Optional[str] = None
    cursor: Optional[str] = None
    has_more: bool = False
    expandable: bool = True


@dataclasses.dataclass
class ExtractContext:
    """What a strategy needs from the run that is not on the page.

    `address_kind` is the profile's `[navigate].kind`, and a strategy is allowed to know
    it for exactly one reason: whether "this link points below the page" is a question
    that means anything. On a path-addressed server it is the filter that removes every
    breadcrumb; on a manager that keeps the path in `?p=` every row would fail it.
    """

    allow_offsite: bool = False
    address_kind: str = "href"


@dataclasses.dataclass(frozen=True)
class Strategy:
    """A named reader plus the option keys its templates may set."""

    name: str
    options: frozenset[str]
    read: Callable[[Page, Mapping[str, Any], ExtractContext], ExtractResult]
    summary: str = ""


_REGISTRY: dict[str, Strategy] = {}


def register(strategy: Strategy) -> Strategy:
    _REGISTRY[strategy.name] = strategy
    return strategy


def get(name: str) -> Optional[Strategy]:
    _load()
    return _REGISTRY.get(name)


def names() -> tuple[str, ...]:
    _load()
    return tuple(sorted(_REGISTRY))


def all_strategies() -> tuple[Strategy, ...]:
    _load()
    return tuple(_REGISTRY[name] for name in sorted(_REGISTRY))


_loaded = False


def _load() -> None:
    """Import the strategy modules once, on first use.

    Late rather than at package import so that `crawler.listing.model` and the profile
    schema stay importable on a machine with no BeautifulSoup -- `--list-profiles` and
    template validation are useful there, and a crawl is not possible there anyway.
    """
    global _loaded
    if _loaded:
        return
    _loaded = True
    from . import anchors, jsonrows, manifest, rows, xmlrows  # noqa: F401
