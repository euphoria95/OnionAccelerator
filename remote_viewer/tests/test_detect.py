"""Identifying a format when the URL will not say what it is.

A download endpoint like ``/dl?id=42`` has no extension to read, so rvtree falls through
a ladder of weaker evidence: magic bytes, then the ``Content-Disposition`` filename the
server supplies, then ``Content-Type``, then the URL path. Magic bytes always win — the
whole point is that a name can lie, and acting on a lie means fetching the wrong part of
a very large file.
"""

from __future__ import annotations

import io
import subprocess
import zipfile

import pytest

from rvtree import archive
from rvtree.formats import detect
from rvtree.transport import Transport
from rvtree.util import human_bytes


def _serve(root, **kwargs):
    from rangeserver import RangeServer

    return RangeServer(str(root), **kwargs)


def _blob(data: bytes):
    """A minimal stand-in for HttpRangeFile, for the pure-detection tests."""
    fp = io.BytesIO(data)
    fp.size = len(data)  # type: ignore[attr-defined]
    return fp


ZIP_MAGIC = _blob(b"PK\x03\x04" + b"\x00" * 400)
JUNK = b"<html><title>404 Not Found</title></html>" + b"\x00" * 400


def _write_v7_tar(path, names=("a.txt", "b/c.txt")):
    """A v7 tar carries no ustar magic, so nothing but the name identifies it.

    This is the real shape of the problem: a format ``tarfile`` parses perfectly well but
    that no amount of sniffing can recognise. Written by GNU tar because stdlib
    ``tarfile`` can read the v7 layout but not write it.
    """
    src = path.parent / "_v7src"
    for name in names:
        member = src / name
        member.parent.mkdir(parents=True, exist_ok=True)
        member.write_text("hello")
    subprocess.run(["tar", "--format=v7", "-cf", str(path), *names], cwd=src, check=True)
    assert path.read_bytes()[257:263] == b"\x00" * 6, "fixture is not actually magic-free"
    return path


def _write_zip(path, names=("a.txt", "b/c.txt")):
    with zipfile.ZipFile(path, "w") as zf:
        for name in names:
            zf.writestr(name, "hello")
    return path


# ----------------------------------------------------------------- extension table


@pytest.mark.parametrize(
    "name,expected",
    [
        ("backup.tar.xz", detect.TAR_XZ),
        ("backup.txz", detect.TAR_XZ),
        ("BACKUP.TAR.XZ", detect.TAR_XZ),
        ("backup.tar", detect.TAR),
        ("backup.zip", detect.ZIP),
        ("backup.jar", detect.ZIP),
        ("backup.7z", detect.SEVENZIP),
        ("backup.rar", detect.RAR),
        ("BACKUP.RAR", detect.RAR),
        ("comic.cbr", detect.RAR),
        # Volume sets: .partN.rar ends in .rar; the old scheme's continuations do not.
        ("backup.part1.rar", detect.RAR),
        ("backup.r00", detect.RAR),
        ("backup.xz", detect.XZ),
        # Longest suffix first: .tar.gz must not be read as .gz.
        ("backup.tar.gz", detect.TAR_GZ),
        ("backup.tgz", detect.TAR_GZ),
        ("backup.gz", detect.GZIP),
        ("backup.tar.bz2", detect.TAR_BZ2),
        ("backup.tbz", detect.TAR_BZ2),
        ("backup.tar.zst", detect.TAR_ZST),
        ("download", ""),
        ("", ""),
    ],
)
def test_from_name(name, expected):
    assert detect.from_name(name) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("application/zip", detect.ZIP),
        ("application/x-7z-compressed; charset=binary", detect.SEVENZIP),
        ("application/x-rar-compressed", detect.RAR),
        ("application/vnd.rar", detect.RAR),
        ("application/x-xz", detect.XZ),
        ("application/gzip", detect.GZIP),
        # These say nothing, and servers hand them out for everything.
        ("application/octet-stream", ""),
        ("text/html; charset=utf-8", ""),
        ("", ""),
    ],
)
def test_from_content_type(value, expected):
    assert detect.from_content_type(value) == expected


@pytest.mark.parametrize(
    "header,expected",
    [
        ('attachment; filename="backup.tar.xz"', "backup.tar.xz"),
        ("attachment; filename=backup.7z", "backup.7z"),
        ("attachment; filename*=UTF-8''b%C3%A4ckup.tar.xz", "bäckup.tar.xz"),
        ('attachment; filename="../../etc/passwd"', "passwd"),
        ("inline", ""),
    ],
)
def test_filename_from_headers(header, expected):
    assert detect.filename_from_headers({"content-disposition": header}) == expected


def test_filename_falls_back_to_the_url_path():
    assert detect.filename_for("https://x.onion/a/b/backup.tar.xz?k=1") == "backup.tar.xz"
    assert detect.filename_for("https://x.onion/dl") == "dl"
    assert detect.filename_for("https://x.onion/a%20b.zip") == "a b.zip"


@pytest.mark.parametrize(
    "magic",
    [
        b"Rar!\x1a\x07\x00",          # RAR 1.5 through 4.x
        b"Rar!\x1a\x07\x01\x00",      # RAR 5.0 and later
    ],
)
def test_rar_signatures_are_recognised(magic):
    """The two differ only from the seventh byte, so both arms must be reachable."""
    det = detect.identify(_blob(magic + b"\x00" * 400), "https://x.onion/dl")
    assert det.fmt == detect.RAR
    assert det.via == "magic bytes"


def test_explain_unsupported_names_rar_as_handled():
    det = detect.Detection(detect.GZIP, "magic bytes")
    assert ".rar" in detect.explain_unsupported(det, 1024)


# ----------------------------------------------------------------- precedence


def test_magic_bytes_beat_a_lying_filename():
    det = detect.identify(
        ZIP_MAGIC,
        "https://x.onion/dl?id=1",
        {"content-disposition": 'attachment; filename="thing.7z"'},
    )
    assert det.fmt == detect.ZIP
    assert det.via == "magic bytes"


def test_magic_bytes_beat_a_lying_content_type():
    det = detect.identify(ZIP_MAGIC, "https://x.onion/x.tar", {"content-type": "application/x-tar"})
    assert det.fmt == detect.ZIP


def test_content_disposition_names_it_when_the_bytes_do_not():
    det = detect.identify(
        _blob(JUNK),
        "https://x.onion/dl?id=1",
        {"content-disposition": 'attachment; filename="backup.tar.xz"'},
    )
    assert det.fmt == detect.TAR_XZ
    assert "Content-Disposition" in det.via
    assert det.filename == "backup.tar.xz"


def test_content_type_is_the_next_fallback():
    det = detect.identify(
        _blob(JUNK), "https://x.onion/dl", {"content-type": "application/x-7z-compressed"}
    )
    assert det.fmt == detect.SEVENZIP
    assert "Content-Type" in det.via


def test_url_extension_is_the_last_resort():
    det = detect.identify(_blob(JUNK), "https://x.onion/a/backup.zip?token=abc")
    assert det.fmt == detect.ZIP
    assert det.via == "URL extension"


def test_nothing_recognisable_reports_what_it_saw():
    det = detect.identify(_blob(JUNK), "https://x.onion/dl", {"content-type": "text/html"})
    assert det.fmt == detect.UNKNOWN
    message = detect.explain_unsupported(det)
    assert det.head[:4].hex(" ") in message  # the bytes, so an error page is obvious
    assert "text/html" in message
    assert "--archive-type" in message


def test_an_override_beats_everything():
    det = detect.identify(ZIP_MAGIC, "https://x.onion/x.zip", None, override="tar")
    assert det.fmt == detect.TAR
    assert det.via == "--archive-type"


def test_auto_is_not_treated_as_an_override():
    det = detect.identify(ZIP_MAGIC, "https://x.onion/dl", None, override="auto")
    assert det.fmt == detect.ZIP


def test_headers_are_matched_case_insensitively():
    det = detect.identify(
        _blob(JUNK), "https://x.onion/dl", {"Content-Type": "application/x-7z-compressed"}
    )
    assert det.fmt == detect.SEVENZIP


# ----------------------------------------------------------------- end to end


def test_extensionless_url_is_listed_using_the_servers_filename(tmp_path):
    """The headline case: no extension, no magic bytes, and it still picks tar."""
    _write_v7_tar(tmp_path / "download")
    headers = {"Content-Disposition": 'attachment; filename="stuff.tar"'}
    with _serve(tmp_path, extra_headers=headers) as srv:
        t = Transport(proxy=None, circuits=1)
        try:
            arc = archive.open_archive(t, f"{srv.base}/download")
            assert arc.fmt == detect.TAR
            assert "Content-Disposition" in arc.detection.via
            assert {e.path for e in archive.list_archive(arc)} == {"a.txt", "b/c.txt"}
        finally:
            t.close()


def test_unidentifiable_body_without_hints_fails_with_a_useful_message(tmp_path):
    (tmp_path / "download").write_bytes(JUNK)
    with _serve(tmp_path, extra_headers={"Content-Type": "text/html"}) as srv:
        t = Transport(proxy=None, circuits=1)
        try:
            arc = archive.open_archive(t, f"{srv.base}/download")
            assert arc.fmt == detect.UNKNOWN
            with pytest.raises(archive.UnsupportedFormat) as exc:
                list(archive.list_archive(arc))
            assert "3c 68 74 6d" in str(exc.value)  # "<htm"
        finally:
            t.close()


def test_a_wrong_filename_hint_is_reported_not_raised_raw(tmp_path):
    """Falling back to a name means the name can be wrong, so that has to read well.

    An error page served under an archive name is the everyday version of this.
    """
    (tmp_path / "download").write_bytes(JUNK)
    headers = {"Content-Disposition": 'attachment; filename="nightly-backup.tar"'}
    with _serve(tmp_path, extra_headers=headers) as srv:
        t = Transport(proxy=None, circuits=1)
        try:
            arc = archive.open_archive(t, f"{srv.base}/download")
            assert arc.fmt == detect.TAR
            with pytest.raises(archive.MisidentifiedFormat) as exc:
                list(archive.list_archive(arc))
            message = str(exc.value)
            assert "nightly-backup.tar" in message  # what led us here
            assert "3c 68 74 6d" in message  # and what was actually there
            assert "--archive-type" in message
        finally:
            t.close()


def test_a_wrong_override_is_reported_not_raised_raw(transport, server):
    arc = archive.open_archive(transport, f"{server.base}/test.tar", fmt=detect.ZIP)
    with pytest.raises(archive.MisidentifiedFormat, match="--archive-type"):
        list(archive.list_archive(arc))


def test_a_damaged_archive_says_so_rather_than_blaming_detection(tmp_path):
    """When magic bytes picked the parser, a failure is the archive's fault, not the guess."""
    _write_zip(tmp_path / "broken.zip")
    data = bytearray((tmp_path / "broken.zip").read_bytes())
    data[-22:] = b"\x00" * 22  # wipe the end of central directory, keep the local header
    (tmp_path / "broken.zip").write_bytes(bytes(data))
    with _serve(tmp_path) as srv:
        t = Transport(proxy=None, circuits=1)
        try:
            arc = archive.open_archive(t, f"{srv.base}/broken.zip")
            with pytest.raises(archive.ArchiveError, match="truncated or damaged"):
                list(archive.list_archive(arc))
        finally:
            t.close()


def test_cli_reports_a_misidentified_archive_without_a_traceback(tmp_path, capsys):
    from rvtree.cli import main

    (tmp_path / "download").write_bytes(JUNK)
    headers = {"Content-Disposition": 'attachment; filename="nightly-backup.tar"'}
    with _serve(tmp_path, extra_headers=headers) as srv:
        code = main(["list", "--proxy", "none", f"{srv.base}/download"])
    err = capsys.readouterr().err
    assert code == 1
    assert err.startswith("rvtree: identified as tar")
    assert "Traceback" not in err


def test_magic_still_wins_over_the_url_for_an_extensionless_zip(tmp_path):
    _write_zip(tmp_path / "download")
    with _serve(tmp_path) as srv:
        t = Transport(proxy=None, circuits=1)
        try:
            arc = archive.open_archive(t, f"{srv.base}/download")
            assert arc.fmt == detect.ZIP
            assert arc.detection.via == "magic bytes"
            assert {e.path for e in archive.list_archive(arc)} == {"a.txt", "b/c.txt"}
        finally:
            t.close()


def test_a_plain_xz_is_named_from_the_content_disposition(tmp_path):
    """A lone .xz lists as one file, and /dl is a poor name for it when the server knows better."""
    import lzma

    (tmp_path / "dl").write_bytes(lzma.compress(b"hello" * 1000, format=lzma.FORMAT_XZ))
    headers = {"Content-Disposition": 'attachment; filename="report.txt.xz"'}
    with _serve(tmp_path, extra_headers=headers) as srv:
        t = Transport(proxy=None, circuits=1)
        try:
            arc = archive.open_archive(t, f"{srv.base}/dl")
            assert arc.fmt == detect.XZ
            entries = list(archive.list_archive(arc, force=True))
            assert [e.path for e in entries] == ["report.txt"]
        finally:
            t.close()


def test_extracting_from_a_plain_xz_says_why_it_cannot(tmp_path):
    import lzma

    (tmp_path / "dl.xz").write_bytes(lzma.compress(b"hello" * 1000, format=lzma.FORMAT_XZ))
    with _serve(tmp_path) as srv:
        t = Transport(proxy=None, circuits=1)
        try:
            arc = archive.open_archive(t, f"{srv.base}/dl.xz")
            with pytest.raises(archive.UnsupportedFormat, match="no members"):
                archive.extract(arc, "dl")
        finally:
            t.close()


def test_archive_type_override_forces_the_parser(transport, server):
    """A zip forced to tar must be handed to tarwalk, not quietly re-detected."""
    arc = archive.open_archive(transport, f"{server.base}/test.zip", fmt=detect.TAR)
    assert arc.fmt == detect.TAR
    assert arc.detection.via == "--archive-type"


def test_override_survives_the_tar_in_xz_sniff(transport, server):
    """Forcing xz on a real .tar.xz must stay xz — but still read the index for probe."""
    arc = archive.open_archive(transport, f"{server.base}/test.multiblock.tar.xz", fmt=detect.XZ)
    assert arc.fmt == detect.XZ
    assert arc.xz_index is not None
    assert [e.path for e in archive.list_archive(arc)] == ["test.multiblock.tar"]


def test_gzip_is_named_precisely_and_explained(transport, server):
    """"gzip stream" tells the reader nothing; "tar.gz, and here is why not" does."""
    arc = archive.open_archive(transport, f"{server.base}/test.tar.gz")
    assert arc.fmt == detect.TAR_GZ
    with pytest.raises(archive.UnsupportedFormat) as exc:
        list(archive.list_archive(arc))
    message = str(exc.value)
    assert "gzip" in message and "no block index" in message
    assert human_bytes(arc.size) in message  # names the transfer it would have cost


def test_identification_costs_a_single_range_request(transport, server):
    """Identification runs before anything else, so it has to stay nearly free."""
    arc = archive.open_archive(transport, f"{server.base}/test.zip")
    assert arc.detection.via == "magic bytes"
    assert transport.requests_made == 1
    assert transport.bytes_fetched <= 512
