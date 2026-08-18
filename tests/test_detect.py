"""Tests for template selection: which profile a page gets read with, and why.

Selection is where a wrong answer is most expensive. Pick nothing when something fits and
the crawl silently reads a file manager as an empty page; pick a template when nothing
fits and the crawl walks into an application. So two properties are pinned here:

  * every fixture ranks its own profile first -- the detector agrees with the templates;
  * a page that is not a listing ranks nothing usable, whatever matched its markup.

The second is the one that matters most: `not_an_index.html` is a forum, and a forum has
hundreds of in-scope links too.
"""

import os

import pytest

from crawler.listing import ListingEngine, Page, rank, render
from crawler.listing.registry import FALLBACK, load_profiles

HERE = os.path.dirname(os.path.abspath(__file__))
LISTINGS = os.path.join(HERE, "fixtures", "listings")

PROFILES = load_profiles()


def load(name: str) -> str:
    with open(os.path.join(LISTINGS, name), encoding="utf-8") as fh:
        return fh.read()


def page(fixture: str, url: str, content_type: str = "text/html", **headers) -> Page:
    return Page(url=url, body=load(fixture), content_type=content_type, headers=headers)


def engine() -> ListingEngine:
    return ListingEngine(PROFILES)


# ---------------------------------------------------------------- the fixtures rank


@pytest.mark.parametrize("fixture,url,content_type,headers,expected", [
    ("apache_table.html", "http://h.onion/files/", "text/html", {}, "apache-autoindex"),
    ("apache_pre.html", "http://h.onion/files/", "text/html", {}, "apache-autoindex"),
    ("nginx.html", "http://h.onion/files/", "text/html",
     {"Server": "nginx/1.24.0"}, "nginx-autoindex"),
    ("lighttpd.html", "http://h.onion/files/", "text/html", {}, "lighttpd-dirlisting"),
    ("nginx_autoindex.json", "http://h.onion/files/", "application/json", {}, "nginx-json"),
    ("tiny_file_manager.html", "http://h.onion/index.php?p=files", "text/html",
     {}, "tiny-file-manager"),
    ("tree_dump.txt", "http://h.onion/List_of_files.txt", "text/plain", {}, "tree-dump"),
    ("ls_lr.txt", "http://h.onion/listing.txt", "text/plain", {}, "ls-lr-dump"),
    ("sitemap.xml", "http://h.onion/sitemap.xml", "application/xml", {}, "sitemap-xml"),
    ("s3_bucket.xml", "http://h.onion/?list-type=2", "application/xml", {}, "s3-bucket-xml"),
])
def test_a_fixture_ranks_its_own_profile_first(fixture, url, content_type, headers, expected):
    findings = rank(engine(), page(fixture, url, content_type, **headers))
    assert findings, f"{fixture} matched nothing at all"
    assert findings[0].profile.name == expected, \
        f"{fixture} ranked {[f.profile.name for f in findings[:3]]}"
    assert findings[0].usable, f"{expected} matched {fixture} but read nothing out of it"


def test_a_page_with_no_template_still_reads_structurally():
    """The fallback is not a failure: a custom autoindex nobody wrote a template for."""
    findings = rank(engine(), page("custom_template.html", "http://h.onion/files/sub/"))
    best = findings[0]
    assert best.profile.name == FALLBACK
    assert best.usable and best.directories == 2 and best.files == 2


def test_an_application_ranks_nothing_usable():
    """The guard that keeps a directory walk from becoming a crawl of somebody's forum."""
    findings = rank(engine(), page("not_an_index.html", "http://h.onion/files/sub/"))
    assert not any(f.usable for f in findings), \
        f"a forum was read as a listing by {[f.profile.name for f in findings if f.usable]}"


def test_selection_is_locked_per_host():
    """The first page decides, and the rest of that host's crawl keeps the decision.

    A manager's deeper pages are usually less distinctive than its root; re-matching every
    page would let a crawl drift onto a different profile halfway down and start
    addressing children a different way.
    """
    driver = engine()
    first = driver.profile_for(page("tiny_file_manager.html", "http://h.onion/index.php?p=files"))
    assert first.name == "tiny-file-manager"

    # A page from the same host that matches nothing keeps the locked profile.
    second = driver.profile_for(page("custom_template.html", "http://h.onion/index.php?p=x"))
    assert second.name == "tiny-file-manager"

    # A different host decides for itself.
    other = driver.profile_for(page("apache_table.html", "http://other.onion/files/"))
    assert other.name == "apache-autoindex"


def test_a_forced_profile_skips_detection_entirely():
    driver = ListingEngine(PROFILES, forced="alist")
    assert driver.profile_for(page("apache_table.html", "http://h.onion/files/")).name == "alist"


def test_an_unknown_forced_profile_is_refused_with_the_list():
    with pytest.raises(ValueError) as exc:
        ListingEngine(PROFILES, forced="nosuchthing")
    assert "nosuchthing" in str(exc.value)
    assert FALLBACK in str(exc.value), "the error should say what is available"


# ---------------------------------------------------------------- the report


def test_the_report_names_the_command_to_run_next():
    """--detect's whole point: end with the line the operator types."""
    seed = "http://h.onion/index.php?p=files"
    findings = rank(engine(), page("tiny_file_manager.html", seed))
    text = render(findings, seed)

    assert "tiny-file-manager" in text
    assert "--profile tiny-file-manager" in text
    assert seed in text


def test_the_report_says_when_the_structural_reader_is_enough():
    seed = "http://h.onion/files/sub/"
    text = render(rank(engine(), page("custom_template.html", seed)), seed)
    assert "no --profile needed" in text


def test_the_report_admits_when_nothing_read_the_page():
    seed = "http://h.onion/files/sub/"
    text = render(rank(engine(), page("not_an_index.html", seed)), seed)
    assert "No profile read this page as a listing" in text


def test_the_report_flags_an_unverified_template():
    """An unverified template is a guess written from documentation; say so."""
    seed = "http://h.onion/?list-type=2"
    text = render(rank(engine(), page("s3_bucket.xml", seed, "application/xml")), seed)
    assert "unverified" in text
