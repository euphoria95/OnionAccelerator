"""Tests for the listing templates themselves.

Two claims are being pinned here, and they are the claims the whole template mechanism
rests on.

The first is that every shipped template *loads*: a typo in a TOML file is a rule that
silently stopped applying, and the crawl that produces looks exactly like a target with
nothing in it. So loading is strict, and this suite proves the strictness is real by
feeding the loader bad templates and requiring it to complain about the right file.

The second is that a template marked `verified` is verified. That word appears in
`--list-profiles` and in `--detect`'s output, and an operator decides whether to trust a
profile on the strength of it -- so every `verified` template must have a fixture here
that it reads correctly. A template with no fixture must say `unverified`, and this suite
fails if one lies.
"""

import os

import pytest

from crawler.config import INDEX_CONFIDENCE_THRESHOLD
from crawler.listing import ListingEngine, Page
from crawler.listing.profile import TemplateError
from crawler.listing.navigate import seed_request
from crawler.listing.registry import BUILTIN_DIR, FALLBACK, load_file, load_profiles

HERE = os.path.dirname(os.path.abspath(__file__))
LISTINGS = os.path.join(HERE, "fixtures", "listings")

PROFILES = load_profiles()
BY_NAME = {p.name: p for p in PROFILES}


def load(name: str) -> str:
    with open(os.path.join(LISTINGS, name), encoding="utf-8") as fh:
        return fh.read()


def engine(**kwargs) -> ListingEngine:
    return ListingEngine(PROFILES, **kwargs)


def read(profile: str, fixture: str, url: str, seed: str = "", **page_kwargs):
    """Read one fixture with one named profile, bypassing detection.

    `seed` matters only for API profiles, and it is what the crawler itself does: an API
    answer's URL is the endpoint, the same for every directory, so the request is the only
    thing that records which directory was asked for. Building the page without one would
    be testing a situation the crawler never produces.
    """
    request = seed_request(BY_NAME[profile], seed or url)
    page = Page(url=url, request=request, body=load(fixture), **page_kwargs)
    return engine().parse(page, profile=BY_NAME[profile])


# ---------------------------------------------------------------- the templates load


def test_every_builtin_template_loads():
    """A shipped template that does not parse is a rule that stopped applying."""
    files = [f for f in os.listdir(BUILTIN_DIR) if f.endswith(".toml")]
    assert len(PROFILES) == len(files), "a template file was skipped by the loader"
    assert FALLBACK in BY_NAME, "the structural fallback must always be present"


def test_names_and_priorities_are_sane():
    assert len({p.name for p in PROFILES}) == len(PROFILES), "duplicate template name"
    assert BY_NAME[FALLBACK].priority == 0, "the fallback must lose every tie"
    assert all(p.priority > 0 for p in PROFILES if p.name != FALLBACK)


@pytest.mark.parametrize("bad,message", [
    ('name = "x"\n[extract]\nstrategy = "anchors"\n[match]\nnope = 1\n', "unknown key"),
    ('name = "x"\n[extract]\nstrategy = "nosuch"\n', "unknown [extract].strategy"),
    ('name = "x"\n[extract]\nstrategy = "rows"\nrowz = "tr"\n', "not taken by"),
    ('name = "x"\n[extract]\nstrategy = "anchors"\n[navigate]\nkind = "telepathy"\n',
     "[navigate].kind"),
    ('name = "x"\n[extract]\nstrategy = "anchors"\n[match]\ntitle_regex = "(unclosed"\n',
     "not a valid regex"),
    ('[extract]\nstrategy = "anchors"\n', "'name' is required"),
    ('name = "x"\n', "[extract] section is required"),
])
def test_a_broken_template_is_rejected_by_name(tmp_path, bad, message):
    """The error has to name the file. A template nobody can find is a template nobody fixes."""
    path = tmp_path / "broken.toml"
    path.write_text(bad, encoding="utf-8")
    with pytest.raises(TemplateError) as exc:
        load_file(str(path))
    assert "broken.toml" in str(exc.value)
    assert message in str(exc.value)


def test_a_user_template_directory_extends_the_builtins(tmp_path):
    (tmp_path / "mine.toml").write_text(
        'name = "mine"\ntitle = "t"\npriority = 99\n[extract]\nstrategy = "anchors"\n',
        encoding="utf-8")
    profiles = load_profiles([str(tmp_path)])
    assert profiles[0].name == "mine", "a higher priority must sort first"
    assert FALLBACK in {p.name for p in profiles}, "built-ins are still there"


def test_a_user_template_can_replace_a_builtin(tmp_path):
    """Fixing a shipped profile mid-engagement must not mean editing the repository."""
    (tmp_path / "apache.toml").write_text(
        'name = "apache-autoindex"\ntitle = "mine"\n[extract]\nstrategy = "anchors"\n',
        encoding="utf-8")
    profiles = {p.name: p for p in load_profiles([str(tmp_path)])}
    assert profiles["apache-autoindex"].title == "mine"


# ---------------------------------------------------------------- verified means verified


VERIFIED_FIXTURES = {
    "generic-structural": "custom_template.html",
    "apache-autoindex": "apache_table.html",
    "nginx-autoindex": "nginx.html",
    "lighttpd-dirlisting": "lighttpd.html",
    "nginx-json": "nginx_autoindex.json",
    "tiny-file-manager": "tiny_file_manager.html",
    "laravel-encoded-segment": "filemanager_encoded.html",
    "tree-dump": "tree_dump.txt",
    "ls-lr-dump": "ls_lr.txt",
    "find-dump": "find_list.txt",
    "sitemap-xml": "sitemap.xml",
}


def test_every_verified_template_has_a_fixture():
    """`verified` is a claim shown to operators; it has to be backed by a test."""
    verified = {p.name for p in PROFILES if p.verified}
    assert verified == set(VERIFIED_FIXTURES), (
        "a template's status disagrees with the fixtures: "
        f"claimed-but-untested={sorted(verified - set(VERIFIED_FIXTURES))}, "
        f"tested-but-unclaimed={sorted(set(VERIFIED_FIXTURES) - verified)}"
    )


@pytest.mark.parametrize("name", sorted(VERIFIED_FIXTURES))
def test_a_verified_template_reads_its_fixture(name):
    """Every verified profile finds entries, and none of them are junk."""
    url = {
        "nginx-json": "http://examplexyz.onion/files/",
        "tiny-file-manager": "http://examplexyz.onion/index.php?p=files",
        "laravel-encoded-segment": "http://examplexyz.onion/r/filemanager/TOK/dumps%2Fraw",
    }.get(name, "http://examplexyz.onion/files/")
    content_type = {
        "nginx-json": "application/json",
        "sitemap.xml": "application/xml",
    }.get(name, "text/html")

    listing = read(name, VERIFIED_FIXTURES[name], url, content_type=content_type)

    assert listing.is_index, f"{name} refused its own fixture"
    assert listing.total > 0, f"{name} found nothing in its own fixture"
    assert listing.profile == name
    for entry in listing.entries:
        assert entry.name, "an entry with no name"
        assert entry.url.startswith("http"), f"unresolved URL: {entry.url}"
        assert entry.name.strip(".").lower() not in ("", "parent directory", "..")


# ---------------------------------------------------------------- per-family behaviour


def test_tiny_file_manager_addresses_children_by_query_parameter():
    """The case the structural reader cannot do: every row links to the page itself.

    Read structurally, `?p=files/archive` differs from the page only by its query, which
    is the signature of a sort control -- so the whole listing is discarded and the
    directory looks empty. The template says the path is in `?p=`, and the same rows
    become a tree.
    """
    url = "http://examplexyz.onion/index.php?p=files"
    listing = read("tiny-file-manager", "tiny_file_manager.html", url)

    assert {e.name for e in listing.directories} == {"archive", "incoming"}
    assert {e.name for e in listing.files} == {"dump.sql.gz", "README.txt"}
    assert all("p=files%2F" in e.url or "p=files/" in e.url for e in listing.directories)

    # The `..` row addresses the parent, and a parent is not an entry.
    assert not any(e.name == ".." for e in listing.entries)

    # Sizes still come from the row, wherever the column happens to be.
    sizes = {e.name: e.size_bytes for e in listing.files}
    assert sizes["dump.sql.gz"] == 512 * 1024 * 1024
    assert sizes["README.txt"] == int(4.1 * 1024)

    # A file's bytes live at a different URL than its listing row: `p` is the *directory*
    # it was listed in and `dl` is its name. Passing the file's own path as `p` asks for a
    # directory that does not exist, and every URL in the manifest comes back an error.
    dump = next(e for e in listing.files if e.name == "dump.sql.gz")
    assert dump.download_url == (
        "http://examplexyz.onion/index.php?p=files&dl=dump.sql.gz")
    assert dump.fetch_url == dump.download_url

    # Structurally, the same page is empty -- which is the point.
    assert engine().parse(
        Page(url=url, body=load("tiny_file_manager.html"), content_type="text/html"),
        profile=BY_NAME[FALLBACK],
    ).total == 0


def test_the_encoded_separator_file_manager_still_reads_structurally():
    """The live target's shape: full hrefs, with the parent path folded into one segment."""
    listing = read("laravel-encoded-segment", "filemanager_encoded.html",
                   "http://examplexyz.onion/r/filemanager/TOK/dumps%2Fraw")

    assert {e.name for e in listing.directories} == {"archive", "incoming"}
    assert {e.name for e in listing.files} == {"dump.sql.gz", "README.txt"}
    # The spelling the server published is the spelling that goes back on the wire.
    assert all("dumps%2Fraw" in e.url for e in listing.entries)


def test_a_json_api_answer_reads_without_any_markup():
    """AList's POST answer, read by the same JSON strategy nginx's autoindex uses."""
    listing = read("alist", "alist_api.json", "http://examplexyz.onion/api/fs/list",
                   seed="http://examplexyz.onion/", content_type="application/json")

    assert {e.name for e in listing.directories} == {"archive", "incoming"}
    assert {e.name: e.size_bytes for e in listing.files} == {
        "dump.sql.gz": 536870912, "README.txt": 4198,
    }
    # Every directory is the same endpoint with a different body -- the case the engine
    # learned non-GET requests for.
    for entry in listing.directories:
        request = entry.listing_request()
        assert request.method == "POST"
        assert request.url.endswith("/api/fs/list")
        assert f'"/{entry.name}"' in (request.body or "")
    # And a file's bytes are somewhere else entirely.
    assert all(e.download_url and "/d/" in e.download_url for e in listing.files)


def test_a_webdav_multistatus_reads_as_a_listing():
    listing = read("webdav-propfind", "webdav_propfind.xml",
                   "http://examplexyz.onion/files/", content_type="application/xml")

    names = {e.name for e in listing.entries}
    assert "archive" in names and "dump.sql.gz" in names
    # The collection itself is in its own answer, and is not one of its own children.
    assert sum(1 for e in listing.entries if e.name == "files") == 0
    dump = next(e for e in listing.files if e.name == "dump.sql.gz")
    assert dump.size_bytes == 536870912


def test_a_bucket_listing_pages_on_its_continuation_token():
    listing = read("s3-bucket-xml", "s3_bucket.xml",
                   "http://examplexyz.onion/?list-type=2&prefix=files/",
                   content_type="application/xml")

    assert {e.name for e in listing.directories} == {"archive", "incoming"}
    assert {e.name for e in listing.files} == {"dump.sql.gz", "README.txt"}
    # A bucket answers 1000 keys at a time; without following the token the rest is
    # silence, so the next page has to be queued from this one.
    assert listing.more, "a truncated bucket listing queued no continuation"
    assert "1ueGcxLPRx1Tr" in listing.more[0].url


# ---------------------------------------------------------------- the guard and the match


# A themed autoindex holding one file: the title is the directory name rather than
# "Index of", there is no server footer, no sort controls and no up-link, and a search box
# costs it the last point. Everything the index-confidence heuristic counts is absent --
# which is what a real directory holding a single entry looks like, and why the heuristic
# refuses it. The theme's own name is the only thing identifying the software.
_ONE_FILE = (
    "<html><head><title>archive</title>"
    '<link rel="stylesheet" href="/theme/apaxy/style.css"></head><body>'
    '<form action="/search"><input name="q"></form>'
    "<table><tr><td><a href=\"old.tar\">old.tar</a></td><td>512</td></tr></table>"
    "</body></html>"
)

# The same server answering with an application at one of its paths. The theme's name is
# not on this page, so the template does not match it -- which is the only thing separating
# a directory walk from a crawl of somebody's forum.
_APPLICATION = (
    "<html><head><title>Welcome</title></head><body>"
    '<form action="/search"><input name="q"></form>'
    '<a href="/rules">Rules</a><a href="/faq">FAQ</a><a href="/login">Log in</a>'
    '<a href="topic-1">First topic</a><a href="topic-2">Second topic</a>'
    "</body></html>"
)


def _page(body: str, url: str) -> Page:
    return Page(url=url, body=body, content_type="text/html", status=200)


def test_a_matched_template_expands_a_directory_holding_one_file():
    """The template's match is the evidence; the heuristic is not asked again.

    A directory with a single file scores below the index-confidence threshold, and being
    refused makes it a leaf -- silently pruning whatever is under it. That false negative
    is the reason the plain-autoindex templates exist at all, so a page one of them
    matches must be expanded on the strength of the match.
    """
    page = _page(_ONE_FILE, "http://h.onion/files/archive/")
    listing = engine().parse(page, profile=BY_NAME["fancyindex-theme"])

    assert listing.confidence < INDEX_CONFIDENCE_THRESHOLD, "the fixture must be a hard one"
    assert listing.is_index, f"refused its own target at confidence {listing.confidence}"
    assert [e.name for e in listing.files] == ["old.tar"]


def test_the_structural_reader_still_refuses_the_same_page():
    """With no template there is nothing to trust, so the guard stays on."""
    page = _page(_ONE_FILE, "http://h.onion/files/archive/")
    assert not engine().parse(page, profile=BY_NAME[FALLBACK]).is_index


def test_a_locked_profile_does_not_vouch_for_every_page_on_the_host():
    """The lock says which template reads the host, not that every page is a listing.

    A server that autoindexes most of its paths and answers with an application at one of
    them is common. Waiving the guard for the whole host on the strength of the root page
    would walk straight into it, so the match rules are re-checked per page.
    """
    shared = engine()
    root = _page(_ONE_FILE, "http://h.onion/")
    assert shared.profile_for(root).name == "fancyindex-theme", "the host should lock"

    app = _page(_APPLICATION, "http://h.onion/a/")
    assert shared.profile_for(app).name == "fancyindex-theme", "the lock should hold"
    assert not shared.parse(app).is_index, "but the application must still be refused"
