"""Selective fetch and export.

`fetch` hands the index's best hits to the download engine (and a hit inside an archive to
rvtree's `extract`); `export` writes them somewhere an analyst can read them. Neither
touches a network here: the download and extract callables are injected, which is also how
the command line wires the real ones in.
"""

import csv
import io
import json
import os
import stat
from types import SimpleNamespace

import pytest

from index import export as exportmod
from index import fetch as fetchmod
from index import store
from index.ingest import ingest_dir, ingest_ndjson
from index.rules import load_rules
from indexfixtures import HOST_A, HOST_B, tree_entries, write_crawl_dir, write_ndjson

ARCHIVE = f"http://{HOST_A}/dumps/backup.7z"


@pytest.fixture
def ix(tmp_path):
    with store.Index.open(str(tmp_path / "idx" / "index.db")) as index:
        yield index


@pytest.fixture
def populated(ix, tmp_path):
    ingest_dir(ix, write_crawl_dir(tmp_path, "run1", host=HOST_A, files=[
        ("/it/id_rsa", 1700), ("/hr/payroll_2024.xlsx", 5000), ("/pub/holiday.jpg", 9),
        ("/mail/archive.pst", 99)]), rules=load_rules())
    ingest_dir(ix, write_crawl_dir(tmp_path, "run2", host=HOST_B, files=[
        ("/vpn/client.ovpn", 300)], at=1000), rules=load_rules())
    ingest_ndjson(ix, write_ndjson(tmp_path / "t.nd", tree_entries([
        ("db/prod_dump.sql", 77), ("docs/readme.txt", 3)])), archive_url=ARCHIVE,
        job_id="tree1", rules=load_rules())
    return ix


class Recorder:
    """Stands in for the engine and for rvtree: records calls, returns scripted results."""

    def __init__(self, fail=()):
        self.downloads, self.extracts, self.fail = [], [], set(fail)

    def download(self, urls, **options):
        self.downloads.append((list(urls), options))
        out = []
        for url in urls:
            ok = url not in self.fail
            out.append(SimpleNamespace(
                url=url, ok=ok, reason=None if ok else "refused", sha256="cd" * 32 if ok else None,
                custody={"kind": "custody", "job_id": "dl-9", "url": url, "size": 7,
                         "sha256": "cd" * 32, "etag": '"e"', "last_modified": None} if ok else None))
        return out

    def extract(self, archive_url, member, dest, max_fetch_mib):
        self.extracts.append((archive_url, member, dest, max_fetch_mib))
        return archive_url not in self.fail


# ----------------------------------------------------------------------- selection


def test_selection_is_files_at_or_above_the_score_best_first(populated):
    chosen = fetchmod.select(populated, min_score=50)
    scores = [c.score for c in chosen]
    assert scores == sorted(scores, reverse=True)
    paths = [c.path for c in chosen]
    assert "/it/id_rsa" in paths and "/pub/holiday.jpg" not in paths
    assert all(c.kind == "file" for c in chosen), "a directory cannot be fetched"


def test_a_higher_threshold_selects_fewer(populated):
    low = {c.path for c in fetchmod.select(populated, min_score=1)}
    high = {c.path for c in fetchmod.select(populated, min_score=90)}
    assert high < low and "/it/id_rsa" in high


def test_selection_can_be_narrowed_to_a_target_and_a_count(populated):
    only_b = fetchmod.select(populated, min_score=1, host=HOST_B)
    assert {c.host for c in only_b} == {HOST_B} and [c.path for c in only_b] == ["/vpn/client.ovpn"]
    assert len(fetchmod.select(populated, min_score=1, limit=2)) == 2


def test_an_entry_already_downloaded_is_not_fetched_again_unless_asked(populated):
    from index.ingest import Ingester
    ing = Ingester(populated)
    ing.apply("dl-1", "download.done", {"kind": "custody", "job_id": "dl-1", "size": 1700,
              "url": f"http://{HOST_A}/it/id_rsa", "sha256": "ab" * 32})
    ing.flush()
    assert "/it/id_rsa" not in {c.path for c in fetchmod.select(populated, min_score=1)}
    assert "/it/id_rsa" in {c.path for c in fetchmod.select(populated, min_score=1, again=True)}


def test_an_archive_member_is_selected_as_an_extract_of_its_archive(populated):
    member = next(c for c in fetchmod.select(populated, min_score=1)
                  if c.path.endswith("prod_dump.sql"))
    assert member.source == "tree"
    assert (member.container_url, member.member) == (ARCHIVE, "db/prod_dump.sql")


def test_two_entries_with_one_fetch_url_are_fetched_once(ix, tmp_path):
    from indexfixtures import file_record
    twin = [file_record(HOST_A, "/view/id_rsa", 1, download_url=f"http://{HOST_A}/get?f=1"),
            file_record(HOST_A, "/other/id_rsa", 1, download_url=f"http://{HOST_A}/get?f=1")]
    ingest_dir(ix, write_crawl_dir(tmp_path, "run1", files=[], extra_files=twin),
               rules=load_rules())
    urls = [c.url for c in fetchmod.select(ix, min_score=1)]
    assert urls == [f"http://{HOST_A}/get?f=1"]


# ----------------------------------------------------------------------- running it


def test_a_dry_run_selects_and_calls_nothing(populated, tmp_path):
    rec = Recorder()
    plan = fetchmod.run(populated, min_score=50, download=rec.download, extract=rec.extract,
                        out_dir=str(tmp_path / "dl"), dry_run=True)
    assert plan.downloaded == [] and rec.downloads == [] and rec.extracts == []
    assert plan.selected and plan.dry_run


def test_files_go_to_the_engine_in_one_call_and_archive_members_to_extract(populated, tmp_path):
    rec = Recorder()
    summary = fetchmod.run(populated, min_score=50, download=rec.download, extract=rec.extract,
                           out_dir=str(tmp_path / "dl"), max_fetch_mib=256,
                           engine_options={"piece_size": 1 << 20})
    assert len(rec.downloads) == 1
    urls, options = rec.downloads[0]
    assert f"http://{HOST_A}/it/id_rsa" in urls and f"http://{HOST_B}/vpn/client.ovpn" in urls
    assert options["piece_size"] == 1 << 20 and options["preserve_path"] is True
    assert [(a, m, mib) for a, m, _, mib in rec.extracts] == [(ARCHIVE, "db/prod_dump.sql", 256)]
    assert summary.ok == len(urls) + 1 and summary.failed == 0


def test_custody_is_recorded_and_linked_so_the_entry_is_not_fetched_twice(populated, tmp_path):
    rec = Recorder()
    fetchmod.run(populated, min_score=50, download=rec.download, extract=rec.extract,
                 out_dir=str(tmp_path / "dl"))
    linked = populated.conn.execute(
        "SELECT e.path, d.sha256, d.job_id FROM downloads d JOIN entries e ON e.id=d.entry_id "
        "ORDER BY e.path").fetchall()
    assert ("/it/id_rsa", "cd" * 32, "dl-9") in linked
    again = Recorder()
    fetchmod.run(populated, min_score=50, download=again.download, extract=again.extract,
                 out_dir=str(tmp_path / "dl"))
    fetched = [u for urls, _ in again.downloads for u in urls]
    assert f"http://{HOST_A}/it/id_rsa" not in fetched


def test_a_failure_is_counted_and_does_not_stop_the_rest(populated, tmp_path):
    rec = Recorder(fail={f"http://{HOST_A}/it/id_rsa", ARCHIVE})
    summary = fetchmod.run(populated, min_score=50, download=rec.download, extract=rec.extract,
                           out_dir=str(tmp_path / "dl"))
    assert summary.failed == 2 and summary.ok >= 1
    assert any(f.startswith(f"http://{HOST_A}/it/id_rsa") for f in summary.failures)


def test_extract_destinations_stay_inside_the_output_directory(tmp_path):
    base = str(tmp_path / "dl")
    for member in ("../../etc/passwd", "/abs/path", "a/../../b", "a\\..\\..\\b", "x\x00y",
                   "..", ".", "", "C:/windows/x", "a/" + "b" * 400):
        dest = fetchmod.extract_dest(base, HOST_A, "/dumps/backup.7z", member or "empty")
        real = os.path.realpath(dest)
        assert real == os.path.realpath(base) or real.startswith(os.path.realpath(base) + os.sep), (
            member, dest)
        assert max(len(p) for p in dest.split(os.sep)) <= 255


def test_an_archive_url_with_a_hostile_path_cannot_escape_either(tmp_path):
    base = str(tmp_path / "dl")
    dest = fetchmod.extract_dest(base, "evil.test", "/../../outside/x.7z", "m.txt")
    assert os.path.realpath(dest).startswith(os.path.realpath(base) + os.sep)


# ----------------------------------------------------------------------- export


def export(ix, fmt, **kwargs):
    buf = io.StringIO()
    count = exportmod.export_hits(ix, fmt, buf, **kwargs)
    return count, buf.getvalue()


def test_jsonl_has_one_object_per_entry_with_every_hit_and_where_it_came_from(populated):
    populated.conn.execute("UPDATE targets SET label='Case 12', grp='tier-1', victim='Example Corp' "
                           "WHERE host=?", (HOST_A,))
    populated.conn.commit()
    count, text = export(populated, "jsonl", min_score=50)
    objs = [json.loads(line) for line in text.splitlines()]
    assert count == len(objs) and count >= 4
    key = next(o for o in objs if o["path"] == "/it/id_rsa")
    assert key["host"] == HOST_A and key["label"] == "Case 12" and key["group"] == "tier-1"
    assert key["victim"] == "Example Corp" and key["size"] == 1700
    assert key["first_run"] == "run1" and key["last_run"] == "run1"
    assert key["score"] == max(h["score"] for h in key["hits"])
    assert all({"rule", "score", "reason", "span"} <= set(h) for h in key["hits"])
    assert key["downloads"] == []
    assert objs == sorted(objs, key=lambda o: (-o["score"], o["host"], o["path"]))


def test_a_download_appears_in_the_export_with_its_hash(populated, tmp_path):
    rec = Recorder()
    fetchmod.run(populated, min_score=90, download=rec.download, extract=rec.extract,
                 out_dir=str(tmp_path / "dl"))
    _, text = export(populated, "jsonl", min_score=90)
    key = next(json.loads(l) for l in text.splitlines() if '"/it/id_rsa"' in l)
    assert key["downloads"][0]["sha256"] == "cd" * 32


def test_csv_is_flat_and_round_trips_awkward_paths(ix, tmp_path):
    from indexfixtures import file_record
    awkward = 'id_rsa, "the key"\nline two'
    ingest_dir(ix, write_crawl_dir(tmp_path, "run1", files=[], extra_files=[
        file_record(HOST_A, "/it/" + awkward, 5)]), rules=load_rules())
    _, text = export(ix, "csv", min_score=1)
    rows = list(csv.DictReader(io.StringIO(text, newline="")))
    assert rows and rows[0]["path"] == "/it/" + awkward
    assert list(rows[0]) == ["score", "host", "label", "group", "victim", "path", "size",
                             "kind", "source", "url", "rules", "reasons", "first_run",
                             "last_run", "sha256"]


@pytest.mark.parametrize("lead", ["=", "+", "-", "@", "\t", "\r"])
def test_csv_cells_a_spreadsheet_would_run_as_formulas_are_defused(ix, tmp_path, lead):
    """A listing is written by the server being investigated; an analyst opens this file."""
    from indexfixtures import file_record
    evil = f"{lead}HYPERLINK(\"http://x.test\",\"id_rsa\")"
    ingest_dir(ix, write_crawl_dir(tmp_path, "run1", files=[], extra_files=[
        file_record(HOST_A, "/it/id_rsa", 5)]), rules=load_rules())
    ix.conn.execute("UPDATE targets SET label=?", (evil,))
    ix.conn.commit()
    _, text = export(ix, "csv", min_score=1)
    row = next(csv.DictReader(io.StringIO(text, newline="")))
    assert row["label"] == "'" + evil, "a cell starting with a formula character must be quoted"


def test_csv_leaves_ordinary_text_and_negative_looking_sizes_alone(populated):
    _, text = export(populated, "csv", min_score=50)
    row = next(r for r in csv.DictReader(io.StringIO(text, newline="")) if r["path"] == "/it/id_rsa")
    assert row["size"] == "1700" and not row["path"].startswith("'")


def test_export_filters_and_an_unknown_format(populated):
    only_b = export(populated, "jsonl", min_score=1, host=HOST_B)[1]
    assert {json.loads(l)["host"] for l in only_b.splitlines()} == {HOST_B}
    with pytest.raises(ValueError, match="stix|format"):
        exportmod.export_hits(populated, "stix", io.StringIO())


def test_export_to_a_file_is_private_and_will_not_overwrite(populated, tmp_path):
    out = tmp_path / "out" / "hits.jsonl"
    exportmod.export_to_path(populated, "jsonl", str(out), min_score=50)
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600
    before = out.read_bytes()
    with pytest.raises(FileExistsError):
        exportmod.export_to_path(populated, "jsonl", str(out), min_score=50)
    assert out.read_bytes() == before
    exportmod.export_to_path(populated, "jsonl", str(out), min_score=50, force=True)
