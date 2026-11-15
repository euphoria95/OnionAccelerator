"""What changed on a target between two sightings of it.

The hard part of a diff is not finding what is new; it is not claiming a file was removed
when the second crawl simply never looked where it lived. A crawl that was cut short, or
failed on one directory, has said nothing about that directory's contents, so those files
are reported as *unknown*, never as removed.
"""

import pytest

from index import diff as diffmod
from index import store
from index.ingest import ingest_dir
from indexfixtures import HOST_A, HOST_B, write_crawl_dir


@pytest.fixture
def ix(tmp_path):
    with store.Index.open(str(tmp_path / "idx" / "index.db")) as index:
        yield index


def paths(entries):
    return sorted(e["path"] for e in entries)


def two_runs(ix, tmp_path, first, second, *, second_dirs=("/",), stopped="completed"):
    ingest_dir(ix, write_crawl_dir(tmp_path, "run1", files=first, at=0))
    ingest_dir(ix, write_crawl_dir(tmp_path, "run2", files=second, dirs=second_dirs, at=1000,
                                   stopped_because=stopped))


def test_added_removed_and_resized(ix, tmp_path):
    two_runs(ix, tmp_path,
             first=[("/keep.txt", 5), ("/gone.txt", 6), ("/grown.bin", 100)],
             second=[("/keep.txt", 5), ("/new.txt", 9), ("/grown.bin", 250)])
    result = diffmod.diff(ix, HOST_A)

    assert (result.since_job, result.until_job) == ("run1", "run2")
    assert paths(result.added) == ["/new.txt"]
    assert paths(result.removed) == ["/gone.txt"]
    assert [(e["path"], e["old_size"], e["size"]) for e in result.resized] == [
        ("/grown.bin", 100, 250)]
    assert result.unknown == [] and result.warnings == []


def test_a_re_uploaded_file_of_a_different_size_is_resized_not_added_and_removed(ix, tmp_path):
    two_runs(ix, tmp_path, first=[("/pub/report.pdf", 1000)], second=[("/pub/report.pdf", 1024)])
    result = diffmod.diff(ix, HOST_A)
    assert paths(result.added) == [] and paths(result.removed) == []
    assert [(e["path"], e["old_size"], e["size"]) for e in result.resized] == [
        ("/pub/report.pdf", 1000, 1024)]


def test_a_renamed_directory_is_its_old_files_removed_and_its_new_files_added(ix, tmp_path):
    """The index sees paths, not intent: it cannot know /old moved to /new.

    What it can do is report both halves of the move, directory entries included, so the
    analyst can see that the same names and sizes appear on both sides.
    """
    files = [("/old/a.txt", 11), ("/old/b.txt", 22)]
    ingest_dir(ix, write_crawl_dir(tmp_path, "run1", files=files, at=0))
    ingest_dir(ix, write_crawl_dir(
        tmp_path, "run2", files=[("/new/a.txt", 11), ("/new/b.txt", 22)], at=1000))
    result = diffmod.diff(ix, HOST_A)

    assert paths(result.removed) == ["/old", "/old/a.txt", "/old/b.txt"]
    assert paths(result.added) == ["/new", "/new/a.txt", "/new/b.txt"]
    assert result.resized == []
    removed = {(e["name"], e["size"]) for e in result.removed if e["kind"] == "file"}
    added = {(e["name"], e["size"]) for e in result.added if e["kind"] == "file"}
    assert removed == added, "same names and sizes on both sides is the tell for a move"


def test_a_directory_that_was_not_listed_again_makes_its_files_unknown_not_removed(ix, tmp_path):
    ingest_dir(ix, write_crawl_dir(tmp_path, "run1", files=[("/a/x.txt", 1), ("/b/y.txt", 2)], at=0))
    # Run 2 listed /a (and the root) but never got to /b, and says why it stopped.
    ingest_dir(ix, write_crawl_dir(tmp_path, "run2", files=[("/a/x.txt", 1)], dirs=("/", "/a"),
                                   at=1000, stopped_because="max_pages"))
    result = diffmod.diff(ix, HOST_A)

    assert paths(result.removed) == []
    assert paths(result.unknown) == ["/b", "/b/y.txt"]
    assert any("max_pages" in w for w in result.warnings)


def test_a_listed_directory_that_lost_a_file_is_removed_even_after_a_cut_short_crawl(ix, tmp_path):
    ingest_dir(ix, write_crawl_dir(tmp_path, "run1", files=[("/a/x.txt", 1), ("/a/z.txt", 3)], at=0))
    ingest_dir(ix, write_crawl_dir(tmp_path, "run2", files=[("/a/x.txt", 1)], dirs=("/", "/a"),
                                   at=1000, stopped_because="time_budget"))
    assert paths(diffmod.diff(ix, HOST_A).removed) == ["/a/z.txt"]


def test_an_entry_missing_for_one_run_and_back_in_the_next_is_added_again(ix, tmp_path):
    ingest_dir(ix, write_crawl_dir(tmp_path, "run1", files=[("/a.bin", 10)], at=0))
    ingest_dir(ix, write_crawl_dir(tmp_path, "run2", files=[("/b.bin", 1)], at=1000))
    ingest_dir(ix, write_crawl_dir(tmp_path, "run3", files=[("/a.bin", 10)], at=2000))

    last = diffmod.diff(ix, HOST_A)                       # run2 -> run3
    assert "/a.bin" in paths(last.added)
    span = diffmod.diff(ix, HOST_A, since="run1")         # run1 -> run3
    assert "/a.bin" not in paths(span.added) + paths(span.removed)


def test_since_names_the_older_sighting_and_until_the_newer(ix, tmp_path):
    ingest_dir(ix, write_crawl_dir(tmp_path, "run1", files=[("/a", 1)], at=0))
    ingest_dir(ix, write_crawl_dir(tmp_path, "run2", files=[("/a", 1), ("/b", 1)], at=1000))
    ingest_dir(ix, write_crawl_dir(tmp_path, "run3", files=[("/a", 1), ("/b", 1), ("/c", 1)],
                                   at=2000))
    assert paths(diffmod.diff(ix, HOST_A, since="run1").added) == ["/b", "/c"]
    assert paths(diffmod.diff(ix, HOST_A, since="run1", until="run2").added) == ["/b"]
    assert paths(diffmod.diff(ix, HOST_A).added) == ["/c"]


def test_runs_that_did_not_see_the_target_are_not_a_sighting_of_it(ix, tmp_path):
    ingest_dir(ix, write_crawl_dir(tmp_path, "run1", host=HOST_A, files=[("/a", 1)], at=0))
    ingest_dir(ix, write_crawl_dir(tmp_path, "other", host=HOST_B, files=[("/z", 1)], at=500))
    ingest_dir(ix, write_crawl_dir(tmp_path, "run2", host=HOST_A, files=[("/a", 1), ("/b", 1)],
                                   at=1000))
    result = diffmod.diff(ix, HOST_A)
    assert (result.since_job, result.until_job) == ("run1", "run2")
    assert paths(result.added) == ["/b"]


def test_one_sighting_has_nothing_to_diff_against(ix, tmp_path):
    ingest_dir(ix, write_crawl_dir(tmp_path, "run1", files=[("/a", 1)]))
    with pytest.raises(diffmod.DiffError, match="only one"):
        diffmod.diff(ix, HOST_A)


@pytest.mark.parametrize("kwargs, match", [
    ({"host": "nobody.test"}, "no target"),
    ({"host": HOST_A, "since": "ghost"}, "no run"),
    ({"host": HOST_B, "since": "run1"}, "did not see"),
])
def test_naming_something_that_is_not_there_says_so(ix, tmp_path, kwargs, match):
    ingest_dir(ix, write_crawl_dir(tmp_path, "run1", files=[("/a", 1)], at=0))
    ingest_dir(ix, write_crawl_dir(tmp_path, "run2", files=[("/a", 1)], at=1000))
    ingest_dir(ix, write_crawl_dir(tmp_path, "run9", host=HOST_B, files=[("/q", 1)], at=3000))
    ingest_dir(ix, write_crawl_dir(tmp_path, "run10", host=HOST_B, files=[("/q", 1)], at=4000))
    with pytest.raises(diffmod.DiffError, match=match):
        diffmod.diff(ix, **kwargs)


def test_archive_members_are_removed_only_when_the_archive_was_walked_again(ix, tmp_path):
    from index.ingest import ingest_ndjson
    from indexfixtures import tree_entries, write_ndjson
    url = f"http://{HOST_A}/d/backup.7z"
    ingest_ndjson(ix, write_ndjson(tmp_path / "t1.nd", tree_entries(
        [("a.txt", 1), ("b.txt", 2)])), archive_url=url, job_id="t1")
    ingest_ndjson(ix, write_ndjson(tmp_path / "t2.nd", tree_entries([("a.txt", 1)])),
                  archive_url=url, job_id="t2")
    result = diffmod.diff(ix, HOST_A)
    assert paths(result.removed) == ["/d/backup.7z!/b.txt"]
