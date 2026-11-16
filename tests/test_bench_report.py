"""Tables, and the comparison that makes 'reproducible' something a command can say."""

import os
import subprocess
import sys

import pytest

from bench import report, runners
from bench.report import Table, compare, parse_tables, render_table

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def table(rows=None, title="Throughput", columns=("lanes", "engine s", "engine % of ideal")):
    return Table(title, list(columns), rows or [["1", "16.2", "98%"], ["8", "2.2", "90%"]],
                 ["a note that is not part of the comparison"])


def test_a_table_survives_being_written_and_read_back():
    t = table()
    back = parse_tables("some prose\n\n" + render_table(t) + "\nmore prose\n")
    assert len(back) == 1
    assert (back[0].title, back[0].columns, back[0].rows) == (t.title, t.columns, t.rows)


def test_several_tables_on_one_page_are_all_found():
    page = render_table(table(title="A")) + "\n" + render_table(table(title="B"))
    assert [t.title for t in parse_tables(page)] == ["A", "B"]


def test_an_identical_rerun_reproduces():
    assert compare([table()], [table()]) == []


def test_noise_inside_the_tolerance_reproduces_and_outside_it_does_not():
    near = table([["1", "17.0", "96%"], ["8", "2.4", "88%"]])
    assert compare([near], [table()], tolerance=0.35) == []
    far = table([["1", "40.0", "98%"], ["8", "2.2", "90%"]])
    problems = compare([far], [table()], tolerance=0.35)
    assert len(problems) == 1 and "engine s" in problems[0] and "1" in problems[0]


def test_tiny_absolute_differences_are_not_noise_worth_failing_for():
    assert compare([table([["1", "0.2", "98%"]])], [table([["1", "0.3", "98%"]])]) == []


def test_a_changed_shape_never_reproduces():
    assert compare([table(columns=("lanes", "x", "y"))], [table()])
    assert compare([table([["1", "1", "1%"]])], [table([["2", "1", "1%"]])])
    assert compare([table(title="New")], [table()])
    assert compare([], [table()])                      # published but not produced


def test_a_text_cell_must_match_exactly_and_n_a_is_not_a_number():
    a = table([["1", "n/a", "98%"]])
    assert compare([a], [table([["1", "n/a", "98%"]])]) == []
    assert compare([a], [table([["1", "12.0", "98%"]])]), "n/a is not 'close to 12'"


@pytest.mark.parametrize("cell, value", [("16.2", 16.2), ("98%", 98.0), ("1.00x", 1.0),
                                          ("0.68x", 0.68), ("n/a", None), ("2/5", None),
                                          ("hedging on", None)])
def test_cells_read_as_numbers_only_when_they_are_numbers(cell, value):
    assert report._number(cell) == value


# ----------------------------------------------------------------------------- runners


def test_the_aria2c_command_gives_it_every_mirror_for_one_file():
    argv = runners.aria2c_argv("/usr/bin/aria2c", ["http://a/f", "http://b/f"], "/tmp/d")
    assert argv[0] == "/usr/bin/aria2c" and argv[-2:] == ["http://a/f", "http://b/f"]
    assert "-s2" in argv and "-x1" in argv and "-d" in argv


def test_an_unknown_baseline_commit_is_reported_not_guessed():
    assert runners.prepare_baseline("0" * 40) is None


def test_the_baseline_is_extracted_when_the_commit_is_here():
    have = subprocess.run(["git", "-C", ROOT, "cat-file", "-e", "6d8a1d8^{commit}"],
                          capture_output=True).returncode == 0
    if not have:
        pytest.skip("this clone is shallow and does not contain the baseline commit")
    dest = runners.prepare_baseline("6d8a1d8")
    try:
        assert os.path.isfile(os.path.join(dest, "OnionAccelerator.py"))
        assert not os.path.exists(os.path.join(dest, "oacore")), "the pre-engine code has none"
    finally:
        import shutil
        shutil.rmtree(dest, ignore_errors=True)


def test_aria2c_is_skipped_cleanly_when_it_is_not_installed(monkeypatch):
    monkeypatch.setattr(runners, "aria2c_path", lambda: None)
    assert runners.run_aria2c(None, ["http://x/f"]) is None


def test_a_wrong_file_from_a_system_under_test_is_a_failure_not_a_time(tmp_path):
    """A fast wrong answer is not a result."""
    class FakeLab:
        info = {"content_port": 1, "sha256": "0" * 64}
    host = tmp_path / "partials" / "bench.lab_1"
    host.mkdir(parents=True)
    (host / "file.bin").write_bytes(b"not the right bytes")
    run = runners.verify(FakeLab, str(tmp_path), "engine", {"seconds": 0.01, "ok": [True]})
    assert not run.ok and "wrong" in run.note
    assert not runners.verify(FakeLab, str(tmp_path), "engine", None).ok


# ----------------------------------------------------------------------------- the command


def test_the_command_refuses_to_publish_quick_numbers():
    done = subprocess.run([sys.executable, "-m", "bench", "--quick", "--write-docs"], cwd=ROOT,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 2 and "quick" in done.stderr


def test_a_partial_run_cannot_overwrite_the_page(tmp_path):
    done = subprocess.run([sys.executable, "-m", "bench", "--suite", "scaling", "--write-docs",
                           str(tmp_path / "p.md")], cwd=ROOT, capture_output=True, text=True,
                          timeout=60)
    assert done.returncode == 2 and not (tmp_path / "p.md").exists()


@pytest.mark.slow
def test_a_quick_run_of_every_suite_produces_every_table():
    done = subprocess.run([sys.executable, "-m", "bench", "--quick"], cwd=ROOT,
                          capture_output=True, text=True, timeout=900)
    assert done.returncode == 0, done.stderr[-500:]
    titles = [t.title for t in parse_tables(done.stdout)]
    assert titles == ["Throughput against lane count", "Bytes served on a clean network",
                      "One lane at a tenth of the speed", "Connections that die mid-transfer",
                      "Killed halfway, then run again",
                      "Time to the first listing entry, and what hedging costs"]
    for t in parse_tables(done.stdout):
        assert all(len(r) == len(t.columns) for r in t.rows), t.title
