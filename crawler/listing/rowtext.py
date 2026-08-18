"""Reading a listing row's *text*: the size, the timestamp, the junk.

Shared by every strategy that works from rendered rows -- the structural anchor reader
and the CSS-selector one both meet `4.1K`, `512M` and `02-Nov-2023 14:29` in the wild,
and they have to agree on what those mean or one target's sizes would depend on which
strategy happened to read it.

Machine-readable listings (JSON, XML) never come through here: they carry integers.
"""

from __future__ import annotations

import re
from typing import Optional

# 4.1K / 12M / 1.2 GiB / 4096 / 4096 bytes. Anchored on a word boundary so it doesn't
# pick digits out of a filename.
SIZE_RE = re.compile(
    r"(?<![\w.])(\d+(?:[.,]\d+)?)\s*(K|M|G|T|P|KB|MB|GB|TB|PB|KIB|MIB|GIB|TIB|PIB|B|BYTES)?(?![\w.])",
    re.IGNORECASE,
)
SIZE_UNITS = {
    None: 1, "B": 1, "BYTES": 1,
    "K": 1024, "KB": 1024, "KIB": 1024,
    "M": 1024 ** 2, "MB": 1024 ** 2, "MIB": 1024 ** 2,
    "G": 1024 ** 3, "GB": 1024 ** 3, "GIB": 1024 ** 3,
    "T": 1024 ** 4, "TB": 1024 ** 4, "TIB": 1024 ** 4,
    "P": 1024 ** 5, "PB": 1024 ** 5, "PIB": 1024 ** 5,
}

# The date shapes the common autoindex implementations emit.
DATE_RES = (
    re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?"),           # nginx, Caddy
    re.compile(r"\d{2}-[A-Za-z]{3}-\d{4}\s+\d{2}:\d{2}(?::\d{2})?"),      # Apache
    re.compile(r"\d{4}-[A-Za-z]{3}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?"),      # lighttpd
    re.compile(r"[A-Za-z]{3}\s+\d{1,2}\s+(?:\d{4}|\d{2}:\d{2})"),         # ls -l style
)


def parse_size(row: str, *, exclude: str = "") -> Optional[int]:
    """Best-effort byte count from a listing row.

    Two things are stripped before anything is matched, and both are load-bearing:

      * `exclude`, the entry's own name, so `leak-2024.tar.gz` doesn't donate its year;
      * the timestamp, because `2023-11-02 14:29` is nothing but digits and a naive
        scan reads the year as the file size -- which then makes every directory look
        like a file, since "has a size" is what distinguishes the two.

    Directories are conventionally `-`, which matches nothing and correctly yields None.
    """
    if not row:
        return None
    haystack = row.replace(exclude, " ", 1) if exclude else row
    for pattern in DATE_RES:
        haystack = pattern.sub(" ", haystack)
    best: Optional[int] = None
    for match in SIZE_RE.finditer(haystack):
        number, unit = match.group(1), match.group(2)
        key = unit.upper() if unit else None
        if key not in SIZE_UNITS:
            continue
        if key is None and "." in number:
            continue                      # a bare decimal is a version, not a size
        try:
            value = float(number.replace(",", "."))
        except ValueError:
            continue
        candidate = int(value * SIZE_UNITS[key])
        # A unitless number could be anything on the row (a date part, a permission
        # mask). One carrying a unit is unambiguous, so it wins outright.
        if key not in (None, "B", "BYTES"):
            return candidate
        if best is None:
            best = candidate
    return best


def parse_date(row: str) -> Optional[str]:
    """The modification timestamp as it appears in the row, verbatim.

    Kept as text on purpose: the listing carries no timezone, so parsing it into a
    datetime would invent precision that isn't there. For CTI, what the server said is
    the evidence.
    """
    for pattern in DATE_RES:
        match = pattern.search(row)
        if match:
            return match.group(0)
    return None
