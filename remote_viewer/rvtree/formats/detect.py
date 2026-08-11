"""Working out what a remote URL is actually pointing at.

Magic bytes come first and always win: a URL may end in ``.bin``, be content-negotiated,
or lie outright, and guessing wrong here means fetching the wrong part of a 100 GB file.

Names are consulted only when the bytes fail to resolve — which is the common shape of a
download endpoint like ``/dl?id=42``, where there is no extension to read and the server
puts the real filename in ``Content-Disposition`` instead. The order is therefore magic
bytes, then the ``Content-Disposition`` filename, then ``Content-Type``, then the URL
path, with an explicit override ahead of all of them.
"""

from __future__ import annotations

import dataclasses
import logging
import posixpath
from email.message import Message
from typing import Mapping, Optional
from urllib.parse import unquote, urlsplit

from ..util import human_bytes

log = logging.getLogger(__name__)

ZIP = "zip"
SEVENZIP = "7z"
XZ = "xz"
TAR = "tar"
TAR_XZ = "tar.xz"
RAR = "rar"
UNKNOWN = "unknown"

# Recognised but not supported, kept so the error message can be specific.
GZIP = "gzip"
BZIP2 = "bzip2"
ZSTD = "zstd"
TAR_GZ = "tar.gz"
TAR_BZ2 = "tar.bz2"
TAR_ZST = "tar.zst"

SUPPORTED = (ZIP, SEVENZIP, TAR, XZ, TAR_XZ, RAR)

# The compressors with no block index. Nothing in them can be reached without
# decompressing everything before it, which is the whole reason they are excluded.
UNINDEXED = {
    GZIP: "gzip",
    BZIP2: "bzip2",
    ZSTD: "zstandard",
    TAR_GZ: "gzip",
    TAR_BZ2: "bzip2",
    TAR_ZST: "zstandard",
}

# A tar inside a compressor, keyed by the compressor's own format name.
_TAR_INSIDE = {GZIP: TAR_GZ, BZIP2: TAR_BZ2, ZSTD: TAR_ZST}

_HEAD = 264  # far enough to reach the tar ustar magic at offset 257

EOCD_SIG = b"PK\x05\x06"

# Longest first: ``.tar.gz`` must be matched before ``.gz``.
_BY_SUFFIX = (
    (".tar.xz", TAR_XZ),
    (".tar.gz", TAR_GZ),
    (".tar.bz2", TAR_BZ2),
    (".tar.zst", TAR_ZST),
    (".txz", TAR_XZ),
    (".tgz", TAR_GZ),
    (".tbz2", TAR_BZ2),
    (".tbz", TAR_BZ2),
    (".tzst", TAR_ZST),
    (".tar", TAR),
    (".zip", ZIP),
    (".zipx", ZIP),
    (".jar", ZIP),
    (".7z", SEVENZIP),
    (".rar", RAR),
    (".cbr", RAR),
    (".r00", RAR),
    (".xz", XZ),
    (".gz", GZIP),
    (".bz2", BZIP2),
    (".zst", ZSTD),
)

_BY_MIME = {
    "application/zip": ZIP,
    "application/x-zip-compressed": ZIP,
    "application/java-archive": ZIP,
    "application/x-7z-compressed": SEVENZIP,
    "application/x-rar-compressed": RAR,
    "application/vnd.rar": RAR,
    "application/x-rar": RAR,
    "application/x-tar": TAR,
    "application/tar": TAR,
    "application/x-xz": XZ,
    "application/xz": XZ,
    "application/gzip": GZIP,
    "application/x-gzip": GZIP,
    "application/x-bzip2": BZIP2,
    "application/x-bzip": BZIP2,
    "application/zstd": ZSTD,
    "application/x-zstd": ZSTD,
}

# Says nothing at all, and servers hand it out for everything.
_USELESS_MIME = {"application/octet-stream", "binary/octet-stream", "application/binary"}


@dataclasses.dataclass
class Detection:
    """What the format is, and what convinced us of it."""

    fmt: str
    via: str
    filename: str = ""
    head: bytes = b""
    content_type: str = ""

    def __str__(self) -> str:
        return f"{self.fmt} ({describe(self.fmt)}) via {self.via}"


# ------------------------------------------------------------------- magic bytes


def detect(fp) -> str:
    """Identify by content alone. Never consults any name."""
    return _from_head(_pread(fp, 0, _HEAD)) or (ZIP if _has_eocd(fp) else UNKNOWN)


def _from_head(head: bytes) -> str:
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        return ZIP
    if head[:6] == b"7z\xbc\xaf\x27\x1c":
        return SEVENZIP
    # RAR5 first: the two signatures agree up to the sixth byte.
    if head[:8] == b"Rar!\x1a\x07\x01\x00" or head[:7] == b"Rar!\x1a\x07\x00":
        return RAR
    if head[:6] == b"\xfd7zXZ\x00":
        return XZ
    if head[257:263] in (b"ustar\x00", b"ustar "):
        return TAR
    if head[:2] == b"\x1f\x8b":
        return GZIP
    if head[:3] == b"BZh":
        return BZIP2
    if head[:4] == b"\x28\xb5\x2f\xfd":
        return ZSTD
    return ""


def _has_eocd(fp) -> bool:
    """Self-extracting zips carry an executable stub, so the local header is not at 0.

    The End of Central Directory record near the tail still identifies them.
    """
    size = getattr(fp, "size", 0)
    if size < 22:
        return False
    window = min(size, 65536 + 22)
    tail = _pread(fp, size - window, window)
    return EOCD_SIG in tail


# ------------------------------------------------------------------- names and headers


def from_name(name: str) -> str:
    """Map a filename to a format by its extension, or "" if it says nothing."""
    lowered = name.strip().lower()
    for suffix, fmt in _BY_SUFFIX:
        if lowered.endswith(suffix):
            return fmt
    return ""


def from_content_type(value: str) -> str:
    """Map a Content-Type to a format, ignoring the ones that carry no information."""
    mime = value.split(";", 1)[0].strip().lower()
    if not mime or mime in _USELESS_MIME:
        return ""
    return _BY_MIME.get(mime, "")


def _header(headers: Optional[Mapping[str, str]], name: str) -> str:
    """Case-insensitive lookup. Transport lowercases its keys; callers may not."""
    if not headers:
        return ""
    if name in headers:
        return headers[name]
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return ""


def filename_from_headers(headers: Optional[Mapping[str, str]]) -> str:
    """Pull the filename out of Content-Disposition.

    ``Message.get_filename`` is the stdlib's RFC 2231 parser, so the ``filename*=UTF-8''``
    form used for non-ASCII names comes for free. (``cgi.parse_header`` would be the
    obvious alternative and is gone in Python 3.13.)
    """
    raw = _header(headers, "content-disposition")
    if not raw:
        return ""
    msg = Message()
    msg["Content-Disposition"] = raw
    return posixpath.basename((msg.get_filename() or "").replace("\\", "/")).strip()


def filename_for(url: str, headers: Optional[Mapping[str, str]] = None) -> str:
    """The best name we have for the entity: what the server called it, else the URL's."""
    return filename_from_headers(headers) or unquote(posixpath.basename(urlsplit(url).path))


# ------------------------------------------------------------------- the layered answer


def identify(
    fp,
    url: str = "",
    headers: Optional[Mapping[str, str]] = None,
    override: Optional[str] = None,
) -> Detection:
    """Work out the format, falling back through weaker evidence as needed."""
    det = _identify(fp, url, headers, override)
    log.info("identified as %s via %s", det.fmt, det.via)
    log.debug("first bytes: %s", det.head[:16].hex(" ") or "(nothing)")
    return det


def _identify(
    fp,
    url: str,
    headers: Optional[Mapping[str, str]],
    override: Optional[str],
) -> Detection:
    disposition = filename_from_headers(headers)
    url_name = unquote(posixpath.basename(urlsplit(url).path))
    name = disposition or url_name
    ctype = _header(headers, "content-type")
    head = _pread(fp, 0, _HEAD)

    if override and override != "auto":
        return Detection(override, "--archive-type", name, head, ctype)

    magic = _from_head(head)
    if magic:
        # A tar inside gzip/bzip2/zstd is indistinguishable from any other payload
        # without decompressing, so let the name name it — only ever to sharpen the
        # error, since none of the three is listable either way.
        if magic in _TAR_INSIDE and from_name(name) == _TAR_INSIDE[magic]:
            return Detection(_TAR_INSIDE[magic], "magic bytes and filename", name, head, ctype)
        return Detection(magic, "magic bytes", name, head, ctype)

    if _has_eocd(fp):
        return Detection(ZIP, "trailing central directory", name, head, ctype)

    # Nothing in the bytes. Whatever a name says now is a guess, but a guess that names
    # a parser beats failing outright, and ``via`` keeps it honest when it goes wrong.
    fmt = from_name(disposition)
    if fmt:
        return Detection(fmt, f"Content-Disposition filename {disposition!r}", name, head, ctype)

    fmt = from_content_type(ctype)
    if fmt:
        return Detection(fmt, f"Content-Type {ctype.split(';')[0].strip()!r}", name, head, ctype)

    fmt = from_name(url_name)
    if fmt:
        return Detection(fmt, "URL extension", name, head, ctype)

    return Detection(UNKNOWN, "nothing recognisable", name, head, ctype)


# ------------------------------------------------------------------- reporting


def describe(fmt: str) -> str:
    return {
        ZIP: "ZIP archive",
        SEVENZIP: "7-Zip archive",
        XZ: "XZ stream",
        TAR: "POSIX tar archive",
        TAR_XZ: "tar inside an XZ stream",
        RAR: "RAR archive",
        GZIP: "gzip stream",
        BZIP2: "bzip2 stream",
        ZSTD: "zstandard stream",
        TAR_GZ: "tar inside a gzip stream",
        TAR_BZ2: "tar inside a bzip2 stream",
        TAR_ZST: "tar inside a zstandard stream",
    }.get(fmt, "unrecognised data")


def explain_unsupported(det: Detection, size: int = 0) -> str:
    """Why this cannot be listed, in terms the reader can act on."""
    handled = "rvtree handles .tar.xz, .zip, .7z, .rar and .tar"

    if det.fmt in UNINDEXED:
        cost = f"downloading all {human_bytes(size)}" if size else "downloading it whole"
        return (
            f"{describe(det.fmt)} is not supported: {UNINDEXED[det.fmt]} has no block "
            f"index, so nothing in it can be reached without decompressing everything "
            f"before it — listing would mean {cost}. {handled}."
        )

    if det.fmt == UNKNOWN:
        preview = det.head[:16].hex(" ") or "(empty)"
        ctype = f", Content-Type {det.content_type!r}" if det.content_type else ""
        return (
            f"could not identify this as an archive: it starts with {preview}{ctype}. "
            f"A server error page or a login redirect looks exactly like this. "
            f"{handled} — pass --archive-type to force one."
        )

    return f"{describe(det.fmt)} is not supported; {handled}"


def _pread(fp, offset: int, length: int) -> bytes:
    if hasattr(fp, "pread"):
        return fp.pread(offset, length)
    fp.seek(offset)
    return fp.read(length)
