"""The template: what a target looks like, how to read it, and how to walk it.

A profile is data, not code. It is deliberately small enough to write from one look at a
target's HTML -- four sections, none of them mandatory except the strategy -- because the
point of the redesign is that adding support for a new file manager is a file you drop in
a directory, not a patch to the parser.

The four questions a profile answers, in the order they get asked:

  [match]     is this that target?          -- every rule present must hold
  [extract]   where are the rows?           -- which strategy reads them, and with what
  [navigate]  how is a child addressed?     -- a path, a query parameter, an API call
  [download]  where do the bytes live?      -- when that is not the listing's own URL

Validation is strict and loud: an unknown key is an error naming the file, because a
silently-ignored typo in a template is a rule that quietly stopped applying, and the
failure that produces is a crawl that finds nothing and says nothing.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Any, Mapping, Optional, Pattern

# Every regex in [match] is compiled case-insensitively and multi-line. Titles, server
# banners and generator strings vary in case between releases of the same daemon, and a
# template author who has to remember `(?i)` will forget -- Python also rejects an inline
# flag that does not lead the pattern, so this is the spelling that cannot go wrong.
# Multi-line because a body regex is looking for one line in a document: `^-rw-r--r--`
# means "a line like this", and without the flag it would mean "the file starts with it".
_MATCH_FLAGS = re.IGNORECASE | re.MULTILINE

STATUS_VERIFIED = "verified"
STATUS_UNVERIFIED = "unverified"
_STATUSES = (STATUS_VERIFIED, STATUS_UNVERIFIED)

NAV_HREF = "href"
NAV_QUERY = "query"
NAV_ENCODED = "encoded"
NAV_API = "api"
_NAV_KINDS = (NAV_HREF, NAV_QUERY, NAV_ENCODED, NAV_API)

PAGE_NONE = "none"
PAGE_QUERY = "query"
PAGE_CURSOR = "cursor"
_PAGE_KINDS = (PAGE_NONE, PAGE_QUERY, PAGE_CURSOR)

_TOP_KEYS = frozenset({
    "name", "title", "priority", "status", "notes",
    "match", "probe", "extract", "navigate", "paginate", "download",
})


class TemplateError(ValueError):
    """A template that cannot be loaded. Always names the file it came from."""


def _check_keys(section: str, data: Mapping[str, Any], allowed: frozenset[str],
                source: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise TemplateError(
            f"{source}: unknown key(s) in [{section}]: {', '.join(unknown)} "
            f"(allowed: {', '.join(sorted(allowed))})"
        )


def _regex(value: Any, field: str, source: str) -> Optional[Pattern[str]]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TemplateError(f"{source}: {field} must be a string, got {type(value).__name__}")
    try:
        return re.compile(value, _MATCH_FLAGS)
    except re.error as exc:
        raise TemplateError(f"{source}: {field} is not a valid regex: {exc}") from exc


def _str_tuple(value: Any, field: str, source: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        return tuple(value)
    raise TemplateError(f"{source}: {field} must be a string or a list of strings")


def _int(value: Any, field: str, source: str, default: int) -> int:
    """A whole number, or a TemplateError naming the file.

    Bare `int()` raises a ValueError that says only what it could not convert, which for a
    file dropped in a --templates directory mid-engagement is the one thing already known
    and the file name is the thing wanted.
    """
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise TemplateError(f"{source}: {field} must be a whole number, got {value!r}") from None


# ---------------------------------------------------------------- [match]


@dataclasses.dataclass(frozen=True)
class Match:
    """When a page is this target.

    Rules are ANDed: every one that is present must hold. The score is a count of how
    much evidence there was, which is what separates "matched because it is an Apache
    autoindex" from "matched because it is HTML".
    """

    content_type: tuple[str, ...] = ()
    status: tuple[int, ...] = ()
    title_regex: Optional[Pattern[str]] = None
    generator_regex: Optional[Pattern[str]] = None
    body_regex: Optional[Pattern[str]] = None
    url_regex: Optional[Pattern[str]] = None
    headers: tuple[tuple[str, Pattern[str]], ...] = ()

    _KEYS = frozenset({
        "content_type", "status", "title_regex", "generator_regex",
        "body_regex", "url_regex", "header",
    })

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], source: str) -> "Match":
        _check_keys("match", data, cls._KEYS, source)
        status = data.get("status") or ()
        if isinstance(status, int):
            status = (status,)
        headers = data.get("header") or {}
        if not isinstance(headers, Mapping):
            raise TemplateError(f"{source}: [match].header must be a table of name = regex")
        return cls(
            content_type=tuple(
                v.lower() for v in _str_tuple(data.get("content_type"), "content_type", source)
            ),
            status=tuple(_int(s, "[match].status", source, 0) for s in status),
            title_regex=_regex(data.get("title_regex"), "title_regex", source),
            generator_regex=_regex(data.get("generator_regex"), "generator_regex", source),
            body_regex=_regex(data.get("body_regex"), "body_regex", source),
            url_regex=_regex(data.get("url_regex"), "url_regex", source),
            headers=tuple(
                (str(name), _regex(pattern, f"header.{name}", source))  # type: ignore[misc]
                for name, pattern in sorted(headers.items())
            ),
        )

    @property
    def rule_count(self) -> int:
        return (
            bool(self.content_type) + bool(self.status) + bool(self.title_regex)
            + bool(self.generator_regex) + bool(self.body_regex) + bool(self.url_regex)
            + len(self.headers)
        )


# ---------------------------------------------------------------- [probe]


@dataclasses.dataclass(frozen=True)
class Probe:
    """An active request `--detect` may try, for targets that do not advertise.

    A SPA serves the same JavaScript shell whatever directory you ask for; the only way
    to tell one from another is to call the API it would call. That is a request the user
    has to opt into, which is why it lives here and not in [match]: passive matching runs
    on every crawl, probes run only under --detect.
    """

    method: str = "GET"
    path: str = ""
    headers: tuple[tuple[str, str], ...] = ()
    body: Optional[str] = None
    expect_regex: Optional[Pattern[str]] = None

    _KEYS = frozenset({"method", "path", "headers", "body", "expect_regex"})

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], source: str) -> "Probe":
        _check_keys("probe", data, cls._KEYS, source)
        headers = data.get("headers") or {}
        if not isinstance(headers, Mapping):
            raise TemplateError(f"{source}: [probe].headers must be a table")
        return cls(
            method=str(data.get("method", "GET")).upper(),
            path=str(data.get("path", "")),
            headers=tuple(sorted((str(k), str(v)) for k, v in headers.items())),
            body=data.get("body"),
            expect_regex=_regex(data.get("expect_regex"), "expect_regex", source),
        )


# ---------------------------------------------------------------- [extract]


@dataclasses.dataclass(frozen=True)
class Extract:
    """Which strategy reads the rows, and the options it takes.

    The options are validated against the strategy's own declared set rather than against
    a list kept here, so adding a strategy does not mean editing this file.
    """

    strategy: str
    options: Mapping[str, Any] = dataclasses.field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], source: str) -> "Extract":
        strategy = data.get("strategy")
        if not strategy:
            raise TemplateError(f"{source}: [extract].strategy is required")
        options = {k: v for k, v in data.items() if k != "strategy"}
        return cls(strategy=str(strategy), options=options)


# ---------------------------------------------------------------- [navigate]


@dataclasses.dataclass(frozen=True)
class Navigate:
    """How a listed child becomes the next request.

    This is the section the whole redesign exists for. A path-addressed autoindex is
    `href` and needs nothing else; a file manager that keeps the path in `?p=` is `query`;
    one that folds it into a single percent-encoded segment is `encoded`; one that answers
    a JSON API is `api`, and only then does the crawler need to build a request that is
    not a GET of a link it found.
    """

    kind: str = NAV_HREF
    param: str = "path"
    join: str = "/"
    prefix: str = ""
    method: str = "GET"
    url: str = ""
    body: Optional[str] = None
    headers: tuple[tuple[str, str], ...] = ()

    _KEYS = frozenset({"kind", "param", "join", "prefix", "method", "url", "body", "headers"})

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], source: str) -> "Navigate":
        _check_keys("navigate", data, cls._KEYS, source)
        kind = str(data.get("kind", NAV_HREF))
        if kind not in _NAV_KINDS:
            raise TemplateError(
                f"{source}: [navigate].kind must be one of {', '.join(_NAV_KINDS)}, got {kind!r}")
        headers = data.get("headers") or {}
        if not isinstance(headers, Mapping):
            raise TemplateError(f"{source}: [navigate].headers must be a table")
        if kind == NAV_API and not data.get("url"):
            raise TemplateError(f"{source}: [navigate].url is required when kind = \"api\"")
        return cls(
            kind=kind,
            param=str(data.get("param", "path")),
            join=str(data.get("join", "/")),
            prefix=str(data.get("prefix", "")),
            method=str(data.get("method", "GET")).upper(),
            url=str(data.get("url", "")),
            body=data.get("body"),
            headers=tuple(sorted((str(k), str(v)) for k, v in headers.items())),
        )


# ---------------------------------------------------------------- [paginate]


@dataclasses.dataclass(frozen=True)
class Paginate:
    """How to ask for the rest of *this* directory.

    Distinct from navigation on purpose: another page of the same directory is queued at
    the same depth, because it is not a level down. Getting that wrong turns a manager
    with 40 pages of one directory into a tree 40 levels deep.
    """

    kind: str = PAGE_NONE
    param: str = "page"
    start: int = 1
    max_pages: int = 500
    cursor_field: str = ""
    more_field: str = ""

    _KEYS = frozenset({"kind", "param", "start", "max_pages", "cursor_field", "more_field"})

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], source: str) -> "Paginate":
        _check_keys("paginate", data, cls._KEYS, source)
        kind = str(data.get("kind", PAGE_NONE))
        if kind not in _PAGE_KINDS:
            raise TemplateError(
                f"{source}: [paginate].kind must be one of {', '.join(_PAGE_KINDS)}, got {kind!r}")
        # No cursor_field requirement: the cursor is whatever the strategy read out of
        # the answer, and it is [extract] that knows where in the answer that was.
        return cls(
            kind=kind,
            param=str(data.get("param", "page")),
            start=_int(data.get("start"), "[paginate].start", source, 1),
            max_pages=_int(data.get("max_pages"), "[paginate].max_pages", source, 500),
            cursor_field=str(data.get("cursor_field", "")),
            more_field=str(data.get("more_field", "")),
        )

    @property
    def enabled(self) -> bool:
        return self.kind != PAGE_NONE


# ---------------------------------------------------------------- [download]


@dataclasses.dataclass(frozen=True)
class Download:
    """Where a file's bytes are, when that is not where its listing links.

    `url` is a template over `{origin}`, `{root}`, `{base}`, `{path}`, `{parent}`,
    `{name}` and `{href}`. `{path}` is the file's own path and `{parent}` the directory it
    was listed in, which is the distinction a manager that browses at `?p=dir` and serves
    at `?p=dir&dl=file` turns on. An autoindex needs none of it and leaves the section out.
    """

    url: str = ""

    _KEYS = frozenset({"url"})

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], source: str) -> "Download":
        _check_keys("download", data, cls._KEYS, source)
        return cls(url=str(data.get("url", "")))

    @property
    def enabled(self) -> bool:
        return bool(self.url)


# ---------------------------------------------------------------- the profile


@dataclasses.dataclass(frozen=True)
class Profile:
    """One template, loaded and validated."""

    name: str
    extract: Extract
    title: str = ""
    priority: int = 50
    status: str = STATUS_UNVERIFIED
    notes: str = ""
    match: Match = dataclasses.field(default_factory=Match)
    probe: Optional[Probe] = None
    navigate: Navigate = dataclasses.field(default_factory=Navigate)
    paginate: Paginate = dataclasses.field(default_factory=Paginate)
    download: Download = dataclasses.field(default_factory=Download)
    source: str = "<builtin>"

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], source: str) -> "Profile":
        _check_keys("", data, _TOP_KEYS, source)
        name = data.get("name")
        if not name:
            raise TemplateError(f"{source}: 'name' is required")
        extract = data.get("extract")
        if not isinstance(extract, Mapping):
            raise TemplateError(f"{source}: an [extract] section is required")
        status = str(data.get("status", STATUS_UNVERIFIED))
        if status not in _STATUSES:
            raise TemplateError(
                f"{source}: status must be one of {', '.join(_STATUSES)}, got {status!r}")

        for section in ("match", "probe", "navigate", "paginate", "download"):
            value = data.get(section)
            if value is not None and not isinstance(value, Mapping):
                raise TemplateError(f"{source}: [{section}] must be a table")

        probe = data.get("probe")
        return cls(
            name=str(name),
            title=str(data.get("title", "")),
            priority=_int(data.get("priority"), "priority", source, 50),
            status=status,
            notes=str(data.get("notes", "")),
            match=Match.from_dict(data.get("match") or {}, source),
            probe=Probe.from_dict(probe, source) if probe else None,
            extract=Extract.from_dict(extract, source),
            navigate=Navigate.from_dict(data.get("navigate") or {}, source),
            paginate=Paginate.from_dict(data.get("paginate") or {}, source),
            download=Download.from_dict(data.get("download") or {}, source),
            source=source,
        )

    @property
    def verified(self) -> bool:
        return self.status == STATUS_VERIFIED
