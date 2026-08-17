"""Tests for what a download run is allowed to believe and allowed to overwrite.

Both behaviours here were found the same way: a 58-URL run over a leaked-fileshare
onion reported 51 successes and left 42 files on disk, six of which were HTML error
pages carrying CAD extensions. Nothing in the log said so. The two causes are
independent -- one is trusting the HTTP status line, the other is two URLs sharing a
local path -- and each is silent on its own, which is why both are pinned down here.
"""

import os

import OnionAccelerator as oa


class FakeResponse:
    """The parts of requests.Response that _check_response() reads."""

    def __init__(self, status_code: int, content_type: str = "") -> None:
        self.status_code = status_code
        self.headers = {"content-type": content_type} if content_type else {}

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    def raise_for_status(self) -> None:
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")


# ---------------------------------------------------------------- error pages

def test_markup_answering_a_binary_target_is_an_error_page():
    """The silent corruption: 200 OK, 9.6 KB of HTML, saved as a 166 MB CAD model."""
    reason = oa._error_page_reason("http://h.onion/x/Auftragslayout.STEP",
                                   "text/html; charset=utf-8")
    assert reason is not None
    assert ".step" in reason


def test_a_bad_status_does_not_condemn_a_believable_body():
    """The mirror image: 404, but the body is the file, at the advertised length.

    This file manager answers 404 while sending a complete 16-byte note. Discarding
    it cost a real file and twenty retries against a deterministic condition.
    """
    assert oa._error_page_reason("http://h.onion/x/Verweis.txt",
                                 "text/plain; charset=UTF-8") is None
    assert oa._error_page_reason("http://h.onion/x/archive.zip",
                                 "application/zip") is None


def test_markup_is_allowed_where_markup_is_plausible():
    """The check may only reject a body that contradicts the name it was asked for."""
    for path in ("index.html", "page.php", "listing"):
        assert oa._error_page_reason(f"http://h.onion/{path}", "text/html") is None


def test_check_response_keeps_a_mislabelled_body_and_rejects_an_error_page():
    # 404 + a plausible content type: kept, because the body is the file.
    oa._check_response("http://h.onion/x/Verweis.txt", FakeResponse(404, "text/plain"))
    # 200 + markup for a binary target: rejected, because the body is not.
    try:
        oa._check_response("http://h.onion/x/model.STEP", FakeResponse(200, "text/html"))
    except RuntimeError:
        pass
    else:
        raise AssertionError("an HTML error page was accepted as a .STEP file")


def test_a_bad_status_with_nothing_to_identify_the_body_still_fails():
    try:
        oa._check_response("http://h.onion/x/thing.zip", FakeResponse(503))
    except RuntimeError:
        pass
    else:
        raise AssertionError("503 with no content type was accepted")


# ---------------------------------------------------------------- collisions

def test_flat_layout_collisions_are_counted(caplog):
    """Three Thumbs.db from three directories are three lost files, not one warning."""
    urls = [f"http://h.onion/a/v{i}/Thumbs.db" for i in (1, 2, 3)]
    lost = oa._warn_on_path_collisions(urls, preserve_path=False)
    assert lost == 2


def test_preserving_the_path_removes_the_collision():
    urls = [f"http://h.onion/a/v{i}/Thumbs.db" for i in (1, 2, 3)]
    assert oa._warn_on_path_collisions(urls, preserve_path=True) == 0


def test_distinct_basenames_never_collide():
    urls = ["http://h.onion/a/one.txt", "http://h.onion/b/two.txt"]
    assert oa._warn_on_path_collisions(urls, preserve_path=False) == 0


# ---------------------------------------------------------------- clobbering

def test_a_failed_download_leaves_an_existing_file_alone(tmp_path, monkeypatch):
    """A timeout on one URL must not delete a different URL's finished download.

    Under the flat layout `.../v1/Thumbs.db` and `.../v3/Thumbs.db` are one path, so
    the old unconditional `os.remove(out_path)` in the failure handler destroyed
    completed work whenever a same-named sibling failed later.
    """
    monkeypatch.setattr(oa, "DOWNLOAD_DIR", str(tmp_path))
    url = "http://h.onion/a/v3/Thumbs.db"
    out_path = oa.url_to_local_path(url, str(tmp_path), preserve_path=False)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "wb") as fh:
        fh.write(b"the good file that arrived earlier")

    def explode(*a, **kw):
        raise OSError("connection reset mid-body")

    monkeypatch.setattr(oa.requests, "get", explode)
    assert oa.download_file(url, "127.0.0.1:9050", ["ua"]) is False

    with open(out_path, "rb") as fh:
        assert fh.read() == b"the good file that arrived earlier"


def test_no_partial_files_are_left_behind(tmp_path, monkeypatch):
    monkeypatch.setattr(oa, "DOWNLOAD_DIR", str(tmp_path))

    def explode(*a, **kw):
        raise OSError("connection reset")

    monkeypatch.setattr(oa.requests, "get", explode)
    oa.download_file("http://h.onion/a/thing.zip", "127.0.0.1:9050", ["ua"])

    leftovers = [f for _, _, files in os.walk(tmp_path) for f in files]
    assert leftovers == []
