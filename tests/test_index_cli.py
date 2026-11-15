"""`--mode index`: the actions, through the index CLI and through OnionAccelerator.main."""

import io
import json
import os
import types

import pytest

import OnionAccelerator as oa
from index import cli, store
from indexfixtures import HOST_A, tree_entries, write_crawl_dir, write_ndjson

FILES = [("/it/id_rsa", 1700), ("/hr/payroll_2024.xlsx", 5000), ("/pub/holiday.jpg", 9)]


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "idx" / "index.db")


def run(db, *argv, hooks=None):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(list(argv), index_path=db, hooks=hooks, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


# ----------------------------------------------------------------------- the actions


def test_ingest_reports_what_it_added_and_is_idempotent(db, tmp_path):
    job = write_crawl_dir(tmp_path, "run1", files=FILES)
    code, out, _ = run(db, "ingest", job)
    assert code == 0 and "run1" in out and "7 new" in out and "hit" in out
    code, out, _ = run(db, "ingest", job)
    assert code == 0 and "0 new" in out


def test_ingest_takes_several_paths_and_a_scope_file(db, tmp_path):
    a = write_crawl_dir(tmp_path, "run1", files=[("/shares/Example Corp/a.txt", 1)])
    b = write_crawl_dir(tmp_path, "run2", files=[("/shares/Example Corp/a.txt", 1)], at=1000)
    scope = tmp_path / "scope.toml"
    scope.write_text('[scope]\norganisations = ["Example Corp"]\n')
    code, out, _ = run(db, "ingest", a, b, "--scope", str(scope))
    assert code == 0 and "run1" in out and "run2" in out
    code, out, _ = run(db, "hits", "--min-score", "1")
    assert "scope:organisation:Example Corp" in out


def test_ingest_of_tree_output_needs_to_be_told_the_archive(db, tmp_path):
    nd = write_ndjson(tmp_path / "t.nd", tree_entries([("a.txt", 1)]))
    code, _, err = run(db, "ingest", nd)
    assert code == 2 and "archive-url" in err
    code, out, _ = run(db, "ingest", nd, "--archive-url", f"http://{HOST_A}/x/b.7z",
                       "--job-id", "tree1")
    assert code == 0 and "tree1" in out


def test_ingest_of_a_missing_path_fails_clearly(db, tmp_path):
    code, _, err = run(db, "ingest", str(tmp_path / "nope"))
    assert code == 1 and "nope" in err


def test_diff_prints_added_removed_and_resized(db, tmp_path):
    run(db, "ingest", write_crawl_dir(tmp_path, "run1", files=[("/a", 1), ("/b", 2)], at=0))
    run(db, "ingest", write_crawl_dir(tmp_path, "run2", files=[("/a", 9), ("/c", 3)], at=1000))
    code, out, _ = run(db, "diff", "--target", HOST_A)
    assert code == 0
    assert "+ /c" in out and "- /b" in out and "~ /a" in out and "1 -> 9" in out
    code, out, _ = run(db, "diff", "--target", HOST_A, "--since", "run1", "--format", "json")
    data = json.loads(out)
    assert [e["path"] for e in data["added"]] == ["/c"]
    assert (data["since"], data["until"]) == ("run1", "run2")


def test_diff_with_one_sighting_is_a_clear_error(db, tmp_path):
    run(db, "ingest", write_crawl_dir(tmp_path, "run1", files=[("/a", 1)]))
    code, _, err = run(db, "diff", "--target", HOST_A)
    assert code == 1 and "only one" in err


def test_search_and_hits_and_runs_and_target(db, tmp_path):
    run(db, "ingest", write_crawl_dir(tmp_path, "run1", files=FILES))
    code, out, _ = run(db, "search", "payroll")
    assert code == 0 and "/hr/payroll_2024.xlsx" in out
    code, out, _ = run(db, "hits", "--min-score", "50")
    assert out.index("/it/id_rsa") < out.index("/hr/payroll_2024.xlsx") or "/hr/" in out
    assert "holiday" not in out
    code, out, _ = run(db, "runs")
    assert "run1" in out and "completed" in out
    code, out, _ = run(db, "target", HOST_A, "--label", "Case 12", "--victim", "Example Corp",
                       "--group", "tier-1")
    assert code == 0
    code, out, _ = run(db, "hits", "--min-score", "50", "--format", "json")
    assert json.loads(out)[0]["label"] == "Case 12"


def test_a_path_is_a_valid_search_and_a_malformed_raw_query_is_a_usage_error(db, tmp_path):
    run(db, "ingest", write_crawl_dir(tmp_path, "run1", files=FILES))
    code, out, _ = run(db, "search", "/hr/payroll-2024")
    assert code == 0 and "/hr/payroll_2024.xlsx" in out
    code, _, err = run(db, "search", "--fts", '"unterminated')
    assert code == 2 and "search" in err.lower()


def test_rescore_applies_a_new_scope_to_what_is_already_indexed(db, tmp_path):
    run(db, "ingest", write_crawl_dir(tmp_path, "run1", files=[("/shares/Example Corp/a", 1)]))
    scope = tmp_path / "scope.toml"
    scope.write_text('[scope]\norganisations = ["Example Corp"]\n')
    code, out, _ = run(db, "rescore", "--scope", str(scope))
    assert code == 0 and "rescored" in out
    assert "scope:organisation" in run(db, "hits", "--min-score", "1")[1]


def test_export_to_stdout_and_to_a_file(db, tmp_path):
    run(db, "ingest", write_crawl_dir(tmp_path, "run1", files=FILES))
    code, out, _ = run(db, "export", "--format", "csv", "--min-score", "50")
    assert code == 0 and out.startswith("score,host,")
    target = str(tmp_path / "hits.jsonl")
    code, out, _ = run(db, "export", "--format", "jsonl", "--min-score", "50", "--out", target)
    assert code == 0 and os.path.exists(target)
    code, _, err = run(db, "export", "--format", "jsonl", "--out", target)
    assert code == 1 and "exists" in err
    assert run(db, "export", "--format", "stix")[0] == 2


def test_an_unknown_action_and_a_missing_one(db):
    assert run(db, "frobnicate")[0] == 2
    code, _, err = run(db)
    assert code == 2 and "ingest" in err


def test_fetch_needs_the_programs_engine_and_says_so(db, tmp_path):
    run(db, "ingest", write_crawl_dir(tmp_path, "run1", files=FILES))
    code, _, err = run(db, "fetch", "--min-score", "50")
    assert code == 1 and "engine" in err


def test_fetch_dry_run_lists_without_downloading(db, tmp_path):
    run(db, "ingest", write_crawl_dir(tmp_path, "run1", files=FILES))
    called = []
    hooks = types.SimpleNamespace(
        download=lambda *a, **k: called.append("dl") or [],
        extract=lambda *a, **k: called.append("ex") or True,
        out_dir=str(tmp_path / "dl"), engine_options={})
    code, out, _ = run(db, "fetch", "--min-score", "50", "--dry-run", hooks=hooks)
    assert code == 0 and called == [] and "/it/id_rsa" in out and "would fetch" in out


def test_fetch_with_no_proxy_is_an_error_message_not_a_traceback(db, tmp_path):
    run(db, "ingest", write_crawl_dir(tmp_path, "run1", files=FILES))

    def no_proxies(*a, **k):
        raise RuntimeError("no usable SOCKS5 proxy: start a farm")

    hooks = types.SimpleNamespace(download=no_proxies, extract=no_proxies,
                                  out_dir=str(tmp_path / "dl"), engine_options={})
    code, _, err = run(db, "fetch", "--min-score", "50", hooks=hooks)
    assert code == 1 and "no usable SOCKS5 proxy" in err


def test_fetch_requires_a_threshold(db):
    code, _, err = run(db, "fetch")
    assert code == 2 and "min-score" in err


# ----------------------------------------------------------------------- through main()


@pytest.fixture
def quiet(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(oa, "setup_logging", lambda: None)
    monkeypatch.setattr(oa, "resolve_proxies", lambda *a, **k: pytest.fail("resolved proxies"))


def test_mode_index_runs_without_urls_or_proxies(quiet, db, tmp_path, capsys):
    job = write_crawl_dir(tmp_path, "run1", files=FILES)
    with pytest.raises(SystemExit) as excinfo:
        oa.main(["--mode", "index", "--index", db, "ingest", job])
    assert excinfo.value.code == 0
    assert "run1" in capsys.readouterr().out
    assert os.path.exists(db)


def test_mode_index_passes_exit_codes_through(quiet, db):
    with pytest.raises(SystemExit) as excinfo:
        oa.main(["--mode", "index", "--index", db, "diff", "--target", "nobody.test"])
    assert excinfo.value.code == 1


def test_mode_index_uses_the_default_location_when_no_flag_is_given(quiet, tmp_path, monkeypatch):
    monkeypatch.setenv("ONIONACCELERATOR_INDEX", str(tmp_path / "chosen.db"))
    with pytest.raises(SystemExit):
        oa.main(["--mode", "index", "runs"])
    assert os.path.exists(tmp_path / "chosen.db")


def test_other_modes_still_reject_stray_arguments(quiet):
    with pytest.raises(SystemExit) as excinfo:
        oa.main(["--mode", "multi", "ingest", "somewhere"])
    assert excinfo.value.code == 2


def test_stream_is_refused_for_index_mode(quiet, db):
    with pytest.raises(SystemExit) as excinfo:
        oa.main(["--mode", "index", "--index", db, "--stream", "runs"])
    assert excinfo.value.code == 2


def test_fetch_through_main_uses_the_download_engine_and_the_shared_endpoint_ladder(
        quiet, db, tmp_path, monkeypatch):
    run(db, "ingest", write_crawl_dir(tmp_path, "run1", files=FILES))
    seen = {}
    monkeypatch.setattr(oa, "resolve_crawl_endpoints", lambda args, urls: ["127.0.0.1:1"])

    def fake_multi(urls, proxies, **kwargs):
        seen.update(urls=list(urls), proxies=proxies, **kwargs)
        return [types.SimpleNamespace(url=u, ok=True, reason=None, sha256="ee" * 32,
                                      custody={"kind": "custody", "job_id": "j", "url": u,
                                               "size": 1, "sha256": "ee" * 32}) for u in urls]

    monkeypatch.setattr(oa, "multi_download_mode", fake_multi)
    with pytest.raises(SystemExit) as excinfo:
        oa.main(["--mode", "index", "--index", db, "--piece-size", "1M", "--retries", "5",
                 "fetch", "--min-score", "50"])
    assert excinfo.value.code == 0
    assert f"http://{HOST_A}/it/id_rsa" in seen["urls"] and seen["proxies"] == ["127.0.0.1:1"]
    assert seen["piece_size"] == 1 << 20 and seen["retries"] == 5 and seen["preserve_path"] is True
    with store.Index.open(db) as ix:
        assert ix.conn.execute("SELECT COUNT(*) FROM downloads").fetchone()[0] >= 1
