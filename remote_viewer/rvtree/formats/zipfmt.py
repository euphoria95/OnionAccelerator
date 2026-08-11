"""ZIP listing, delegated to stdlib ``zipfile``.

Nothing clever is needed: ``zipfile`` reads the End of Central Directory record and
then the Central Directory in one bulk read, so pointing it at a range-backed file
object lists a remote archive in three requests. It already understands ZIP64 and the
prepended-data (self-extracting) case, so we let it.

The one hard requirement is on the file object beneath it: ``_EndRecData`` seeks from
the end, and that seek must be resolved into an absolute range locally, because a
literal ``Range: bytes=-N`` is rejected outright by some CDN tiers.
"""

from __future__ import annotations

import logging
import zipfile
from typing import Iterator

from ..model import DIR, FILE, SYMLINK, Entry

log = logging.getLogger(__name__)

_S_IFLNK = 0xA000
_S_IFMT = 0xF000


def walk(fp) -> Iterator[Entry]:
    # Constructing the ZipFile is the whole cost: it reads the EOCD and then the central
    # directory in bulk, and the loop below touches nothing remote.
    z = zipfile.ZipFile(fp)
    log.info("zip central directory: %d entries", len(z.infolist()))
    for info in z.infolist():
        yield to_entry(info)


def to_entry(info: zipfile.ZipInfo) -> Entry:
    mode = None
    kind = FILE
    if info.create_system == 3:  # made on Unix: high 16 bits hold st_mode
        mode = info.external_attr >> 16
        if mode and (mode & _S_IFMT) == _S_IFLNK:
            kind = SYMLINK
    if info.is_dir():
        kind = DIR
    try:
        mtime = __import__("calendar").timegm(tuple(info.date_time) + (0, 0, -1))
    except (ValueError, TypeError):
        mtime = None
    return Entry(
        path=info.filename,
        size=info.file_size,
        csize=info.compress_size,
        mtime=float(mtime) if mtime is not None else None,
        mode=mode if mode else None,
        kind=kind,
        locator={"name": info.filename, "header_offset": info.header_offset},
    )


def open_member(fp, name: str):
    """Open one member for reading; ``zipfile`` fetches only that member's bytes."""
    z = zipfile.ZipFile(fp)
    return z.open(name)


def is_encrypted(fp) -> bool:
    z = zipfile.ZipFile(fp)
    return any(i.flag_bits & 0x1 for i in z.infolist())
