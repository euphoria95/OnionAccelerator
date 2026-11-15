"""The index file: where it lives, who can read it, and what shape it has.

The index is evidence: every path a leak site listed, with what it matched. So the file is
created 0600 in a 0700 directory, outside the repository, before SQLite ever sees it -- a
database created at the umask and chmod'ed afterwards has a window in which it is not
private, and a WAL file created later inherits whatever the directory allows.
"""

import os
import sqlite3
import stat

import pytest

from index import store


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def test_the_default_path_is_in_the_users_data_directory_not_the_repo(monkeypatch, tmp_path):
    monkeypatch.delenv("ONIONACCELERATOR_INDEX", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    path = store.default_index_path()
    assert path == str(tmp_path / "xdg" / "onionaccelerator" / "index.db")
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assert not path.startswith(repo)


def test_the_environment_overrides_the_default(monkeypatch, tmp_path):
    monkeypatch.setenv("ONIONACCELERATOR_INDEX", str(tmp_path / "mine.db"))
    assert store.default_index_path() == str(tmp_path / "mine.db")


def test_the_file_is_private_from_the_moment_it_exists(tmp_path):
    path = tmp_path / "new" / "dir" / "index.db"
    with store.Index.open(str(path)) as ix:
        ix.conn.execute("INSERT INTO targets(host) VALUES ('x.test')")
        ix.conn.commit()
        assert mode(path) == 0o600
        assert mode(path.parent) == 0o700
        for sidecar in (str(path) + "-wal", str(path) + "-shm"):
            if os.path.exists(sidecar):
                assert mode(sidecar) == 0o600, sidecar


def test_a_looser_existing_file_is_tightened(tmp_path, caplog):
    path = tmp_path / "index.db"
    sqlite3.connect(path).close()
    os.chmod(path, 0o644)
    with store.Index.open(str(path)):
        pass
    assert mode(path) == 0o600
    assert "0644" in caplog.text or "tighten" in caplog.text


def test_an_existing_directory_is_not_chmodded(tmp_path):
    """Only a directory the index creates is made 0700: the user's own one is theirs."""
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    os.chmod(shared, 0o755)
    with store.Index.open(str(shared / "index.db")):
        pass
    assert mode(shared) == 0o755


def test_wal_foreign_keys_and_fts5_are_on(tmp_path):
    with store.Index.open(str(tmp_path / "i.db")) as ix:
        assert ix.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert ix.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        ix.conn.execute("SELECT * FROM entries_fts WHERE entries_fts MATCH 'x'").fetchall()


def test_the_schema_has_the_approved_tables(tmp_path):
    with store.Index.open(str(tmp_path / "i.db")) as ix:
        tables = {r[0] for r in ix.conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table')")}
    assert {"runs", "targets", "entries", "entry_versions", "hits", "downloads",
            "run_targets", "schema_migrations", "entries_fts"} <= tables


def test_opening_twice_applies_each_migration_once(tmp_path):
    path = str(tmp_path / "i.db")
    with store.Index.open(path) as ix:
        first = ix.conn.execute("SELECT version, applied_at FROM schema_migrations").fetchall()
        assert [v for v, _ in first] == list(range(1, store.SCHEMA_VERSION + 1))
    with store.Index.open(path) as ix:
        assert ix.conn.execute("SELECT version, applied_at FROM schema_migrations"
                               ).fetchall() == first
        assert ix.version == store.SCHEMA_VERSION


def test_a_database_from_a_newer_version_is_refused_untouched(tmp_path):
    path = str(tmp_path / "i.db")
    with store.Index.open(path) as ix:
        ix.conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (999, 'x')")
        ix.conn.commit()
    before = open(path, "rb").read()
    with pytest.raises(store.IndexStoreError, match="newer"):
        store.Index.open(path)
    assert open(path, "rb").read() == before


def test_a_file_that_is_not_an_index_is_refused_not_overwritten(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("my notes, not a database, and not mine to touch\n" * 30)
    with pytest.raises(store.IndexStoreError):
        store.Index.open(str(path))
    assert path.read_text().startswith("my notes")


def test_a_reader_is_not_blocked_by_a_writer(tmp_path):
    """WAL: an analyst can query while a live consumer is writing."""
    path = str(tmp_path / "i.db")
    with store.Index.open(path) as writer:
        writer.conn.execute("INSERT INTO targets(host) VALUES ('a.test')")
        writer.conn.commit()
        writer.conn.execute("INSERT INTO targets(host) VALUES ('b.test')")      # uncommitted
        with store.Index.open(path) as reader:
            hosts = [r[0] for r in reader.conn.execute("SELECT host FROM targets")]
        assert hosts == ["a.test"]
        writer.conn.commit()


def test_the_test_suite_cannot_reach_the_real_index():
    """tests/conftest.py's guard: a test that forgets --index must not write to ~/.local."""
    assert not store.default_index_path().startswith(os.path.expanduser("~/.local"))
