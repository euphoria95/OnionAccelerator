"""Tests for addressing: how a listed row becomes the next request.

This is the layer the redesign added, and the one that decides whether a crawl of a file
manager goes anywhere at all. Four schemes, one rule each, and a set of cases that are all
drawn from things servers actually do: fold a path into one escaped segment, keep a
session token in the query beside the path, serve a file's bytes from a different URL than
its listing row, and answer a directory only to a POST.
"""

import pytest

from crawler.listing.model import PageRequest
from crawler.listing.navigate import Location, next_page, resolve, seed_request
from crawler.listing.profile import Extract, Navigate, Paginate, Download, Profile
from crawler.listing.strategies import ExtractResult, RawEntry


def profile(**navigate) -> Profile:
    download = navigate.pop("download", "")
    paginate = navigate.pop("paginate", None)
    return Profile(
        name="test",
        extract=Extract(strategy="anchors"),
        navigate=Navigate(**navigate),
        paginate=paginate or Paginate(),
        download=Download(url=download),
    )


def at(prof: Profile, url: str, path=None) -> Location:
    request = PageRequest(url=url, path=path) if path is not None else PageRequest.get(url)
    return Location.of(prof, url, request)


# ---------------------------------------------------------------- kind = href


def test_a_link_is_the_address():
    location = at(profile(kind="href"), "http://h.onion/files/")
    entry = resolve(location, RawEntry(name="archive", is_dir=True,
                                       href="http://h.onion/files/archive/"))
    assert entry.url == "http://h.onion/files/archive/"
    assert entry.listing_request().method == "GET"


def test_a_name_without_a_link_is_resolved_against_the_page():
    """A JSON autoindex states names and no links; the name is the path from the page."""
    location = at(profile(kind="href"), "http://h.onion/files/")
    directory = resolve(location, RawEntry(name="archive", is_dir=True))
    a_file = resolve(location, RawEntry(name="dump.sql.gz", is_dir=False, size_bytes=10))

    assert directory.url == "http://h.onion/files/archive/", "a directory keeps its slash"
    assert a_file.url == "http://h.onion/files/dump.sql.gz"


def test_a_name_that_needs_escaping_gets_it():
    location = at(profile(kind="href"), "http://h.onion/files/")
    entry = resolve(location, RawEntry(name="Q1 2019 report.txt", is_dir=False, size_bytes=1))
    assert entry.url == "http://h.onion/files/Q1%202019%20report.txt"


# ---------------------------------------------------------------- kind = query


def test_a_query_parameter_carries_the_path():
    location = at(profile(kind="query", param="p"), "http://h.onion/index.php?p=files")
    entry = resolve(location, RawEntry(name="archive", is_dir=True))
    assert entry.url == "http://h.onion/index.php?p=files%2Farchive"
    assert entry.listing_request().path == "files/archive"


def test_the_other_query_parameters_survive():
    """A manager that needs `?token=...&p=dir` loses its session if the token is dropped."""
    location = at(profile(kind="query", param="p"),
                  "http://h.onion/index.php?token=abc123&p=files")
    entry = resolve(location, RawEntry(name="archive", is_dir=True))
    assert "token=abc123" in entry.url
    assert "p=files%2Farchive" in entry.url


def test_a_row_addressing_this_directory_or_its_parent_is_not_an_entry():
    """The `..` row of a file manager, spelled as a path rather than as a link.

    On a path-addressed server the structural scope test removes these; when the path is a
    query parameter, nothing else can.
    """
    location = at(profile(kind="query", param="p"), "http://h.onion/i.php?p=files/archive")
    assert resolve(location, RawEntry(name="..", is_dir=True, path="files")) is None
    assert resolve(location, RawEntry(name="self", is_dir=True, path="files/archive")) is None
    assert resolve(location, RawEntry(name="deeper", is_dir=True,
                                      path="files/archive/deeper")) is not None


def test_a_download_template_sends_the_bytes_somewhere_else():
    location = at(profile(kind="query", param="p", download="{root}?p={path}&dl={name}"),
                  "http://h.onion/index.php?p=files")
    entry = resolve(location, RawEntry(name="dump.sql.gz", is_dir=False, size_bytes=10))

    assert entry.url == "http://h.onion/index.php?p=files%2Fdump.sql.gz"
    assert entry.download_url == "http://h.onion/index.php?p=files/dump.sql.gz&dl=dump.sql.gz"
    assert entry.fetch_url == entry.download_url, "urls.txt must carry what --download fetches"


# ---------------------------------------------------------------- kind = encoded


def test_an_encoded_segment_folds_the_whole_path():
    """The shape that returned 418 files out of 86,992 when read as raw path text."""
    location = at(profile(kind="encoded", prefix="r/fm/TOK"),
                  "http://h.onion/r/fm/TOK/dumps%2Fraw")
    entry = resolve(location, RawEntry(name="nested", is_dir=True))

    assert entry.url == "http://h.onion/r/fm/TOK/dumps%2Fraw%2Fnested"
    assert entry.listing_request().path == "dumps/raw/nested"


def test_an_encoded_target_reads_its_own_position_from_the_url():
    location = at(profile(kind="encoded", prefix="r/fm/TOK"),
                  "http://h.onion/r/fm/TOK/dumps%2Fraw")
    assert location.path == "dumps/raw"
    assert location.root == "http://h.onion/r/fm/TOK"


# ---------------------------------------------------------------- kind = api


API = dict(kind="api", method="POST", url="{origin}/api/fs/list",
           body='{"path": "/{path}", "page": 1}')


def test_an_api_child_is_a_request_not_a_url():
    prof = profile(**API)
    location = at(prof, "http://h.onion/api/fs/list", path="dumps")
    entry = resolve(location, RawEntry(name="raw", is_dir=True))

    request = entry.listing_request()
    assert request.method == "POST"
    assert request.url == "http://h.onion/api/fs/list"
    assert request.body == '{"path": "/dumps/raw", "page": 1}'
    assert request.path == "dumps/raw"


def test_a_json_body_survives_substitution():
    """The reason placeholders are not str.format: a body is JSON, and JSON is braces."""
    prof = profile(kind="api", method="POST", url="{origin}/api",
                   body='{"path": "{path}", "opts": {"deep": true}}')
    location = at(prof, "http://h.onion/api", path="")
    entry = resolve(location, RawEntry(name="dumps", is_dir=True))
    assert entry.listing_request().body == '{"path": "dumps", "opts": {"deep": true}}'


def test_a_quote_in_a_name_cannot_break_out_of_the_body():
    prof = profile(**API)
    location = at(prof, "http://h.onion/api/fs/list", path="")
    entry = resolve(location, RawEntry(name='we"ird', is_dir=True))
    assert entry.listing_request().body == '{"path": "/we\\"ird", "page": 1}'


def test_the_seed_of_an_api_target_is_asked_for_in_its_own_scheme():
    """Without this an API profile could never start: the seed would be a GET of a shell."""
    request = seed_request(profile(**API), "http://h.onion/pub/dumps/")
    assert request.method == "POST"
    assert request.url == "http://h.onion/api/fs/list"
    assert request.body == '{"path": "/pub/dumps", "page": 1}'


def test_the_seed_of_an_ordinary_target_stays_a_plain_get():
    request = seed_request(profile(kind="href"), "http://h.onion/files/")
    assert request.method == "GET" and request.url == "http://h.onion/files/"
    assert request.identity == "", "a plain GET must not disturb frontier deduplication"


# ---------------------------------------------------------------- pagination


def test_a_paged_directory_asks_for_the_next_page_at_the_same_depth():
    prof = profile(kind="query", param="p",
                   paginate=Paginate(kind="query", param="page", start=1))
    location = at(prof, "http://h.onion/i.php?p=files&page=1")
    result = ExtractResult(entries=[RawEntry(name="a", is_dir=False)])

    following = next_page(location, result, page_index=0)
    assert following is not None and "page=2" in following.url
    assert "p=files" in following.url, "the directory must not be lost between pages"


def test_paging_stops_when_a_page_comes_back_empty():
    prof = profile(kind="query", param="p", paginate=Paginate(kind="query", param="page"))
    location = at(prof, "http://h.onion/i.php?p=files&page=9")
    assert next_page(location, ExtractResult(entries=[]), page_index=8) is None


def test_paging_stops_at_the_cap():
    prof = profile(kind="query", param="p",
                   paginate=Paginate(kind="query", param="page", max_pages=3))
    location = at(prof, "http://h.onion/i.php?p=files&page=4")
    result = ExtractResult(entries=[RawEntry(name="a", is_dir=False)])
    assert next_page(location, result, page_index=3) is None


def test_a_cursor_is_followed_only_while_the_server_hands_one_out():
    prof = profile(kind="href", paginate=Paginate(kind="cursor", param="continuation-token"))
    location = at(prof, "http://h.onion/?list-type=2")

    with_cursor = next_page(location, ExtractResult(cursor="abc123"), page_index=0)
    assert with_cursor is not None and "continuation-token=abc123" in with_cursor.url
    assert next_page(location, ExtractResult(cursor=None), page_index=1) is None


def test_a_target_without_pagination_never_queues_a_second_page():
    location = at(profile(kind="href"), "http://h.onion/files/")
    result = ExtractResult(entries=[RawEntry(name="a", is_dir=False)], cursor="x")
    assert next_page(location, result, page_index=0) is None


# ---------------------------------------------------------------- inference


@pytest.mark.parametrize("raw,expected", [
    (RawEntry(name="archive", href="http://h.onion/f/archive/"), True),
    (RawEntry(name="archive"), True),                       # no extension, no size
    (RawEntry(name="notes.txt"), False),                    # an extension settles it
    (RawEntry(name="archive", size_bytes=4096), False),     # a size settles it
    (RawEntry(name="archive", is_dir=True, size_bytes=4096), True),   # stated wins
])
def test_kind_is_inferred_only_when_the_page_did_not_say(raw, expected):
    location = at(profile(kind="href"), "http://h.onion/f/")
    assert resolve(location, raw).is_dir is expected
