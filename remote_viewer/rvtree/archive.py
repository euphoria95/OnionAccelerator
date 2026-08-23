"""Format dispatch: turn a remote URL into a listing, or into one extracted member."""

from __future__ import annotations

import dataclasses
import logging
import lzma
import posixpath
import tarfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
from typing import Iterator, Optional

from . import progress
from .formats import detect as detect_mod
from .formats import rar, sevenzip, tarwalk, xz, zipfmt
from .model import FILE, Entry, Listing
from .transport import Capabilities, HttpRangeFile, Transport, probe, spool
from .util import human_bytes, human_duration

log = logging.getLogger(__name__)

# Enough compressed input to yield the ~1 KiB of plaintext needed to spot a tar header,
# and small enough to stay cheap on an archive with no random access at all.
SNIFF_PREFIX = 64 * 1024


class ArchiveError(RuntimeError):
    pass


class UnsupportedFormat(ArchiveError):
    pass


class MisidentifiedFormat(ArchiveError):
    """The chosen parser refused the bytes it was given."""


# What the stdlib parsers raise when handed something that is not their format. Kept
# narrow on purpose: this converts a wrong guess into an explanation, and must not
# swallow a genuine bug in the walkers.
PARSER_ERRORS = (tarfile.TarError, zipfile.BadZipFile, lzma.LZMAError, EOFError)


@dataclasses.dataclass
class SingleBlockWarning(ArchiveError):
    """Raised instead of quietly starting a transfer that has no shortcut.

    A stream written by single-threaded ``xz`` is one block, so the last tar header can
    only be reached by decompressing everything before it. There is no clever way
    around this, and at Tor speeds the difference between "seconds" and "days" deserves
    an explicit decision.
    """

    compressed: int
    uncompressed: int
    throughput: float

    def __str__(self) -> str:
        secs = self.compressed / max(self.throughput, 1.0)
        return (
            f"this .xz has a single block, so it cannot be read out of order: listing it "
            f"means streaming all {human_bytes(self.compressed)} of compressed data "
            f"({human_bytes(self.uncompressed)} uncompressed).\n"
            f"At the observed {human_bytes(self.throughput)}/s that is about "
            f"{human_duration(secs)}.\n"
            f"Re-run with --force to do it anyway."
        )


@dataclasses.dataclass
class RangelessWarning(ArchiveError):
    """Raised when the server will only serve the entity whole, from byte zero.

    Not a failure of rvtree so much as the removal of the thing it is for. A server that
    answers ``Range`` with 200 — an application handing the file out itself, which is
    what a proof-of-work download gate always is — leaves exactly one way to reach the
    metadata at the end of an archive: pull all of it. That is a decision with a price
    on it, so it is offered rather than taken.
    """

    url: str
    size: int
    throughput: float
    gated: bool

    def __str__(self) -> str:
        secs = self.size / max(self.throughput, 1.0)
        why = (
            "it is behind a proof-of-work gate, and the URL that gate grants serves the "
            "file in one stream"
            if self.gated
            else "it does not support partial content"
        )
        return (
            f"this server will not serve byte ranges: {why}.\n"
            f"Nothing can be read out of order, so listing this archive means "
            f"transferring all {human_bytes(self.size)} of it — about "
            f"{human_duration(secs)} at the observed {human_bytes(self.throughput)}/s, "
            f"with no resume if it breaks.\n"
            f"Re-run with --spool FILE to do it anyway and keep the download, or "
            f"--spool-temp to throw it away afterwards."
        )


@dataclasses.dataclass
class Archive:
    """An opened remote archive plus everything already learned about it.

    Holding the xz index and any block already fetched here means identifying the
    format costs nothing extra: the listing reuses both.
    """

    source: HttpRangeFile
    fmt: str
    detection: Optional[detect_mod.Detection] = None
    xz_index: Optional[xz.XzIndex] = None
    raw_blocks: dict[int, bytes] = dataclasses.field(default_factory=dict)
    # The object a progress display should sample: whatever the walk reads through, so
    # that ``tell()`` against its ``size`` is a real fraction of the work. Left unset
    # for the formats whose listing is a header read rather than a walk.
    walk_source: Optional[object] = None
    _sevenzip: Optional[sevenzip.SevenZipArchive] = None
    _rar: Optional[rar.RarInfo] = None
    _rar_members: dict = dataclasses.field(default_factory=dict)

    @property
    def url(self) -> str:
        return self.source.url

    @property
    def size(self) -> int:
        return self.source.size

    def sevenzip(self) -> sevenzip.SevenZipArchive:
        """Parse the 7z header once and reuse it; listing and extraction both need it."""
        if self._sevenzip is None:
            self._sevenzip = sevenzip.read_archive(self.source)
        return self._sevenzip

    def rarinfo(self) -> rar.RarInfo:
        """Read the RAR signature and main header once; two small reads."""
        if self._rar is None:
            self._rar = rar.read_info(self.source)
        return self._rar

    def rar_member(self, path: str) -> tuple[Optional[Entry], list[Entry]]:
        """Locate a member and its solid run, cached.

        Finding one means walking the header chain, and ``extraction_cost`` asks for the
        same member immediately before ``extract`` does. Caching turns that into one walk.
        """
        if path not in self._rar_members:
            self._rar_members[path] = rar.find_member(self.source, path, info=self.rarinfo())
        return self._rar_members[path]


def open_archive(
    transport: Transport,
    url: str,
    fmt: Optional[str] = None,
    spool_to: Optional[str] = None,
    allow_spool: bool = False,
) -> Archive:
    """Open a URL and identify what it holds, without over-fetching.

    ``fmt`` forces a parser and skips detection entirely, for the cases where the bytes
    and the server both mislead. ``allow_spool`` is the caller saying it accepts a whole
    download if the server turns out to refuse ranges; ``spool_to`` names where to keep it.
    """
    bar = progress.get()
    bar.stage("probe", url)
    caps = probe(transport, url)
    if caps.accepts_ranges:
        # The probe already learned both, so this reader starts warm rather than
        # spending a second round trip on a question just answered.
        source = HttpRangeFile(transport, url, size=caps.size, headers=caps.headers)
    else:
        source = _spooled(transport, url, caps, spool_to, allow_spool, bar)

    bar.stage("detect", human_bytes(source.size))
    det = detect_mod.identify(source, url, headers=source.headers, override=fmt)
    arc = Archive(source, det.fmt, detection=det)

    if arc.fmt in (detect_mod.XZ, detect_mod.TAR_XZ):
        bar.stage("index", "reading the xz block index")
        arc.xz_index = xz.read_index(source)
        if arc.fmt == detect_mod.XZ and not fmt:
            bar.stage("sniff", "is there a tar inside?")
            _sniff_tar_in_xz(arc, det)
    return arc


def _spooled(
    transport: Transport,
    url: str,
    caps: Capabilities,
    spool_to: Optional[str],
    allow_spool: bool,
    bar,
):
    """Fall back to a local copy, once the caller has agreed to pay for one."""
    if not allow_spool:
        raise RangelessWarning(
            url=url, size=caps.size, throughput=transport.throughput, gated=caps.gated
        )
    log.info(
        "%s serves no ranges; spooling all %s to disk in one stream",
        url,
        human_bytes(caps.size),
    )
    bar.stage("spool", f"{human_bytes(caps.size)}, no ranges — one pass, no resume")
    written = {"n": 0}

    def advance(n: int) -> None:
        written["n"] = n

    bar.track_bytes(lambda: written["n"], caps.size)
    return spool(
        transport,
        url,
        path=spool_to,
        expected_size=caps.size,
        headers=caps.headers,
        on_progress=advance,
    )


def _sniff_tar_in_xz(arc: Archive, det: detect_mod.Detection) -> None:
    """Decide whether an .xz holds a tar, by decoding a bounded prefix of its first block.

    Only a prefix: on a single-block archive the first block is the entire file, and
    pulling it to answer a question about the first 264 bytes would defeat the tool.
    """
    if arc.xz_index and arc.xz_index.blocks:
        first = arc.xz_index.blocks[0]
        span = min(first.unpadded_size, SNIFF_PREFIX)
        prefix = arc.source.pread(first.comp_offset, span)
        if span == first.unpadded_size:
            arc.raw_blocks[first.index] = prefix
        try:
            head = xz.decode_block_prefix(prefix, 1024)
        except Exception:
            head = b""
        if head[257:263] in (b"ustar\x00", b"ustar "):
            log.info("block 0 decodes to a tar header: this is a tar.xz")
            arc.fmt = det.fmt = detect_mod.TAR_XZ
            det.via = "magic bytes"
            return
        if head:
            log.info("block 0 decoded and holds no tar header: a plain .xz stream")
            return  # decoded fine and it is not a tar; the name cannot outvote that

    # The prefix would not decode, so nothing was learned either way. A name saying
    # tar.xz is the only evidence left, and tarwalk will fail loudly if it is wrong.
    if detect_mod.from_name(det.filename) == detect_mod.TAR_XZ:
        arc.fmt = det.fmt = detect_mod.TAR_XZ
        det.via = "filename (the first block would not decode)"


def _entity_name(arc: Archive) -> str:
    """What the server (or failing that the URL) calls this file."""
    if arc.detection and arc.detection.filename:
        return arc.detection.filename
    return posixpath.basename(arc.url.split("?")[0])


def _explain(arc: Archive) -> str:
    det = arc.detection or detect_mod.Detection(arc.fmt, "--archive-type")
    return detect_mod.explain_unsupported(det, arc.size)


def _misparsed(arc: Archive, exc: Exception) -> MisidentifiedFormat:
    """Say which piece of evidence was wrong, rather than letting the parser's error out.

    Identification can fall back to a filename, and a filename can be wrong — a login
    page served under ``backup.tar`` is the common case. When that happens the reader
    needs to know *why* rvtree chose this parser, not what line of ``tarfile`` gave up.
    """
    det = arc.detection
    head = (det.head[:16].hex(" ") if det and det.head else "") or "(nothing)"
    if det and det.via.startswith("magic bytes"):
        return MisidentifiedFormat(
            f"this is a {detect_mod.describe(arc.fmt)} by its magic bytes, but parsing it "
            f"failed: {exc}. The archive is probably truncated or damaged."
        )
    chosen = f"identified as {arc.fmt} from {det.via}" if det else f"parsed as {arc.fmt}"
    return MisidentifiedFormat(
        f"{chosen}, but it does not parse as one: {exc}\n"
        f"The bytes start with {head}, which is not {detect_mod.describe(arc.fmt)}. "
        f"Pass --archive-type to name the right parser."
    )


def _guarded(arc: Archive, entries: Iterator[Entry]) -> Iterator[Entry]:
    """Wrap an entry stream so a parser rejecting the bytes becomes an explanation.

    A generator, because the walkers are lazy: the failure usually lands on the first
    ``next()``, long after ``list_archive`` returned.
    """
    try:
        yield from entries
    except PARSER_ERRORS as exc:
        raise _misparsed(arc, exc) from exc


def list_archive(
    arc: Archive,
    force: bool = False,
    circuits: int = 1,
    limit: Optional[int] = None,
    limit_blocks: Optional[int] = None,
    segments: int = 1,
) -> Iterator[Entry]:
    """Stream the archive's entries, so a huge tree stays lazy.

    ``segments`` only reaches the two formats that have to be *walked* — RAR and plain
    tar. Those are the ones whose cost is one round trip per member, and splitting the
    chain between several walkers is the only thing that changes that. The xz path is
    already parallel a block at a time, and zip and 7z read an index instead of walking.
    """
    fmt = arc.fmt

    if fmt == detect_mod.ZIP:
        return _guarded(arc, zipfmt.walk(arc.source))

    if fmt == detect_mod.SEVENZIP:
        return iter(arc.sevenzip().entries)

    if fmt == detect_mod.RAR:
        # No stream position to sample: the RAR walker drives its own positional reads
        # and never moves the file object, so it publishes its offsets itself.
        if segments > 1:
            return _guarded(
                arc,
                rar.walk_parallel(
                    arc.source, info=arc.rarinfo(), limit=limit, segments=segments
                ),
            )
        return _guarded(arc, rar.walk(arc.source, info=arc.rarinfo(), limit=limit))

    if fmt == detect_mod.TAR:
        if segments > 1:
            # Deliberately no ``walk_source``: several walkers hold several positions,
            # and sampling one of them would draw a bar that runs backwards.
            return _guarded(arc, tarwalk.walk_parallel(arc.source, limit=limit, segments=segments))
        arc.walk_source = arc.source
        return _guarded(arc, tarwalk.walk(arc.source, limit=limit))

    if fmt in (detect_mod.TAR_XZ, detect_mod.XZ):
        index = arc.xz_index or xz.read_index(arc.source)
        if not index.seekable and not force:
            raise SingleBlockWarning(
                compressed=index.compressed_size,
                uncompressed=index.uncompressed_size,
                throughput=arc.source.transport.throughput,
            )
        if fmt == detect_mod.XZ:
            # Not a tar, so the stream is one decompressed file with no inner tree.
            name = _entity_name(arc) or "stream"
            if name.endswith(".xz"):
                name = name[:-3]
            return iter([Entry(path=name, size=index.uncompressed_size, kind=FILE)])
        stream = _xz_stream(arc, index, circuits, limit_blocks)
        arc.walk_source = stream
        return _guarded(arc, _releasing(stream, tarwalk.walk(stream, limit=limit)))

    raise UnsupportedFormat(_explain(arc))


def _releasing(stream: xz.XzBlockFile, entries: Iterator[Entry]) -> Iterator[Entry]:
    """Give the prefetch threads back once the walk is over, however it ends.

    ``--limit`` and a caller that stops early both leave the walk unfinished, and the
    executor behind it would otherwise live until interpreter shutdown.
    """
    try:
        yield from entries
    finally:
        stream.release()


def _xz_stream(
    arc: Archive,
    index: xz.XzIndex,
    circuits: int,
    limit_blocks: Optional[int] = None,
) -> xz.XzBlockFile:
    if limit_blocks:
        index = xz.XzIndex(index.blocks[:limit_blocks], index.streams, index.check_type)
    executor = ThreadPoolExecutor(max_workers=circuits) if circuits > 1 else None
    return xz.XzBlockFile(
        arc.source,
        index,
        prefetch=max(0, circuits - 1),
        executor=executor,
        owns_executor=executor is not None,
        raw_cache=arc.raw_blocks,
    )


def collect(arc: Archive, **kwargs) -> Listing:
    """Eagerly materialise a listing, recording what it cost."""
    transport = arc.source.transport
    before_bytes = transport.bytes_fetched
    before_reqs = transport.requests_made
    items = list(list_archive(arc, **kwargs))
    return Listing(
        entries=items,
        format=arc.fmt,
        total_size=sum(e.size for e in items),
        bytes_fetched=transport.bytes_fetched - before_bytes,
        requests=transport.requests_made - before_reqs,
    )


# ------------------------------------------------------------------- extraction


def extract(arc: Archive, path: str, circuits: int = 1) -> bytes:
    """Fetch one member's bytes, touching as little of the archive as possible.

    ``circuits`` only reaches the ``.tar.xz`` path, where reaching a member means walking
    the tar inside the stream — the same walk ``list_archive`` parallelises, and until now
    the only one that was left serial no matter how many lanes were available.
    """
    fmt = arc.fmt

    try:
        if fmt == detect_mod.ZIP:
            with zipfmt.open_member(arc.source, path) as fh:
                return fh.read()

        if fmt == detect_mod.SEVENZIP:
            return _extract_7z(arc, path)

        if fmt == detect_mod.RAR:
            return _extract_rar(arc, path)

        if fmt == detect_mod.TAR:
            return _extract_tar(arc.source, path)

        if fmt == detect_mod.TAR_XZ:
            index = arc.xz_index or xz.read_index(arc.source)
            stream = _xz_stream(arc, index, circuits)
            try:
                return _extract_tar(stream, path)
            finally:
                # Hands the prefetch threads back. Without this the executor lives until
                # interpreter shutdown, which only became visible once extraction was
                # allowed more than one circuit.
                stream.release()
    except PARSER_ERRORS as exc:
        raise _misparsed(arc, exc) from exc
    except KeyError as exc:
        # zipfile's way of saying "no such member".
        raise ArchiveError(f"{path!r} not found in archive") from exc

    if fmt == detect_mod.XZ:
        raise UnsupportedFormat(
            "this .xz is a single compressed stream, not an archive, so it has no members "
            "to extract; download the URL and decompress it instead"
        )
    raise UnsupportedFormat(_explain(arc))


def _extract_tar(stream, path: str) -> bytes:
    for entry in tarwalk.walk(stream):
        if entry.path != path:
            continue
        if entry.kind != FILE:
            raise ArchiveError(f"{path!r} is a {entry.kind}, not a regular file")
        loc = entry.locator or {}
        stream.seek(loc["offset"])
        return stream.read(loc["size"])
    raise ArchiveError(f"{path!r} not found in archive")


def _extract_7z(arc: Archive, path: str) -> bytes:
    parsed = arc.sevenzip()
    for entry in parsed.entries:
        if entry.path != path:
            continue
        if entry.locator is None:
            return b""
        loc = entry.locator
        folder = parsed.folders[loc["folder"]]
        packed = arc.source.pread(parsed.base_offset + folder.pack_offset, folder.packed_size)
        plain = sevenzip.decode_folder(folder, packed)
        return plain[loc["offset"] : loc["offset"] + loc["size"]]
    raise ArchiveError(f"{path!r} not found in archive")


def _extract_rar(arc: Archive, path: str) -> bytes:
    entry, run = arc.rar_member(path)
    if entry is None:
        raise ArchiveError(f"{path!r} not found in archive")
    if entry.kind != FILE:
        raise ArchiveError(f"{path!r} is a {entry.kind}, not a regular file")
    return rar.extract_member(arc.source, entry, arc.rarinfo(), run)


def extraction_cost(arc: Archive, path: str) -> int:
    """Compressed bytes an extraction would pull down.

    The formats that need this are the solid ones. 7z is solid by default, so one small
    file can drag in its entire containing folder; a solid RAR member likewise needs
    every member back to the last one that started a fresh dictionary.
    """
    if arc.fmt == detect_mod.RAR:
        _entry, run = arc.rar_member(path)
        return sum((e.locator or {}).get("csize", 0) for e in run)

    if arc.fmt != detect_mod.SEVENZIP:
        return 0
    parsed = arc.sevenzip()
    for entry in parsed.entries:
        if entry.path == path and entry.locator is not None:
            return parsed.folders[entry.locator["folder"]].packed_size
    return 0
