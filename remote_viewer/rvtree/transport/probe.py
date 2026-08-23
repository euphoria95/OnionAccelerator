"""Up-front capability probe for a remote URL."""

from __future__ import annotations

import dataclasses
from typing import Optional

from .tor import GATE_HEADER, Transport


@dataclasses.dataclass
class Capabilities:
    url: str
    size: int
    accepts_ranges: bool
    multirange: bool
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    server: Optional[str] = None
    content_type: Optional[str] = None
    # Every header the entity came with, so a caller that already paid for this probe
    # does not pay again to identify the format from Content-Disposition.
    headers: dict = dataclasses.field(default_factory=dict)
    # True when the URL only gave up its bytes after a proof-of-work challenge. Worth
    # keeping apart from the rest: it explains both the extra round trips and, usually,
    # why ``accepts_ranges`` below is False.
    gated: bool = False

    def validator(self) -> tuple[Optional[str], Optional[str], int]:
        """Identity of the entity we listed, so a later run can detect rotation."""
        return (self.etag, self.last_modified, self.size)


def probe(transport: Transport, url: str, check_multirange: bool = False) -> Capabilities:
    size, headers = transport.head_like(url)
    accepts = headers.get("accept-ranges", "").lower() == "bytes" or "content-range" in headers
    caps = Capabilities(
        url=url,
        size=size,
        accepts_ranges=accepts,
        multirange=False,
        etag=headers.get("etag"),
        last_modified=headers.get("last-modified"),
        server=headers.get("server"),
        content_type=headers.get("content-type"),
        headers=headers,
        gated=GATE_HEADER in headers,
    )
    if check_multirange and accepts:
        caps.multirange = transport.supports_multirange(url, size)
    return caps
