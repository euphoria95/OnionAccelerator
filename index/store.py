"""The index file: a private SQLite database in WAL mode, migrated on open.

Where it lives. A default in the user's data directory, outside any repository
($XDG_DATA_HOME/onionaccelerator/index.db, else ~/.local/share/...), overridable with
--index or $ONIONACCELERATOR_INDEX. It records every path a leak site listed and what each
matched, so it is evidence and is treated as such.

Who can read it. The file is created 0600, in a directory this module makes 0700 if it
had to make it -- before SQLite sees it. A database created at the umask and chmod'ed
afterwards has a window in which it is readable, and SQLite gives the WAL and shared-
memory files the same permissions as the main file, so the order matters. A directory
that already existed is the user's own and is not touched; a file that already existed
with looser permissions is tightened, with a warning.

What it will not do. Open a file that is not an SQLite database (it would overwrite
somebody's notes) or one written by a newer version of this program (it would downgrade
a schema it does not understand): both are refused before anything is changed.

Standard library only -- sqlite3 with FTS5, which every supported CPython ships.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import stat
import time
from typing import Optional

logger = logging.getLogger("OnionAccelerator.index")

SQLITE_MAGIC = b"SQLite format 3\x00"


class IndexStoreError(RuntimeError):
    """The index cannot be opened, and says why. Nothing was changed."""


def default_index_path(environ=None) -> str:
    env = os.environ if environ is None else environ
    explicit = env.get("ONIONACCELERATOR_INDEX")
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    base = env.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "onionaccelerator", "index.db")


# ----------------------------------------------------------------------- the schema
#
# runs            one crawl, tree walk or download job; `job_id` is what the stream calls
#                 `run`, so a live index and an offline one name runs identically.
#                 started_at / finished_at are the earliest and latest record timestamps,
#                 not the wall clock: an index built later from the same artefacts must be
#                 equal to one built live.
# targets         a host, plus what the operator knows about it.
# run_targets     which targets a run covered. A run that crawled B says nothing about A.
# entries         one row per (target, path), kept for the life of the index.
#                 first_run / last_run are the first and last run that saw it.
# entry_versions  presence segments: (entry, first run of the segment, size, last run of
#                 the segment). A new segment starts when the size changes or when the
#                 entry comes back after a run that did not have it, so "was it there in
#                 run N?" is exactly "does a segment cover N" -- which first_run/last_run
#                 alone cannot say for an entry that vanished and returned.
# hits            one row per (entry, rule): the score, why, and the span of the path.
# downloads       the engine's custody record, linked to the entry it fetched when known.
MIGRATIONS = [
    (1, """
    CREATE TABLE runs (
        id              INTEGER PRIMARY KEY,
        job_id          TEXT NOT NULL UNIQUE,
        mode            TEXT NOT NULL,
        source          TEXT NOT NULL,
        started_at      TEXT,
        finished_at     TEXT,
        stopped_because TEXT
    );
    CREATE TABLE targets (
        id      INTEGER PRIMARY KEY,
        host    TEXT NOT NULL UNIQUE,
        label   TEXT,
        grp     TEXT,
        victim  TEXT
    );
    CREATE TABLE run_targets (
        run_id    INTEGER NOT NULL REFERENCES runs(id),
        target_id INTEGER NOT NULL REFERENCES targets(id),
        PRIMARY KEY (run_id, target_id)
    ) WITHOUT ROWID;
    CREATE TABLE entries (
        id         INTEGER PRIMARY KEY,
        target_id  INTEGER NOT NULL REFERENCES targets(id),
        path       TEXT NOT NULL,
        name       TEXT NOT NULL,
        size       INTEGER,
        mtime_text TEXT,
        kind       TEXT NOT NULL,
        source     TEXT NOT NULL,
        fetch_url  TEXT,
        first_run  INTEGER NOT NULL REFERENCES runs(id),
        last_run   INTEGER NOT NULL REFERENCES runs(id),
        UNIQUE (target_id, path)
    );
    CREATE INDEX entries_by_last_run  ON entries (target_id, last_run);
    CREATE INDEX entries_by_first_run ON entries (target_id, first_run);
    CREATE INDEX entries_by_fetch_url ON entries (fetch_url);
    CREATE TABLE entry_versions (
        entry_id INTEGER NOT NULL REFERENCES entries(id),
        run_id   INTEGER NOT NULL REFERENCES runs(id),
        size     INTEGER,
        last_run INTEGER NOT NULL REFERENCES runs(id),
        PRIMARY KEY (entry_id, run_id)
    ) WITHOUT ROWID;
    CREATE VIRTUAL TABLE entries_fts USING fts5(
        path, content='entries', content_rowid='id', tokenize='unicode61 remove_diacritics 2');
    CREATE TRIGGER entries_fts_insert AFTER INSERT ON entries BEGIN
        INSERT INTO entries_fts(rowid, path) VALUES (new.id, new.path);
    END;
    CREATE TRIGGER entries_fts_delete AFTER DELETE ON entries BEGIN
        INSERT INTO entries_fts(entries_fts, rowid, path) VALUES ('delete', old.id, old.path);
    END;
    CREATE TABLE hits (
        id         INTEGER PRIMARY KEY,
        entry_id   INTEGER NOT NULL REFERENCES entries(id),
        rule       TEXT NOT NULL,
        score      INTEGER NOT NULL,
        reason     TEXT NOT NULL,
        span_start INTEGER,
        span_end   INTEGER,
        UNIQUE (entry_id, rule)
    );
    CREATE INDEX hits_by_score ON hits (score DESC);
    CREATE TABLE downloads (
        id            INTEGER PRIMARY KEY,
        entry_id      INTEGER REFERENCES entries(id),
        url           TEXT NOT NULL,
        sha256        TEXT NOT NULL,
        size          INTEGER,
        etag          TEXT,
        last_modified TEXT,
        job_id        TEXT NOT NULL,
        custody_json  TEXT NOT NULL,
        UNIQUE (url, job_id, sha256)
    );
    CREATE INDEX downloads_by_entry ON downloads (entry_id);
    """),
]
SCHEMA_VERSION = MIGRATIONS[-1][0]


def _is_sqlite(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
    except OSError as exc:
        raise IndexStoreError(f"cannot read {path}: {exc}") from exc
    return head == b"" or head == SQLITE_MAGIC


def _recorded_version(path: str) -> int:
    """The newest migration the file records, read without being able to change it."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return 0
    try:
        return conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


class Index:
    """An open index. Use `Index.open`, as a context manager."""

    def __init__(self, conn: sqlite3.Connection, path: str):
        self.conn, self.path = conn, path

    @property
    def version(self) -> int:
        return self.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]

    @classmethod
    def open(cls, path: Optional[str] = None) -> "Index":
        path = os.path.abspath(path or default_index_path())
        parent = os.path.dirname(path)
        if not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
            os.chmod(parent, 0o700)
        if os.path.exists(path):
            if not _is_sqlite(path):
                raise IndexStoreError(
                    f"{path} exists and is not an SQLite database; refusing to use or "
                    f"overwrite it. Choose another --index.")
            recorded = _recorded_version(path)
            if recorded > SCHEMA_VERSION:
                raise IndexStoreError(
                    f"{path} was written by a newer OnionAccelerator (schema {recorded}, "
                    f"this one understands {SCHEMA_VERSION}); upgrade rather than open it.")
            if stat.S_IMODE(os.stat(path).st_mode) & 0o077:
                logger.warning("tightening %s to 0600: it was readable by other users", path)
                os.chmod(path, 0o600)
        else:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        try:
            conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
        except sqlite3.Error as exc:
            raise IndexStoreError(f"cannot open {path}: {exc}") from exc
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=30000")
            cls._migrate(conn)
        except sqlite3.Error as exc:
            conn.close()
            raise IndexStoreError(f"{path} is not a usable index: {exc}") from exc
        except BaseException:
            conn.close()
            raise
        return cls(conn, path)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations ("
                     "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        have = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
        for version, script in MIGRATIONS:
            if version <= have:
                continue
            # One transaction per migration: a failure leaves the schema at the version
            # before it, never half of one.
            conn.executescript("BEGIN;\n" + script + f"""
                INSERT INTO schema_migrations(version, applied_at)
                VALUES ({version}, '{time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}');
                COMMIT;""")

    def close(self) -> None:
        try:
            self.conn.commit()
        except sqlite3.Error:
            pass
        self.conn.close()

    def __enter__(self) -> "Index":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
