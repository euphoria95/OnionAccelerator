"""Archive entry model shared by every format parser."""

from __future__ import annotations

import dataclasses
import stat as statmod
from typing import Any, Optional

FILE = "file"
DIR = "dir"
SYMLINK = "symlink"
HARDLINK = "hardlink"
OTHER = "other"


@dataclasses.dataclass
class Entry:
    """One member of a remote archive.

    ``size`` is always the uncompressed size. ``csize`` is only set by formats that
    track a per-member compressed size (zip does, 7z and tar do not).
    """

    path: str
    size: int = 0
    csize: Optional[int] = None
    mtime: Optional[float] = None
    mode: Optional[int] = None
    kind: str = FILE
    linkname: str = ""
    # Format-specific locator used by the extractors, e.g. the byte offset of the
    # member's data inside the decoded tar stream, or its 7z folder index.
    locator: Optional[Any] = None

    @property
    def is_dir(self) -> bool:
        return self.kind == DIR

    def mode_string(self) -> str:
        if self.mode is None:
            return "?" * 10
        prefix = {DIR: statmod.S_IFDIR, SYMLINK: statmod.S_IFLNK}.get(self.kind, statmod.S_IFREG)
        return statmod.filemode(prefix | statmod.S_IMODE(self.mode))


@dataclasses.dataclass
class Listing:
    """Result of listing an archive, plus what it cost us to find out."""

    entries: list[Entry]
    format: str
    total_size: int = 0
    bytes_fetched: int = 0
    requests: int = 0
    notes: list[str] = dataclasses.field(default_factory=list)
