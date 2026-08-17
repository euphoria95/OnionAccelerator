"""Tests for the URL -> local path mapping that `--download` writes through.

Two things have to hold at once, and they pull in opposite directions: the mirror under
`downloads/<host>/` has to reproduce the remote tree faithfully enough that a crawl of
thousands of same-named files stays sorted, and no URL a hostile onion can publish may
write outside `downloads/`.
"""

import os

import OnionAccelerator as oa

BASE = "downloads"
HOST = "examplexyz.onion"


def local(url: str) -> str:
    return oa.url_to_local_path(url, BASE, preserve_path=True)


def test_the_remote_tree_is_mirrored():
    assert local(f"http://{HOST}/a/b/readme.txt") == os.path.join(
        BASE, HOST, "a", "b", "readme.txt")
    # Percent-encoding is spelling, not structure: %20 is part of a name.
    assert local(f"http://{HOST}/a/my%20docs/readme.txt") == os.path.join(
        BASE, HOST, "a", "my docs", "readme.txt")


def test_an_encoded_separator_still_nests():
    """A file manager folds a nested path into one segment. It is still nested.

    Split before decoding, `/TOK/dumps%2Fraw/part.bin` mirrors as a directory literally
    named "dumps_raw", sitting beside the "dumps" that its own siblings were written
    into -- the tree that --download exists to preserve, flattened one level at a time.
    """
    assert local(f"http://{HOST}/TOK/dumps%2Fraw/part.bin") == local(
        f"http://{HOST}/TOK/dumps/raw/part.bin")
    assert local(f"http://{HOST}/TOK/dumps%2Fraw/part.bin") == os.path.join(
        BASE, HOST, "TOK", "dumps", "raw", "part.bin")


def test_nothing_escapes_the_download_directory():
    """Including when the traversal is encoded -- which is what decoding first exposes.

    A `..` hidden inside `%2E%2E%2F` is invisible to a per-segment check until the
    segment is decoded; decoding before the split is what hands it to that check as a
    segment of its own instead of as part of an opaque name.
    """
    hostile = [
        "/../../etc/passwd",
        "/a/..%2F..%2Fetc/passwd",
        "/a/%2e%2e%2f%2e%2e%2fetc/passwd",
        "/a/..%5C..%5Cwindows",
        "/%2F%2F/etc/passwd",
    ]
    root = os.path.abspath(os.path.join(BASE, HOST))
    for path in hostile:
        resolved = os.path.abspath(local(f"http://{HOST}{path}"))
        assert resolved.startswith(root + os.sep), f"{path} escaped to {resolved}"
