"""Tests for the universal "Index of" parser.

The parser's whole claim is that it does not know or care which web server produced a
listing, so the suite is built as one set of expectations run against listings from
five different servers plus a hand-rolled template. Every fixture describes the *same*
directory -- two subdirectories, two files -- so a single assertion covers all of them,
and a server-specific assumption creeping into the parser shows up as one failure
rather than as a subtly different result nobody notices.

The negative fixture matters just as much: a page that is not an open directory has to
come back is_index=False, because that is the only thing standing between a directory
walk and an unbounded crawl of somebody's forum over Tor.
"""

import json
import os

import pytest

from crawler.indexparse import parse_index, score_index, _parse_size
from crawler.urlnorm import is_parent_dir

# A custom file-manager autoindex: no "Index of" title, no server <address> footer, a
# search <form> on every page, Apache-style column-sort links, and a "Parent directory"
# up-link whose href carries no trailing slash. This is the shape that scored below the
# threshold and was wrongly abandoned as a leaf whenever it held only one entry.
FILEMANAGER_ONE_ENTRY = (
    '<html><head><meta charset="utf-8"></head><body>'
    '<form action="/r/fm/TOK/search" method="get"><input name="search"></form>'
    '<table><thead><tr>'
    '<th><a href="?C=N&O=A">Name</a><a href="?C=N&O=D">down</a></th>'
    '<th><a href="?C=S&O=A">Size</a><a href="?C=S&O=D">down</a></th>'
    '<th><a href="?C=M&O=A">Date</a><a href="?C=M&O=D">down</a></th>'
    '</tr></thead><tbody>'
    '<tr><td><a href="/r/fm/TOK">Parent directory/</a></td><td>-</td><td>-</td></tr>'
    '<tr><td><a href="/r/fm/TOK/IT/report.txt">report.txt</a></td>'
    '<td>88</td><td>2024-01-01 00:00</td></tr>'
    '</tbody></table></body></html>'
)
FILEMANAGER_BASE = "http://examplexyz.onion/r/fm/TOK/IT"      # note: no trailing slash

HERE = os.path.dirname(os.path.abspath(__file__))
LISTINGS = os.path.join(HERE, "fixtures", "listings")

BASE = "http://examplexyz.onion/files/"

# Every HTML fixture below describes this directory.
EXPECTED_DIRS = {
    "http://examplexyz.onion/files/archive/",
    "http://examplexyz.onion/files/incoming/",
}
EXPECTED_FILES = {
    "http://examplexyz.onion/files/dump.sql.gz",
    "http://examplexyz.onion/files/README.txt",
}

# The junk every one of them has to shed: parent links, sort controls, column headers.
FORBIDDEN_SUBSTRINGS = ("?C=", "?N=", "?M=", "?S=", "?T=", "?sort=")
FORBIDDEN_NAMES = {"name", "last modified", "size", "description", "parent directory",
                   "..", "up one level", "type"}


def load(name: str) -> str:
    with open(os.path.join(LISTINGS, name), encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------- the universal claim


@pytest.mark.parametrize("fixture", [
    "apache_pre.html",
    "apache_table.html",
    "nginx.html",
    "lighttpd.html",
])
def test_every_server_yields_the_same_listing(fixture):
    """Five servers, five markups, one result."""
    listing = parse_index(load(fixture), BASE)

    assert listing.is_index, f"{fixture} scored {listing.confidence}"
    assert {e.url for e in listing.directories} == EXPECTED_DIRS
    assert {e.url for e in listing.files} == EXPECTED_FILES


@pytest.mark.parametrize("fixture", [
    "apache_pre.html",
    "apache_table.html",
    "nginx.html",
    "lighttpd.html",
    "custom_template.html",
])
def test_navigation_and_sorting_junk_is_dropped(fixture):
    """No parent link, no sort control and no column header survives, on any server."""
    listing = parse_index(load(fixture), BASE if "custom" not in fixture
                          else "http://examplexyz.onion/files/sub/")
    for entry in list(listing.directories) + list(listing.files):
        assert entry.name.strip().lower() not in FORBIDDEN_NAMES, entry
        for junk in FORBIDDEN_SUBSTRINGS:
            assert junk not in entry.url, entry
        # Nothing may resolve above the directory being listed.
        assert "/files/" in entry.url and not entry.url.endswith("/files/"), entry


@pytest.mark.parametrize("fixture", [
    "apache_pre.html", "apache_table.html", "nginx.html", "lighttpd.html",
])
def test_sizes_and_dates_are_recovered(fixture):
    """Row metadata is best-effort, but on these four it should be there."""
    listing = parse_index(load(fixture), BASE)
    by_name = {e.name: e for e in listing.files}

    dump = by_name["dump.sql.gz"]
    assert dump.size_bytes == 512 * 1024 * 1024
    assert dump.mtime_text is not None

    readme = by_name["README.txt"]
    assert readme.size_bytes in (4198, int(4.1 * 1024))

    # Directories advertise no size, and '-' must not be read as one.
    assert all(d.size_bytes is None for d in listing.directories)


# ---------------------------------------------------------------- hard cases


def test_directories_without_a_trailing_slash():
    """A template that strips the slash must still be categorised correctly.

    This is the case that defeats "is_dir = href.endswith('/')". `archive` and `notes`
    are indistinguishable by name; only the presence of a size in the row separates
    them.
    """
    listing = parse_index(load("custom_template.html"),
                          "http://examplexyz.onion/files/sub/")

    assert {e.name for e in listing.directories} == {"archive", "incoming"}
    assert {e.name for e in listing.files} == {"dump.sql.gz", "notes"}
    assert listing.is_index


def test_breadcrumbs_are_not_entries():
    """Breadcrumb links point above the page, so they are navigation, not content."""
    listing = parse_index(load("custom_template.html"),
                          "http://examplexyz.onion/files/sub/")
    urls = {e.url for e in listing.directories} | {e.url for e in listing.files}
    assert "http://examplexyz.onion/" not in urls
    assert "http://examplexyz.onion/files/" not in urls


def test_json_autoindex():
    """nginx's autoindex_format json carries the same information without markup."""
    listing = parse_index(load("nginx_autoindex.json"), BASE,
                          content_type="application/json")

    assert listing.is_index
    assert {e.url for e in listing.directories} == EXPECTED_DIRS
    assert {e.url for e in listing.files} == EXPECTED_FILES
    assert {e.name: e.size_bytes for e in listing.files} == {
        "dump.sql.gz": 536870912, "README.txt": 4198,
    }


def test_json_that_is_not_a_listing_falls_back_instead_of_reporting_empty():
    """A JSON body of some other shape must not be read as 'this directory is empty'."""
    listing = parse_index(json.dumps({"error": "nope"}), BASE,
                          content_type="application/json")
    assert listing.total == 0
    assert not listing.is_index


def test_a_web_application_is_not_an_index():
    """The guard that stops a directory walk turning into a site crawl."""
    listing = parse_index(load("not_an_index.html"),
                          "http://examplexyz.onion/files/sub/")
    assert not listing.is_index
    assert listing.confidence < 0.5


def test_offsite_links_are_dropped_unless_asked_for():
    page = load("not_an_index.html")
    base = "http://examplexyz.onion/files/sub/"
    assert all("elsewherexyz" not in e.url
               for e in parse_index(page, base).files)
    # ...and mailto: is never an entry, even with --allow-offsite.
    entries = parse_index(page, base, allow_offsite=True)
    assert all(not e.url.startswith("mailto") for e in entries.files)


def test_empty_directory_is_still_an_index():
    """An open directory with nothing in it must not be mistaken for a broken page."""
    html = "<html><head><title>Index of /files/empty/</title></head><body>" \
           "<h1>Index of /files/empty/</h1><pre><a href=\"../\">../</a></pre></body></html>"
    listing = parse_index(html, "http://examplexyz.onion/files/empty/")
    assert listing.is_index
    assert listing.total == 0


def test_duplicate_links_collapse():
    """Listings that link a target from both its icon and its name yield one entry."""
    html = ('<html><title>Index of /files/</title><body><table>'
            '<tr><td><a href="archive/"><img src="/i/folder.gif" alt="[DIR]"></a></td>'
            '<td><a href="archive/">archive/</a></td></tr></table></body></html>')
    listing = parse_index(html, BASE)
    assert len(listing.directories) == 1
    assert listing.directories[0].name == "archive"


# ---------------------------------------------------------------- the parent-link signal


def test_single_entry_directory_is_still_an_index():
    """A directory holding one file must not be abandoned as a leaf.

    Without the parent-directory signal this page scores 0.3 -- one sort-link block and
    nothing else, because it has no "Index of" title, no server footer, a search form,
    one entry, and a low entry/link ratio -- and the crawler records it as a leaf,
    silently dropping the file (or, for a lone subdirectory, the whole subtree). This is
    also the slash-less-URL case: the page is served at `/r/fm/TOK/IT` with no redirect,
    so the parent test must measure against `/r/fm/TOK/IT/`, not the `/r/fm/TOK/` that
    relative hrefs resolve against.
    """
    listing = parse_index(FILEMANAGER_ONE_ENTRY, FILEMANAGER_BASE)

    assert listing.is_index, f"scored only {listing.confidence}"
    assert {e.name for e in listing.files} == {"report.txt"}
    assert listing.files[0].size_bytes == 88


def test_parent_up_link_is_a_signal_not_an_entry():
    """The up-link contributes to the score but is never itself a listing entry."""
    listing = parse_index(FILEMANAGER_ONE_ENTRY, FILEMANAGER_BASE)
    urls = {e.url for e in listing.directories} | {e.url for e in listing.files}
    assert "http://examplexyz.onion/r/fm/TOK" not in urls
    assert "http://examplexyz.onion/r/fm/TOK/" not in urls
    for entry in list(listing.directories) + list(listing.files):
        assert entry.name.strip().lower() not in {"parent directory", "parent directory/"}


def test_parent_link_does_not_rescue_an_application():
    """The signal is the *immediate* parent, so an app's stray up-links don't fire it.

    not_an_index.html links `/rules` and `/faq`, which sit above `/files/sub/` but are
    not its parent directory; the guard that keeps a directory walk from becoming a site
    crawl has to stay shut for them.
    """
    listing = parse_index(load("not_an_index.html"),
                          "http://examplexyz.onion/files/sub/")
    assert not listing.is_index
    assert listing.confidence < 0.5


def test_is_parent_dir_is_segment_aligned():
    """Only the immediate, segment-aligned parent counts -- not any shared ancestor."""
    assert is_parent_dir("http://h.onion/a/b", "http://h.onion/a/b/c/")
    assert is_parent_dir("http://h.onion/a/b/", "http://h.onion/a/b/c/")
    assert is_parent_dir("http://h.onion/files/", "http://h.onion/files/sub/")
    # A grandparent is not the parent.
    assert not is_parent_dir("http://h.onion/a/", "http://h.onion/a/b/c/")
    # A sibling branch that merely shares the root is not a parent.
    assert not is_parent_dir("http://h.onion/rules", "http://h.onion/files/sub/")
    # Cross-host never counts.
    assert not is_parent_dir("http://other.onion/a/", "http://h.onion/a/b/")


def test_score_index_counts_the_parent_link():
    """A lone-entry listing clears the bar with the up-link and misses it without."""
    without = score_index(title=None, n_entries=1, n_links=8, n_sort_links=6,
                          has_form=True, has_parent_link=False)
    with_link = score_index(title=None, n_entries=1, n_links=8, n_sort_links=6,
                            has_form=True, has_parent_link=True)
    assert without < 0.5 <= with_link


# ---------------------------------------------------------------- units


@pytest.mark.parametrize("row,expected", [
    ("2023-11-02 14:29  512M", 512 * 1024 * 1024),
    ("2023-11-02 14:29  4.1K", int(4.1 * 1024)),
    ("2023-11-02 14:29  1.2 GiB", int(1.2 * 1024 ** 3)),
    ("02-Nov-2023 14:29  536870912", 536870912),
    ("2023-11-02 14:31    -", None),          # a directory's empty size column
    ("", None),
])
def test_parse_size(row, expected):
    assert _parse_size(row) == expected


def test_size_ignores_digits_in_the_filename():
    """`leak-2024.tar.gz` must not donate its year to the size column."""
    assert _parse_size("leak-2024.tar.gz  2023-11-02 14:29  4.1K",
                       exclude="leak-2024.tar.gz") == int(4.1 * 1024)


def test_score_index_needs_more_than_in_scope_links():
    """Many in-scope links alone are not enough; a form and no index title sink it."""
    assert score_index(title="Welcome", n_entries=20, n_links=25,
                       n_sort_links=0, has_form=True) < 0.5
    assert score_index(title="Index of /pub", n_entries=2, n_links=3,
                       n_sort_links=0, has_form=False) >= 0.5
