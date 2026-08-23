"""Listing parity: what rvtree reports remotely must match the archive tools locally."""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from rvtree import archive
from rvtree.formats import detect, sevenzip

ARCHIVES = [
    ("test.zip", detect.ZIP),
    ("test.7z", detect.SEVENZIP),
    ("test.multiblock.tar.xz", detect.TAR_XZ),
    ("test.tar", detect.TAR),
    ("test.rar4.rar", detect.RAR),
    ("test.rar5.rar", detect.RAR),
]

RARS = ["test.rar4.rar", "test.rar5.rar"]

needs_unrar = pytest.mark.skipif(
    shutil.which("unrar") is None, reason="ground truth needs the unrar binary"
)

needs_7z = pytest.mark.skipif(
    shutil.which("7z") is None, reason="the fixture is built by the 7z binary"
)


def listing(transport, server, name, **kwargs):
    arc = archive.open_archive(transport, f"{server.base}/{name}")
    return arc, list(archive.list_archive(arc, **kwargs))


def norm(paths) -> set[str]:
    return {p.rstrip("/") for p in paths if p.rstrip("/")}


# ----------------------------------------------------------------- format detection


@pytest.mark.parametrize("name,expected", ARCHIVES)
def test_format_detected_from_magic_not_extension(transport, server, name, expected):
    arc = archive.open_archive(transport, f"{server.base}/{name}")
    assert arc.fmt == expected


# ----------------------------------------------------------------- ground truth


def test_tar_matches_gnu_tar(transport, server, fixtures):
    _, entries = listing(transport, server, "test.tar")
    truth = subprocess.run(
        ["tar", "tf", os.path.join(fixtures, "test.tar")],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    assert norm(e.path for e in entries) == norm(truth)


def test_tar_xz_matches_gnu_tar(transport, server, fixtures):
    _, entries = listing(transport, server, "test.multiblock.tar.xz")
    truth = subprocess.run(
        ["tar", "tf", os.path.join(fixtures, "test.multiblock.tar.xz")],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    assert norm(e.path for e in entries) == norm(truth)


def test_zip_matches_zipinfo(transport, server, fixtures):
    _, entries = listing(transport, server, "test.zip")
    truth = subprocess.run(
        ["zipinfo", "-1", os.path.join(fixtures, "test.zip")],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    assert norm(e.path for e in entries) == norm(truth)


def _7z_truth(path: str) -> dict[str, int]:
    """Parse ``7z l -slt`` into {path: size}, our independent check on a hand-rolled parser."""
    out = subprocess.run(["7z", "l", "-slt", path], capture_output=True, text=True, check=True).stdout
    items: dict[str, int] = {}
    current = None
    for line in out.splitlines():
        if line.startswith("Path = "):
            current = line[7:].strip()
        elif line.startswith("Size = ") and current:
            raw = line[7:].strip()
            items[current] = int(raw) if raw.isdigit() else 0
    items.pop(os.path.basename(path), None)  # 7z echoes the archive name itself
    return items


def test_7z_matches_7z_cli(transport, server, fixtures):
    _, entries = listing(transport, server, "test.7z")
    truth = _7z_truth(os.path.join(fixtures, "test.7z"))
    assert {e.path for e in entries} == set(truth)
    assert {e.path: e.size for e in entries} == truth


def test_7z_directory_and_mode_decoding(transport, server):
    _, entries = listing(transport, server, "test.7z")
    by_path = {e.path: e for e in entries}
    assert by_path["payload"].kind == "dir"
    assert by_path["payload/sub/deep"].kind == "dir"
    assert by_path["payload/file_1.txt"].kind == "file"
    # 7z carries the Unix mode in the high 16 bits when 0x8000 is set.
    assert by_path["payload/file_1.txt"].mode is not None


@needs_7z
def test_7z_symlink_is_not_reported_as_a_file(tmp_path):
    """A 7z symlink is a member whose data is the target, marked only by S_IFLNK.

    Built here rather than read from tests/fixtures because ``7z a`` follows symlinks
    unless it is given ``-snl``, so the standing fixture holds the target's contents
    and cannot show this.
    """
    tree = tmp_path / "p"
    (tree / "sub").mkdir(parents=True)
    (tree / "file.txt").write_text("payload")
    (tree / "sub" / "link.txt").symlink_to("../file.txt")
    arc = tmp_path / "link.7z"
    subprocess.run(
        ["7z", "a", "-snl", "-bso0", "-bsp0", str(arc), "p"],
        cwd=tmp_path, capture_output=True, check=True,
    )

    with open(arc, "rb") as fh:
        entries = {e.path: e for e in sevenzip.read_archive(fh).entries}

    link = entries["p/sub/link.txt"]
    assert link.kind == "symlink"
    assert link.mode_string() == "lrwxrwxrwx"   # what 7z l -slt prints as Attributes
    assert entries["p/file.txt"].kind == "file"
    assert entries["p/sub"].kind == "dir"


def _unrar_truth(path: str) -> dict[str, int]:
    """Parse ``unrar lt`` into {path: size}, our independent check on a hand-rolled parser.

    The fixtures are written by tests/rarwriter.py, so agreeing with ourselves would prove
    nothing. unrar is the reference implementation and has never seen our code.
    """
    out = subprocess.run(["unrar", "lt", path], capture_output=True, text=True, check=True).stdout
    items: dict[str, int] = {}
    current = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Name: "):
            current = line[6:]
            items[current] = 0  # directories carry no Size line at all
        elif line.startswith("Size: ") and current:
            items[current] = int(line[6:])
    return items


@needs_unrar
@pytest.mark.parametrize("name", RARS)
def test_rar_matches_unrar(transport, server, fixtures, name):
    _, entries = listing(transport, server, name)
    truth = _unrar_truth(os.path.join(fixtures, name))
    assert {e.path for e in entries} == set(truth)
    assert {e.path: e.size for e in entries} == truth


@pytest.mark.parametrize("name", RARS)
def test_rar_directory_and_mode_decoding(transport, server, name):
    _, entries = listing(transport, server, name)
    by_path = {e.path: e for e in entries}
    assert by_path["payload"].kind == "dir"
    assert by_path["payload/sub/deep"].kind == "dir"
    assert by_path["payload/file_1.txt"].kind == "file"
    # Written with a Unix host OS, so the attribute field is a st_mode.
    assert by_path["payload/file_1.txt"].mode is not None


@pytest.mark.parametrize("name", RARS)
def test_rar_symlink_preserved(transport, server, name):
    """RAR5 keeps a link target in a header record; RAR4 keeps it in the member's data."""
    _, entries = listing(transport, server, name)
    link = next(e for e in entries if e.path.endswith("link.txt"))
    assert link.kind == "symlink"
    assert link.linkname == "../file_1.txt"


@pytest.mark.parametrize("name", RARS)
def test_rar_sizes_match_tar(transport, server, name):
    _, plain = listing(transport, server, "test.tar")
    _, entries = listing(transport, server, name)
    files = {e.path: e.size for e in entries if e.kind == "file"}
    assert files == {e.path: e.size for e in plain if e.kind == "file"}


@needs_unrar
def test_rar4_unicode_name_decodes(transport, server, fixtures):
    """RAR4's two-bit-opcode name encoding, checked against the reference decoder."""
    _, entries = listing(transport, server, "test.unicode.rar4.rar")
    out = subprocess.run(
        ["unrar", "lb", os.path.join(fixtures, "test.unicode.rar4.rar")],
        capture_output=True, text=True, check=True,
    ).stdout
    assert [e.path for e in entries] == [line for line in out.splitlines() if line]


def test_rar_volume_members_are_flagged_not_chased(transport, server):
    """The volume's own members are real; a split one is marked, and no sibling guessed."""
    arc, entries = listing(transport, server, "test.multivol.rar5.rar")
    assert arc.rarinfo().volume
    by_path = {e.path: e for e in entries}
    assert sorted(by_path) == ["small", "small/a.txt", "small/b.txt"]
    assert by_path["small/a.txt"].locator["split"] is False
    assert by_path["small/b.txt"].locator["split"] is True


@pytest.mark.parametrize(
    "name,expected",
    [
        ("test.rar5.rar", ["RAR 5.0", "solid          no", "volume         no"]),
        ("test.rar4.rar", ["RAR 4.x"]),
        ("test.solid.rar5.rar", ["solid          yes"]),
        ("test.multivol.rar4.rar", ["volume         yes"]),
    ],
)
def test_probe_reports_the_rar_shape(server, capsys, name, expected):
    """Solid and volume both change what you can do next, so probe has to say."""
    from rvtree.cli import main

    assert main(["probe", "--proxy", "none", f"{server.base}/{name}"]) == 0
    out = capsys.readouterr().out
    for fragment in expected:
        assert fragment in out


def test_tar_symlink_preserved(transport, server):
    _, entries = listing(transport, server, "test.multiblock.tar.xz")
    link = next(e for e in entries if e.path.endswith("link.txt"))
    assert link.kind == "symlink"
    assert link.linkname == "../file_1.txt"


def test_sizes_match_between_tar_and_tar_xz(transport, server):
    _, plain = listing(transport, server, "test.tar")
    _, compressed = listing(transport, server, "test.multiblock.tar.xz")
    assert {e.path: e.size for e in plain} == {e.path: e.size for e in compressed}


# ----------------------------------------------------------------- cost regressions


@pytest.mark.parametrize(
    "name,max_bytes",
    [
        ("test.zip", 4 * 1024),        # EOCD + central directory only
        ("test.7z", 4 * 1024),         # signature + encoded header + packed header
        ("test.tar", 512 * 1024),      # 512-byte headers plus adaptive readahead
        # RAR has no index either, but the walker drives its own reads and pulls a 4 KiB
        # window per header rather than paying HttpRangeFile's 16 KiB readahead floor.
        # Measured: ~42 KiB. Falling back to the buffered path would cost ~270 KiB.
        ("test.rar4.rar", 128 * 1024),
        ("test.rar5.rar", 128 * 1024),
    ],
)
def test_listing_stays_cheap(transport, server, name, max_bytes):
    """Guards the index fast paths: a regression here means we lost one."""
    arc, _ = listing(transport, server, name)
    assert transport.bytes_fetched <= max_bytes, (
        f"{name} fetched {transport.bytes_fetched} bytes, over the {max_bytes} budget"
    )


def test_tar_xz_only_reads_blocks_holding_headers(transport, server):
    """Cost tracks header density: this fixture keeps headers in a minority of blocks."""
    arc, entries = listing(transport, server, "test.multiblock.tar.xz")
    assert len(entries) == 14
    # 31 blocks total; the large members mean only a handful carry headers.
    assert transport.bytes_fetched < arc.size * 0.45


def test_limit_blocks_bounds_the_fetch(transport, server):
    arc, entries = listing(transport, server, "test.multiblock.tar.xz", limit_blocks=1)
    assert 0 < len(entries) < 14
    assert transport.bytes_fetched < 2 * 1024 * 1024


def test_collect_reports_entries_and_cost_together(transport, server):
    """The one-call programmatic API: entries plus what fetching them cost."""
    arc = archive.open_archive(transport, f"{server.base}/test.zip")
    listing = archive.collect(arc)
    assert len(listing.entries) == 14
    assert listing.format == detect.ZIP
    assert listing.total_size == sum(e.size for e in listing.entries)
    assert 0 < listing.bytes_fetched < 4 * 1024
    assert listing.requests > 0
