"""Tests for reading a target's own published index instead of crawling it.

The onion this crawler was built against serves an 11 MB `tree -h -f` of 94,228 entries
in its root. Reading it is one request; walking the same tree is seven and a half thousand
over Tor. Two things have to be right for that trade to be worth taking:

  * the paths have to come out exactly, including which of them are directories -- a dump
    marks a directory no differently from a file, so that is inferred from the paths
    themselves rather than from any decoration;
  * the directories must be *recorded and not fetched*. A manifest whose directories were
    queued would spend one request each re-learning what one request already said, which
    is the entire cost the manifest exists to avoid.
"""

import os

import pytest

from crawler.listing import ListingEngine, Page
from crawler.listing.registry import load_profiles
from crawler.listing.strategies.manifest import detect_format

HERE = os.path.dirname(os.path.abspath(__file__))
LISTINGS = os.path.join(HERE, "fixtures", "listings")

PROFILES = load_profiles()
BY_NAME = {p.name: p for p in PROFILES}

# Every fixture describes the same tree.
EXPECTED_FILES = {
    "http://h.onion/files/archive/old.tar",
    "http://h.onion/files/dump.sql.gz",
    "http://h.onion/files/README.txt",
    "http://h.onion/pub/leak.7z",
}
EXPECTED_DIRS = {
    "http://h.onion/files/",
    "http://h.onion/files/archive/",
    "http://h.onion/pub/",
}


def load(name: str) -> str:
    with open(os.path.join(LISTINGS, name), encoding="utf-8") as fh:
        return fh.read()


def read(profile: str, fixture: str, content_type: str = "text/plain"):
    page = Page(url="http://h.onion/manifest.txt", body=load(fixture),
                content_type=content_type)
    return ListingEngine(PROFILES).parse(page, profile=BY_NAME[profile])


@pytest.mark.parametrize("profile,fixture,content_type", [
    ("tree-dump", "tree_dump.txt", "text/plain"),
    ("ls-lr-dump", "ls_lr.txt", "text/plain"),
    ("find-dump", "find_list.txt", "text/plain"),
    ("sitemap-xml", "sitemap.xml", "application/xml"),
])
def test_every_dump_format_yields_the_same_tree(profile, fixture, content_type):
    """Four shells, four formats, one tree."""
    listing = read(profile, fixture, content_type)
    assert {e.url for e in listing.files} == EXPECTED_FILES


@pytest.mark.parametrize("profile,fixture", [
    ("tree-dump", "tree_dump.txt"),
    ("ls-lr-dump", "ls_lr.txt"),
])
def test_directories_are_inferred_from_the_paths(profile, fixture):
    """A dump gives a directory the same `[4.0K]` as anything else; the paths do not."""
    listing = read(profile, fixture)
    assert EXPECTED_DIRS <= {e.url for e in listing.directories}
    assert all(e.size_bytes is None for e in listing.directories)


def test_an_empty_directory_survives_a_tree_dump():
    """`files/incoming` holds nothing, and is still a directory that exists."""
    listing = read("tree-dump", "tree_dump.txt")
    assert "http://h.onion/files/incoming/" in {e.url for e in listing.directories}


def test_sizes_come_through_where_the_format_carries_them():
    tree = {e.name: e.size_bytes for e in read("tree-dump", "tree_dump.txt").files}
    assert tree["dump.sql.gz"] == 512 * 1024 * 1024
    assert tree["README.txt"] == int(4.1 * 1024)
    assert tree["leak.7z"] == int(1.2 * 1024 ** 3)

    ls = {e.name: e.size_bytes for e in read("ls-lr-dump", "ls_lr.txt").files}
    assert ls["dump.sql.gz"] == 536870912
    assert ls["README.txt"] == 4198

    # A `find` dump carries no sizes at all, and inventing one would be a lie.
    assert all(e.size_bytes is None for e in read("find-dump", "find_list.txt").files)


def test_a_manifest_is_not_expanded():
    """The dump is the whole subtree. Queueing its directories is the cost it saves."""
    listing = read("tree-dump", "tree_dump.txt")
    assert listing.directories, "directories still belong in the report"
    assert not listing.expandable, "a manifest's directories must never be fetched"


def test_the_summary_line_is_not_an_entry():
    """`tree` ends with '4 directories, 4 files'. That is not a file."""
    listing = read("tree-dump", "tree_dump.txt")
    assert not any("directories" in e.name for e in listing.entries)


def test_the_dump_resolves_against_its_own_directory():
    """A dump served beside the tree it describes needs no configuration."""
    page = Page(url="http://h.onion/r/fm/TOK/List_of_files.txt",
                body=load("tree_dump.txt"), content_type="text/plain")
    listing = ListingEngine(PROFILES).parse(page, profile=BY_NAME["tree-dump"])
    assert "http://h.onion/r/fm/TOK/files/dump.sql.gz" in {e.url for e in listing.files}


@pytest.mark.parametrize("fixture,expected", [
    ("tree_dump.txt", "tree"),
    ("ls_lr.txt", "ls"),
    ("find_list.txt", "find"),
    ("sitemap.xml", "sitemap"),
])
def test_the_format_is_recognised_without_being_told(fixture, expected):
    """`format = "auto"` exists so a dump of an unexpected shape still reads."""
    assert detect_format(load(fixture)) == expected
