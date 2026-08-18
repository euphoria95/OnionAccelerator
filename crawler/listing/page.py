"""One fetched page, in the form the strategies want to read it.

The engine hands this across the seam. It exists so a strategy never touches aiohttp and
never re-parses: the soup and the JSON are built at most once each, on first use, and a
profile that matches on the body regex does not pay for a parse it never needed.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import warnings
from typing import Any, Mapping, Optional

from bs4 import BeautifulSoup

from ..urlnorm import host_of
from .model import PageRequest

try:  # pragma: no cover - bs4 moved this class between releases
    from bs4 import XMLParsedAsHTMLWarning
except ImportError:  # pragma: no cover
    class XMLParsedAsHTMLWarning(UserWarning):  # type: ignore[no-redef]
        """Stand-in for older bs4 releases that don't define it."""

logger = logging.getLogger("OnionAccelerator.crawl.listing")

# Distinguishes "not parsed yet" from "parsed, and it was not JSON" -- None is a valid
# JSON document, so it cannot do that job.
_UNPARSED = object()

# Which parser BeautifulSoup gets. lxml is both faster and far more forgiving of the
# unclosed tags that hand-rolled autoindex templates are full of; html.parser is the
# fallback so the package still imports on a machine without lxml.
try:  # pragma: no cover - trivial import guard
    import lxml  # noqa: F401
    BS_PARSER = "lxml"
    XML_PARSER = "lxml-xml"
except ImportError:  # pragma: no cover
    BS_PARSER = "html.parser"
    XML_PARSER = "html.parser"


@dataclasses.dataclass
class Page:
    """A fetched body plus everything a profile may want to match against."""

    url: str
    body: str
    status: int = 200
    content_type: str = ""
    headers: Mapping[str, str] = dataclasses.field(default_factory=dict)
    request: Optional[PageRequest] = None

    _soup: Optional[BeautifulSoup] = dataclasses.field(default=None, repr=False, init=False)
    _xml: Optional[BeautifulSoup] = dataclasses.field(default=None, repr=False, init=False)
    _json: Any = dataclasses.field(default=_UNPARSED, repr=False, init=False)

    @property
    def host(self) -> str:
        return host_of(self.url)

    @property
    def soup(self) -> BeautifulSoup:
        """The body parsed as HTML, whatever it claims to be.

        lighttpd and a few custom templates serve XHTML with an XML declaration, which
        makes bs4 warn that an XML document is being parsed as HTML. It is the right call
        here -- an autoindex page is HTML in practice, and the strict XML parser would
        reject the unclosed tags hand-rolled templates are full of -- so the warning is
        suppressed at exactly this call rather than globally.
        """
        if self._soup is None:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
                self._soup = BeautifulSoup(self.body, BS_PARSER)
        return self._soup

    @property
    def xml(self) -> BeautifulSoup:
        """The body parsed as XML -- for WebDAV's PROPFIND and S3's bucket listings.

        A separate parse from `soup`: the HTML parser lowercases tag names and mangles
        namespaces, which is exactly what `D:multistatus` and `ListBucketResult` are made
        of.
        """
        if self._xml is None:
            self._xml = BeautifulSoup(self.body, XML_PARSER)
        return self._xml

    @property
    def json(self) -> Any:
        """The body parsed as JSON, or None if it is not JSON. Parsed at most once."""
        if self._json is _UNPARSED:
            try:
                self._json = json.loads(self.body)
            except (ValueError, TypeError):
                self._json = None
        return self._json

    def header(self, name: str) -> str:
        """One response header, case-insensitively, or ''."""
        lowered = name.lower()
        for key, value in self.headers.items():
            if key.lower() == lowered:
                return str(value)
        return ""
