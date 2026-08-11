"""Single-member extraction, verified by hash against the original file."""

from __future__ import annotations

import hashlib
import os
import shutil

import pytest

from rvtree import archive
from rvtree.archive import SingleBlockWarning
from rvtree.formats import rar

MEMBERS = ["payload/sub/deep/note.md", "payload/sub/small.bin", "payload/file_3.txt"]

needs_unrar = pytest.mark.skipif(
    shutil.which("unrar") is None, reason="the helper path needs the unrar binary"
)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def local_sha(fixtures: str, member: str) -> str:
    with open(os.path.join(fixtures, member), "rb") as fh:
        return sha(fh.read())


@pytest.mark.parametrize(
    "name",
    ["test.zip", "test.multiblock.tar.xz", "test.tar", "test.rar4.rar", "test.rar5.rar"],
)
@pytest.mark.parametrize("member", MEMBERS)
def test_extract_round_trip(transport, server, fixtures, name, member):
    arc = archive.open_archive(transport, f"{server.base}/{name}")
    assert sha(archive.extract(arc, member)) == local_sha(fixtures, member)


@pytest.mark.parametrize("member", MEMBERS)
def test_extract_from_7z(transport, server, fixtures, member):
    arc = archive.open_archive(transport, f"{server.base}/test.nonsolid.7z")
    assert sha(archive.extract(arc, member)) == local_sha(fixtures, member)


def test_extract_only_fetches_the_member_for_zip(transport, server, fixtures):
    """A zip member is reachable directly, so cost should track the member, not the archive."""
    arc = archive.open_archive(transport, f"{server.base}/test.zip")
    data = archive.extract(arc, "payload/sub/deep/note.md")
    assert data == b"hello\n"
    assert transport.bytes_fetched < 256 * 1024


def test_solid_7z_extraction_cost_is_reported(transport, server):
    """7z is solid by default: one small file drags in the whole folder, so say so."""
    arc = archive.open_archive(transport, f"{server.base}/test.7z")
    cost = archive.extraction_cost(arc, "payload/sub/deep/note.md")
    assert cost > 10 * 1024 * 1024


def test_nonsolid_7z_extraction_is_cheap(transport, server):
    arc = archive.open_archive(transport, f"{server.base}/test.nonsolid.7z")
    cost = archive.extraction_cost(arc, "payload/sub/deep/note.md")
    assert cost < 1024


def test_missing_member_raises(transport, server):
    arc = archive.open_archive(transport, f"{server.base}/test.zip")
    with pytest.raises(Exception):
        archive.extract(arc, "payload/does-not-exist")


# ----------------------------------------------------------------- rar


def test_rar_extract_only_fetches_the_member(transport, server):
    """A stored RAR member is reachable directly, with no external binary involved."""
    arc = archive.open_archive(transport, f"{server.base}/test.rar5.rar")
    assert archive.extract(arc, "payload/sub/deep/note.md") == b"hello\n"
    assert transport.bytes_fetched < 256 * 1024


def test_rar_extraction_cost_is_the_members_own_packed_size(transport, server):
    arc = archive.open_archive(transport, f"{server.base}/test.rar5.rar")
    assert archive.extraction_cost(arc, "payload/sub/deep/note.md") == 6


def test_solid_rar_extraction_cost_covers_the_whole_run(transport, server):
    """A solid member can only be reached through every member before it; say so."""
    arc = archive.open_archive(transport, f"{server.base}/test.solid.rar5.rar")
    assert archive.extraction_cost(arc, "payload/file_1.txt") < 5 * 1024 * 1024
    assert archive.extraction_cost(arc, "payload/sub/deep/note.md") > 30 * 1024 * 1024


@needs_unrar
@pytest.mark.parametrize("name", ["test.rar4.rar", "test.rar5.rar"])
def test_rar_helper_path_matches_the_stored_path(transport, server, fixtures, name):
    """The archive synthesized for unrar must decode to exactly the stored bytes.

    Forcing a *stored* member down the helper path is what makes this testable at all:
    no `rar` binary exists here to produce a compressed fixture, but the synthesis, the
    verbatim header copying and the unrar invocation are identical either way, and here
    the correct answer is already known.
    """
    arc = archive.open_archive(transport, f"{server.base}/{name}")
    entry, run = arc.rar_member("payload/sub/small.bin")
    data = rar.extract_member(arc.source, entry, arc.rarinfo(), run, force_helper=True)
    assert sha(data) == local_sha(fixtures, "payload/sub/small.bin")


def test_rar_helper_missing_says_what_to_install(transport, server, monkeypatch):
    arc = archive.open_archive(transport, f"{server.base}/test.rar5.rar")
    entry, run = arc.rar_member("payload/sub/small.bin")
    entry.locator["method"] = 3  # as if it had been added with the default -m3
    monkeypatch.setattr(rar, "helper_argv", lambda: None)
    with pytest.raises(rar.HelperUnavailable) as exc:
        rar.extract_member(arc.source, entry, arc.rarinfo(), run)
    assert "unrar" in str(exc.value)


def test_rar_helper_output_is_checked_against_the_header(transport, server, monkeypatch):
    """A helper producing nothing must fail loudly rather than return empty bytes."""
    arc = archive.open_archive(transport, f"{server.base}/test.rar5.rar")
    entry, run = arc.rar_member("payload/sub/small.bin")
    monkeypatch.setattr(rar, "helper_argv", lambda: ("true",))
    with pytest.raises(rar.HelperFailed):
        rar.extract_member(arc.source, entry, arc.rarinfo(), run, force_helper=True)


def test_rar_split_member_refuses_without_guessing_siblings(transport, server):
    arc = archive.open_archive(transport, f"{server.base}/test.multivol.rar5.rar")
    with pytest.raises(rar.MultiVolumeArchive) as exc:
        archive.extract(arc, "small/b.txt")
    assert "volume" in str(exc.value)


def test_rar_missing_member_raises(transport, server):
    arc = archive.open_archive(transport, f"{server.base}/test.rar5.rar")
    with pytest.raises(archive.ArchiveError, match="not found"):
        archive.extract(arc, "payload/does-not-exist")


def test_rar_directory_is_not_a_regular_file(transport, server):
    arc = archive.open_archive(transport, f"{server.base}/test.rar5.rar")
    with pytest.raises(archive.ArchiveError, match="not a regular file"):
        archive.extract(arc, "payload/sub")


# ----------------------------------------------------------------- single-block guard


def test_single_block_xz_refuses_without_force(transport, server):
    arc = archive.open_archive(transport, f"{server.base}/test.singleblock.tar.xz")
    with pytest.raises(SingleBlockWarning) as exc:
        list(archive.list_archive(arc))
    assert "single block" in str(exc.value)
    assert "--force" in str(exc.value)


def test_single_block_xz_proceeds_with_force(transport, server):
    arc = archive.open_archive(transport, f"{server.base}/test.singleblock.tar.xz")
    entries = list(archive.list_archive(arc, force=True))
    assert len(entries) == 14


def test_single_block_detected_before_any_bulk_fetch(transport, server):
    """Identifying the archive must stay cheap even when it has no random access.

    Sniffing used to pull the entire first block, which on a single-block archive is
    the entire file. It must only decode a bounded prefix.
    """
    arc = archive.open_archive(transport, f"{server.base}/test.singleblock.tar.xz")
    assert arc.fmt == "tar.xz"
    assert transport.bytes_fetched < 256 * 1024
