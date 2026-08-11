"""Up-front capability probe for a remote URL."""

from __future__ import annotations

import dataclasses
from typing import Optional

from .tor import Transport


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
    )
    if check_multirange and accepts:
        caps.multirange = transport.supports_multirange(url, size)
    return caps
