"""Reading crawl and tree output into the index, offline and idempotently."""

import json
import os

import pytest

from index import store
from index.ingest import Ingester, ingest_dir, ingest_ndjson
from indexfixtures import (HOST_A, HOST_B, crawl_events, dump, file_record, tree_entries,
                           write_crawl_dir, write_ndjson)


@pytest.fixture
def ix(tmp_path):
    with store.Index.open(str(tmp_path / "idx" / "index.db")) as index:
        yield index


def rows(ix, sql, *args):
    return ix.conn.execute(sql, args).fetchall()


FILES = [("/pub/readme.txt", 120), ("/pub/data/report.xlsx", 5000, "2025-03-04 10:00"),
         ("/top.bin", None)]


# ----------------------------------------------------------------------- one run


def test_a_crawl_directory_becomes_a_run_a_target_and_entries(ix, tmp_path):
    job = write_crawl_dir(tmp_path, "run1", files=FILES)
    summary = ingest_dir(ix, job)

    assert summary.job_id == "run1" and summary.entries_new == 6
    assert rows(ix, "SELECT job_id, mode, source, stopped_because FROM runs") == [
        ("run1", "crawl", "crawl", "completed")]
    assert rows(ix, "SELECT host, label, grp, victim FROM targets") == [
        (HOST_A, None, None, None)]
    by_path = {r[0]: r for r in rows(
        ix, "SELECT path, name, size, mtime_text, kind, source, fetch_url FROM entries")}
    assert set(by_path) == {"/", "/pub", "/pub/data", "/pub/readme.txt",
                            "/pub/data/report.xlsx", "/top.bin"}
    assert by_path["/pub/data/report.xlsx"][1:] == (
        "report.xlsx", 5000, "2025-03-04 10:00", "file", "crawl",
        f"http://{HOST_A}/pub/data/report.xlsx")
    assert by_path["/top.bin"][2] is None, "an unknown size stays unknown, not 0"
    assert by_path["/pub"][4] == "dir" and by_path["/pub"][2] is None


def test_a_download_url_that_differs_from_the_browse_url_is_what_fetch_uses(ix, tmp_path):
    job = write_crawl_dir(tmp_path, "run1", files=[], extra_files=[
        file_record(HOST_A, "/fm/view/a.zip", 10, download_url=f"http://{HOST_A}/fm/get?id=a.zip")])
    ingest_dir(ix, job)
    assert rows(ix, "SELECT fetch_url FROM entries WHERE path='/fm/view/a.zip'") == [
        (f"http://{HOST_A}/fm/get?id=a.zip",)]


def test_runs_are_timed_from_the_records_not_from_the_clock(ix, tmp_path):
    ingest_dir(ix, write_crawl_dir(tmp_path, "run1", files=FILES, at=0))
    started, finished = rows(ix, "SELECT started_at, finished_at FROM runs")[0]
    assert started == "2026-11-10T00:00:00Z" and finished > started


def test_a_directory_without_stats_json_is_still_ingested(ix, tmp_path):
    job = write_crawl_dir(tmp_path, "run1", files=FILES)
    os.remove(os.path.join(job, "stats.json"))
    ingest_dir(ix, job)
    assert rows(ix, "SELECT job_id, stopped_because FROM runs") == [("run1", None)]


def test_hostile_records_do_not_break_the_ingest(ix, tmp_path):
    """A listing can say anything; one bad line must not lose the other thousand."""
    job = write_crawl_dir(tmp_path, "run1", files=FILES)
    with open(os.path.join(job, "listing.jsonl"), "a", encoding="utf-8") as fh:
        fh.write("this is not json\n")
        fh.write(json.dumps({"url": 12, "path": None}) + "\n")
        fh.write(json.dumps(file_record(HOST_A, "/x" + "y" * 5000, 1)) + "\n")
        fh.write(json.dumps(file_record(HOST_A, "/ok.bin", -5)) + "\n")
        fh.write(json.dumps(file_record(HOST_A, "/nul\x00name", 1)) + "\n")
    summary = ingest_dir(ix, job)
    assert summary.skipped >= 2
    paths = {r[0] for r in rows(ix, "SELECT path FROM entries")}
    assert "/pub/readme.txt" in paths and "/ok.bin" in paths
    assert rows(ix, "SELECT size FROM entries WHERE path='/ok.bin'") == [(None,)], \
        "a negative size is not a size"


def test_a_missing_directory_is_an_error_not_an_empty_run(ix, tmp_path):
    with pytest.raises(FileNotFoundError):
        ingest_dir(ix, str(tmp_path / "nope"))
    assert rows(ix, "SELECT COUNT(*) FROM runs") == [(0,)]


# ----------------------------------------------------------------------- idempotent


def test_ingesting_the_same_run_twice_changes_nothing(ix, tmp_path):
    job = write_crawl_dir(tmp_path, "run1", files=FILES)
    ingest_dir(ix, job)
    first = dump(ix)
    again = ingest_dir(ix, job)
    assert dump(ix) == first
    assert again.entries_new == 0


def test_an_old_run_ingested_again_after_a_newer_one_changes_nothing(ix, tmp_path):
    """Idempotence must hold for run 1 even once run 2 has moved last_run past it."""
    old = write_crawl_dir(tmp_path, "run1", files=FILES, at=0)
    new = write_crawl_dir(tmp_path, "run2", files=FILES + [("/added.txt", 1)], at=1000)
    ingest_dir(ix, old)
    ingest_dir(ix, new)
    before = dump(ix)
    ingest_dir(ix, old)
    assert dump(ix) == before


def test_a_partly_ingested_run_is_completed_by_ingesting_it_again(ix, tmp_path):
    job = write_crawl_dir(tmp_path, "run1", files=FILES)
    ing = Ingester(ix)
    events = crawl_events(job)
    for kind, data in events[:3]:
        ing.apply("run1", kind, data)
    ing.flush()
    ingest_dir(ix, job)
    full = dump(ix)

    with store.Index.open(str(tmp_path / "other.db")) as other:
        ingest_dir(other, job)
        assert dump(other) == full


# ----------------------------------------------------------------------- across runs


def test_a_second_run_extends_what_it_saw_again_and_adds_what_is_new(ix, tmp_path):
    ingest_dir(ix, write_crawl_dir(tmp_path, "run1", files=FILES, at=0))
    ingest_dir(ix, write_crawl_dir(tmp_path, "run2", files=FILES + [("/new.txt", 7)], at=1000))
    runs = dict(rows(ix, "SELECT job_id, id FROM runs"))

    assert rows(ix, "SELECT first_run, last_run FROM entries WHERE path='/pub/readme.txt'") == [
        (runs["run1"], runs["run2"])]
    assert rows(ix, "SELECT first_run, last_run FROM entries WHERE path='/new.txt'") == [
        (runs["run2"], runs["run2"])]
    # Seen at the same size in consecutive runs: one segment, not two.
    assert rows(ix, "SELECT run_id, size, last_run FROM entry_versions "
                    "WHERE entry_id=(SELECT id FROM entries WHERE path='/pub/readme.txt')"
                ) == [(runs["run1"], 120, runs["run2"])]


def test_a_changed_size_starts_a_new_segment_and_updates_the_entry(ix, tmp_path):
    ingest_dir(ix, write_crawl_dir(tmp_path, "run1", files=[("/a.bin", 10)], at=0))
    ingest_dir(ix, write_crawl_dir(tmp_path, "run2", files=[("/a.bin", 99)], at=1000))
    runs = dict(rows(ix, "SELECT job_id, id FROM runs"))
    assert rows(ix, "SELECT size FROM entries WHERE path='/a.bin'") == [(99,)]
    assert rows(ix, "SELECT run_id, size, last_run FROM entry_versions "
                    "WHERE entry_id=(SELECT id FROM entries WHERE path='/a.bin') "
                    "ORDER BY run_id") == [
        (runs["run1"], 10, runs["run1"]), (runs["run2"], 99, runs["run2"])]


def test_an_entry_that_vanishes_and_returns_gets_a_second_segment(ix, tmp_path):
    """Without this a diff would call a file present in the run it was missing from."""
    ingest_dir(ix, write_crawl_dir(tmp_path, "run1", files=[("/a.bin", 10)], at=0))
    ingest_dir(ix, write_crawl_dir(tmp_path, "run2", files=[("/b.bin", 1)], at=1000))
    ingest_dir(ix, write_crawl_dir(tmp_path, "run3", files=[("/a.bin", 10)], at=2000))
    runs = dict(rows(ix, "SELECT job_id, id FROM runs"))
    segments = rows(ix, "SELECT run_id, size, last_run FROM entry_versions "
                        "WHERE entry_id=(SELECT id FROM entries WHERE path='/a.bin') "
                        "ORDER BY run_id")
    assert segments == [(runs["run1"], 10, runs["run1"]), (runs["run3"], 10, runs["run3"])]


def test_two_targets_in_one_run_are_tracked_separately(ix, tmp_path):
    a = write_crawl_dir(tmp_path, "run1", host=HOST_A, files=[("/x.bin", 1)])
    ingest_dir(ix, a)
    # A run that crawled B only must not look like it removed A's files.
    ingest_dir(ix, write_crawl_dir(tmp_path, "run2", host=HOST_B, files=[("/y.bin", 2)], at=1000))
    runs = dict(rows(ix, "SELECT job_id, id FROM runs"))
    assert rows(ix, "SELECT t.host, r.job_id FROM run_targets rt JOIN targets t ON t.id=rt.target_id "
                    "JOIN runs r ON r.id=rt.run_id ORDER BY 1") == [
        (HOST_A, "run1"), (HOST_B, "run2")]
    assert rows(ix, "SELECT last_run FROM entries WHERE path='/x.bin'") == [(runs["run1"],)]


# ----------------------------------------------------------------------- real crawl


def test_the_output_of_a_real_crawl_ingests(ix, tmp_path):
    """The synthetic records above are only as good as their match to what a crawl writes."""
    from indexserver import IndexServer
    from test_crawl_local import crawl

    root = tmp_path / "srv"
    (root / "a" / "deep").mkdir(parents=True)
    (root / "a" / "readme.txt").write_text("a")
    (root / "a" / "deep" / "buried.txt").write_text("deep")
    (root / "top.bin").write_bytes(b"\0" * 2048)
    out = str(tmp_path / "crawl-out")
    with IndexServer(str(root)) as server:
        crawl(server.base_url, out)

    ingest_dir(ix, out)
    paths = {r[0]: (r[1], r[2]) for r in rows(ix, "SELECT path, kind, size FROM entries")}
    # http.server's autoindex shows no sizes, and an unknown size must stay unknown.
    assert paths["/top.bin"] == ("file", None)
    assert paths["/a/deep/buried.txt"][0] == "file"
    assert paths["/a/deep"][0] == "dir" and paths["/"][0] == "dir"
    assert rows(ix, "SELECT job_id FROM runs") == [("test",)]
    first = dump(ix)
    ingest_dir(ix, out)
    assert dump(ix) == first


# ----------------------------------------------------------------------- archives


ARCHIVE = f"http://{HOST_A}/dumps/backup.7z"


def test_tree_output_is_indexed_under_its_archive(ix, tmp_path):
    nd = write_ndjson(tmp_path / "tree.ndjson", tree_entries([
        ("docs", 0, "dir"), ("docs/plan.docx", 4096), ("docs/notes.txt", 12), ("top.cfg", 7)]))
    summary = ingest_ndjson(ix, nd, archive_url=ARCHIVE, job_id="tree1")

    assert summary.entries_new == 5
    by_path = {r[0]: r[1:] for r in rows(
        ix, "SELECT path, name, size, kind, source, fetch_url FROM entries")}
    assert by_path["/dumps/backup.7z!/docs/plan.docx"] == (
        "plan.docx", 4096, "file", "tree", ARCHIVE)
    assert by_path["/dumps/backup.7z!/docs"][2] == "dir"
    assert by_path["/dumps/backup.7z!"][2:4] == ("dir", "tree"), "the archive's own root"
    assert rows(ix, "SELECT mode, source FROM runs") == [("tree", "tree")]
    assert rows(ix, "SELECT host FROM targets") == [(HOST_A,)]


def test_tree_ingest_is_idempotent_and_survives_a_torn_last_line(ix, tmp_path):
    nd = write_ndjson(tmp_path / "tree.ndjson", tree_entries([("a.txt", 1), ("b.txt", 2)]))
    with open(nd, "a", encoding="utf-8") as fh:
        fh.write('{"path":"c.tx')                      # an interrupted run keeps what it had
    ingest_ndjson(ix, nd, archive_url=ARCHIVE, job_id="tree1")
    first = dump(ix)
    ingest_ndjson(ix, nd, archive_url=ARCHIVE, job_id="tree1")
    assert dump(ix) == first
    assert len(rows(ix, "SELECT 1 FROM entries WHERE source='tree' AND kind='file'")) == 2


def test_a_hostile_member_name_stays_a_name(ix, tmp_path):
    nd = write_ndjson(tmp_path / "tree.ndjson", tree_entries([
        ("../../etc/passwd", 1), ("a/../../b", 1), ("/abs/path", 1)]))
    ingest_ndjson(ix, nd, archive_url=ARCHIVE, job_id="tree1")
    for (path,) in rows(ix, "SELECT path FROM entries WHERE kind='file'"):
        assert path.startswith("/dumps/backup.7z!/"), path


def test_archive_url_is_required_for_tree_output(ix, tmp_path):
    nd = write_ndjson(tmp_path / "tree.ndjson", tree_entries([("a.txt", 1)]))
    with pytest.raises(ValueError, match="archive"):
        ingest_ndjson(ix, nd, archive_url="", job_id="t")


# ----------------------------------------------------------------------- search


def test_path_search_finds_words_and_prefixes_and_is_not_duplicated_by_reingest(ix, tmp_path):
    job = write_crawl_dir(tmp_path, "run1", files=[
        ("/Backups/Payroll_2024.xlsx", 1), ("/Backups/holiday.jpg", 2)])
    ingest_dir(ix, job)
    ingest_dir(ix, job)
    from index import query
    assert [r["path"] for r in query.search(ix, "payroll")] == ["/Backups/Payroll_2024.xlsx"]
    assert [r["path"] for r in query.search(ix, "pay*")] == ["/Backups/Payroll_2024.xlsx"]
    assert {r["path"] for r in query.search(ix, "backups")} >= {
        "/Backups/Payroll_2024.xlsx", "/Backups/holiday.jpg"}
    assert query.search(ix, "nothing-matches-this") == []


@pytest.mark.parametrize("typed", ["/Backups/payroll", "payroll_2024.xlsx", "backups/payroll",
                                   "Payroll-2024", "  PAYROLL  ", "payroll;", "\"payroll"])
def test_a_path_typed_into_the_search_is_read_the_way_the_index_reads_paths(ix, tmp_path, typed):
    """A search box that rejects '-' and '/' rejects the thing it exists to search for."""
    from index import query
    ingest_dir(ix, write_crawl_dir(tmp_path, "run1", files=[("/Backups/Payroll_2024.xlsx", 1)]))
    assert [r["path"] for r in query.search(ix, typed)] == ["/Backups/Payroll_2024.xlsx"], typed


def test_a_search_with_no_words_finds_nothing_and_does_not_error(ix):
    from index import query
    assert query.search(ix, "///---") == [] and query.search(ix, "") == []


def test_a_raw_fts_query_is_available_and_a_malformed_one_is_an_error_not_a_crash(ix, tmp_path):
    from index import query
    ingest_dir(ix, write_crawl_dir(tmp_path, "run1", files=[("/a/payroll.txt", 1), ("/b/mail.txt", 1)]))
    assert {r["path"] for r in query.search(ix, "payroll OR mail", raw=True)} == {
        "/a/payroll.txt", "/b/mail.txt"}
    with pytest.raises(query.QueryError):
        query.search(ix, '"unterminated', raw=True)


# ----------------------------------------------------------------------- custody


def custody(url, sha="ab" * 32, job="dl-1", size=10):
    return {"kind": "custody", "job_id": job, "url": url, "path": "downloads/x", "size": size,
            "sha256": sha, "etag": '"v1"', "last_modified": "Wed, 01 Oct 2025 08:00:00 GMT",
            "piece_size": 4194304, "pieces": 1, "resumed_pieces": 0, "restarts": 0,
            "lanes": [], "started_at": "2026-11-15T10:00:00Z", "finished_at": "2026-11-15T10:01:00Z"}


def test_a_custody_record_is_linked_to_the_entry_it_downloaded(ix, tmp_path):
    ingest_dir(ix, write_crawl_dir(tmp_path, "run1", files=FILES))
    ing = Ingester(ix)
    ing.apply("dl-1", "download.done", custody(f"http://{HOST_A}/pub/readme.txt"))
    ing.flush()
    row = rows(ix, "SELECT e.path, d.sha256, d.size, d.etag, d.job_id FROM downloads d "
                   "JOIN entries e ON e.id=d.entry_id")
    assert row == [("/pub/readme.txt", "ab" * 32, 10, '"v1"', "dl-1")]
    assert json.loads(rows(ix, "SELECT custody_json FROM downloads")[0][0])["kind"] == "custody"


def test_custody_files_found_under_a_directory_are_ingested_once(ix, tmp_path):
    ingest_dir(ix, write_crawl_dir(tmp_path, "run1", files=FILES))
    drop = tmp_path / "downloads" / HOST_A / "pub"
    drop.mkdir(parents=True)
    (drop / "readme.txt.oa.json").write_text(json.dumps(custody(f"http://{HOST_A}/pub/readme.txt")))
    (drop / "stray.oa.json").write_text("{ not json")
    ingest_dir(ix, str(tmp_path / "downloads"))
    ingest_dir(ix, str(tmp_path / "downloads"))
    assert rows(ix, "SELECT COUNT(*) FROM downloads") == [(1,)]


def test_a_custody_record_for_an_unknown_url_is_kept_unlinked(ix):
    ing = Ingester(ix)
    ing.apply("dl-1", "download.done", custody("http://elsewhere.test/f.bin"))
    ing.flush()
    assert rows(ix, "SELECT entry_id, url FROM downloads") == [(None, "http://elsewhere.test/f.bin")]


# ----------------------------------------------------------------------- the docs


def test_the_sql_examples_in_the_readme_run_against_the_real_schema(ix, tmp_path):
    """Documentation that cannot be executed rots: run every ```sql block in the README."""
    import re

    from index.rules import load_rules
    readme = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "README.md"), encoding="utf-8").read()
    section = readme.split("## The persistent index", 1)[1].split("\n## ", 1)[0]
    blocks = re.findall(r"```sql\n(.*?)```", section, re.S)
    assert len(blocks) == 1, "expected the schema section's one SQL block"
    statements = [s.strip() for s in blocks[0].split(";") if s.strip()]
    assert len(statements) == 3

    ingest_dir(ix, write_crawl_dir(tmp_path, "run1", files=[("/hr/payroll_2024.xlsx", 5)], at=0),
               rules=load_rules())
    ingest_dir(ix, write_crawl_dir(tmp_path, "run2", files=[("/hr/payroll_2024.xlsx", 5),
                                                          ("/new/payroll_2025.xlsx", 9)], at=1000),
               rules=load_rules())
    runs = dict(rows(ix, "SELECT job_id, id FROM runs"))
    params = {"host": HOST_A, "run": runs["run1"]}
    results = []
    for statement in statements:
        wanted = {k: v for k, v in params.items() if f":{k}" in statement}
        results.append(ix.conn.execute(statement, wanted).fetchall())
    new_entries, top, sightings = results
    assert ("/new/payroll_2025.xlsx", 9) in new_entries
    assert top and top[0][2] >= 1
    assert {r[1] for r in sightings} == {"/hr/payroll_2024.xlsx"}, "payroll AND 2024, not 2025"
