"""Records and events -> rows. One ingester for the offline and the live path.

The stream's contract is that an event's `data` is byte-for-byte the record the run wrote
to disk, so a crawl directory read back is the same sequence of (kind, data) a consumer
would have seen, and `Ingester.apply(job_id, kind, data)` is the only door in. That is
what makes "an index built live equals one built afterwards" a property of the code rather
than a thing to check: there is one code path, fed by two readers (`ingest_dir`,
`ingest_ndjson`) and one listener (index/live.py).

Idempotent by construction, including for an old run ingested again after a newer one has
moved every entry's last_run forward. "Was this entry already seen in run N?" is answered
by the presence segments in entry_versions, not by last_run, so a duplicate is recognised
wherever it falls.

Order independence: nothing depends on a directory arriving before its files, or on the
order of two records of one run. Surrogate ids are an accident of that order and are not
compared anywhere.

Hostile input is the normal case. A record that is not an object, has the wrong types, a
path with a NUL or over MAX_PATH characters, or a negative size is *skipped and counted*,
never raised: one bad line must not cost the other million. A negative size is not a size,
so it becomes unknown rather than discarding the entry.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import time
from typing import Any, Optional
from urllib.parse import unquote, urlsplit

from .rules import RuleSet, load_rules

logger = logging.getLogger("OnionAccelerator.index.ingest")

MAX_PATH = 4096
MAX_LINE = 1 << 20
CUSTODY_SUFFIX = ".oa.json"

CRAWL_KINDS = ("crawl.dir", "crawl.file")
TREE_KINDS = ("tree.open", "tree.entry", "tree.done")


@dataclasses.dataclass
class Summary:
    """What one ingest did. `entries_new` is what the index did not have before."""

    job_id: Optional[str] = None
    entries_new: int = 0
    entries_seen: int = 0          # already in the index: a re-ingest, or a repeat in a run
    skipped: int = 0               # records that were not usable
    hits: int = 0
    custody: int = 0

    def __iadd__(self, other: "Summary") -> "Summary":
        for f in dataclasses.fields(self):
            if f.name != "job_id":
                setattr(self, f.name, getattr(self, f.name) + getattr(other, f.name))
        return self


@dataclasses.dataclass
class _Run:
    row: int
    mode: str
    source: str
    first: Optional[str] = None
    last: Optional[str] = None
    stopped: Optional[str] = None
    container: Optional[dict] = None          # the archive a tree walk is inside


def _clean_path(raw: Any) -> Optional[str]:
    if not isinstance(raw, str) or not raw.startswith("/") or "\x00" in raw or len(raw) > MAX_PATH:
        return None
    return raw


def url_path(url: str) -> str:
    """The decoded path of a URL as '/a/b', root '/'. Decoded before the split, as the
    crawler does, so an encoded separator is a level and not a character in a name."""
    segments = [s for s in unquote(urlsplit(url).path).split("/") if s]
    return "/" + "/".join(segments)


def _host_of(url: str) -> Optional[str]:
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    return host.lower() if host else None


def _size(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _text(value: Any, limit: int = 300) -> Optional[str]:
    return value[:limit] if isinstance(value, str) and value else None


def _iso(epoch: Any) -> Optional[str]:
    if isinstance(epoch, bool) or not isinstance(epoch, (int, float)):
        return None
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(epoch))
    except (OverflowError, OSError, ValueError):
        return None


def member_path(member: Any) -> Optional[str]:
    """An archive member's name as a safe relative path, or None.

    '..' and '.' segments are dropped, not resolved: the name is data about what the
    archive claims, and what matters here is that it can never read as a location outside
    the archive in anything built from it.
    """
    if not isinstance(member, str) or "\x00" in member:
        return None
    parts = [p for p in member.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    return "/".join(parts) if parts else None


class Ingester:
    def __init__(self, ix, rules: Optional[RuleSet] = None, batch: int = 5000):
        self.ix = ix
        self.conn = ix.conn
        self.rules = rules if rules is not None else load_rules()
        self.batch = batch
        self.summary = Summary()
        self._runs: dict[str, _Run] = {}
        self._targets: dict[str, int] = {}
        self._run_targets: set = set()
        self._prev: dict = {}
        self._pending = 0

    # ------------------------------------------------------------------ the door
    def apply(self, job_id: str, kind: str, data: Any) -> bool:
        """Take one event. Returns False if it was not something the index records."""
        if not isinstance(data, dict) or not isinstance(job_id, str) or not job_id:
            return False
        try:
            if kind in CRAWL_KINDS:
                return self._crawl_record(job_id, kind, data)
            if kind in TREE_KINDS:
                return self._tree_event(job_id, kind, data)
            if kind == "run.start":
                return self._run_start(job_id, data)
            if kind == "run.stop":
                return self._run_stop(job_id, data)
            if kind == "download.done":
                return self._custody(data)
        except (KeyError, TypeError, ValueError) as exc:
            logger.debug("skipping a %s record: %r", kind, exc)
        return False

    # ------------------------------------------------------------------ runs and targets
    def _run(self, job_id: str, mode: str, source: str) -> _Run:
        run = self._runs.get(job_id)
        if run is not None:
            return run
        row = self.conn.execute("SELECT id, mode, source, started_at, finished_at, "
                                "stopped_because FROM runs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            cur = self.conn.execute("INSERT INTO runs(job_id, mode, source) VALUES (?,?,?)",
                                    (job_id, mode, source))
            run = _Run(cur.lastrowid, mode, source)
        else:
            run = _Run(row[0], row[1], row[2], row[3], row[4], row[5])
        self._runs[job_id] = run
        return run

    def _stamp(self, run: _Run, when: Any) -> None:
        if isinstance(when, str) and when:
            if run.first is None or when < run.first:
                run.first = when
            if run.last is None or when > run.last:
                run.last = when

    def _target(self, host: str) -> int:
        tid = self._targets.get(host)
        if tid is None:
            row = self.conn.execute("SELECT id FROM targets WHERE host=?", (host,)).fetchone()
            tid = row[0] if row else self.conn.execute(
                "INSERT INTO targets(host) VALUES (?)", (host,)).lastrowid
            self._targets[host] = tid
        return tid

    def _covered(self, run: _Run, tid: int) -> Optional[int]:
        """Register that `run` covered target `tid`; return the target's run before it."""
        key = (run.row, tid)
        if key not in self._run_targets:
            prev = self.conn.execute(
                "SELECT MAX(run_id) FROM run_targets WHERE target_id=? AND run_id<?",
                (tid, run.row)).fetchone()[0]
            self.conn.execute("INSERT OR IGNORE INTO run_targets(run_id, target_id) VALUES (?,?)",
                              key)
            self._run_targets.add(key)
            self._prev[key] = prev
        return self._prev[key]

    # ------------------------------------------------------------------ run events
    def _run_start(self, job_id: str, data: dict) -> bool:
        mode = data.get("mode")
        if mode in ("crawl", "tree"):
            self._run(job_id, mode, mode)
            return True
        return False

    def _run_stop(self, job_id: str, data: dict) -> bool:
        run = self._runs.get(job_id) or self._lookup(job_id)
        if run is None:
            return False
        reason = data.get("stopped_because")
        if isinstance(reason, str) and reason:
            run.stopped = reason[:100]
        return True

    def _lookup(self, job_id: str) -> Optional[_Run]:
        row = self.conn.execute("SELECT 1 FROM runs WHERE job_id=?", (job_id,)).fetchone()
        return self._run(job_id, "crawl", "crawl") if row else None

    # ------------------------------------------------------------------ crawl
    def _crawl_record(self, job_id: str, kind: str, rec: dict) -> bool:
        if kind == "crawl.file":
            url = rec.get("url")
            if not isinstance(url, str):
                raise ValueError("no url")
            path = _clean_path(rec.get("path")) or _clean_path(url_path(url))
            host = _text(rec.get("host"), 255) or _host_of(url)
            name = _text(rec.get("name"), 255) or path.rsplit("/", 1)[-1]
            fetch = _text(rec.get("download_url"), 2000) or url
            size, mtime, entry_kind, when = (_size(rec.get("size_bytes")),
                                             _text(rec.get("mtime_text"), 100), "file",
                                             rec.get("discovered_at"))
        else:
            url = rec.get("final_url") if isinstance(rec.get("final_url"), str) else rec.get("url")
            if not isinstance(url, str):
                raise ValueError("no url")
            path = _clean_path(url_path(url))
            host = _host_of(url)
            name = path.rsplit("/", 1)[-1] or "/"
            fetch, size, mtime, entry_kind, when = url, None, None, "dir", rec.get("discovered_at")
        if path is None or not host:
            raise ValueError("no usable path or host")
        run = self._run(job_id, "crawl", "crawl")
        self._stamp(run, when)
        self._entry(run, host, path, name, size, mtime, entry_kind, "crawl", fetch[:2000])
        return True

    # ------------------------------------------------------------------ tree
    def _tree_event(self, job_id: str, kind: str, data: dict) -> bool:
        run = self._run(job_id, "tree", "tree")
        if kind == "tree.open":
            url = data.get("url")
            host = _host_of(url) if isinstance(url, str) else None
            if not host:
                raise ValueError("no archive url")
            parts = urlsplit(url)
            # A file manager serves every archive from one path and names it in the query,
            # so the query is part of the archive's identity here.
            base = url_path(url) + (f"?{parts.query}" if parts.query else "")
            run.container = {"url": url, "host": host, "path": base + "!"}
            self._stamp(run, data.get("at"))
            # The archive's own root, so a later walk can tell "the archive was re-walked"
            # from "the archive was never looked at": removals are only believed for the
            # first.
            self._entry(run, host, base + "!", os.path.basename(base) or base, None, None,
                        "dir", "tree", url[:2000])
            return True
        if kind == "tree.done":
            self._stamp(run, data.get("at"))
            return True
        box = run.container
        if box is None:
            raise ValueError("a tree.entry before any tree.open")
        member = member_path(data.get("path"))
        if member is None:
            raise ValueError("no usable member name")
        kind_name = data.get("kind") if isinstance(data.get("kind"), str) else "file"
        self._entry(run, box["host"], f"{box['path']}/{member}", member.rsplit("/", 1)[-1],
                    _size(data.get("size")), _iso(data.get("mtime")), kind_name[:20], "tree",
                    box["url"][:2000])
        return True

    # ------------------------------------------------------------------ the upsert
    def _entry(self, run: _Run, host: str, path: str, name: str, size: Optional[int],
               mtime: Optional[str], kind: str, source: str, fetch_url: str) -> None:
        conn = self.conn
        tid = self._target(host)
        prev = self._covered(run, tid)
        r = run.row
        row = conn.execute("SELECT id, first_run, last_run FROM entries WHERE target_id=? AND path=?",
                           (tid, path)).fetchone()
        if row is None:
            eid = conn.execute(
                "INSERT INTO entries(target_id, path, name, size, mtime_text, kind, source, "
                "fetch_url, first_run, last_run) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (tid, path, name, size, mtime, kind, source, fetch_url, r, r)).lastrowid
            conn.execute("INSERT INTO entry_versions(entry_id, run_id, size, last_run) "
                         "VALUES (?,?,?,?)", (eid, r, size, r))
            hits = self.rules.score_path(path)
            if hits:
                conn.executemany(
                    "INSERT INTO hits(entry_id, rule, score, reason, span_start, span_end) "
                    "VALUES (?,?,?,?,?,?)",
                    [(eid, h.rule, h.score, h.reason, h.start, h.end) for h in hits])
                self.summary.hits += len(hits)
            self.summary.entries_new += 1
        else:
            eid, first, last = row
            if conn.execute("SELECT 1 FROM entry_versions WHERE entry_id=? AND run_id<=? "
                            "AND last_run>=? LIMIT 1", (eid, r, r)).fetchone():
                self.summary.entries_seen += 1
                return
            # Only the newest sighting may change what the entry says about itself; a run
            # ingested late must not overwrite a newer one's size with an older one's.
            if r >= last:
                conn.execute(
                    "UPDATE entries SET first_run=MIN(first_run, ?), last_run=?, size=?, "
                    "mtime_text=?, name=?, kind=?, source=?, fetch_url=? WHERE id=?",
                    (r, r, size, mtime, name, kind, source, fetch_url, eid))
            else:
                conn.execute("UPDATE entries SET first_run=MIN(first_run, ?) WHERE id=?", (r, eid))
            extended = prev is not None and conn.execute(
                "UPDATE entry_versions SET last_run=? WHERE entry_id=? AND last_run=? "
                "AND size IS ?", (r, eid, prev, size)).rowcount
            if not extended:
                conn.execute("INSERT INTO entry_versions(entry_id, run_id, size, last_run) "
                             "VALUES (?,?,?,?)", (eid, r, size, r))
            self.summary.entries_seen += 1
        self._pending += 1
        if self._pending >= self.batch:
            self.flush()

    # ------------------------------------------------------------------ custody
    def _custody(self, rec: dict) -> bool:
        url, sha = rec.get("url"), rec.get("sha256")
        job = rec.get("job_id")
        if not (isinstance(url, str) and isinstance(sha, str) and len(sha) == 64
                and isinstance(job, str) and job):
            raise ValueError("not a custody record")
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO downloads(entry_id, url, sha256, size, etag, last_modified, "
            "job_id, custody_json) VALUES (NULL,?,?,?,?,?,?,?)",
            (url[:2000], sha, _size(rec.get("size")), _text(rec.get("etag")),
             _text(rec.get("last_modified")), job,
             json.dumps(rec, sort_keys=True, separators=(",", ":"))))
        self.summary.custody += cur.rowcount
        self._pending += 1
        return True

    # ------------------------------------------------------------------ commit
    def flush(self) -> None:
        """Write run times and stop reasons, link downloads to entries, and commit."""
        for job_id, run in self._runs.items():
            self.conn.execute(
                "UPDATE runs SET "
                "started_at = CASE WHEN started_at IS NULL OR (? IS NOT NULL AND ? < started_at) "
                "THEN ? ELSE started_at END, "
                "finished_at = CASE WHEN finished_at IS NULL OR (? IS NOT NULL AND ? > finished_at) "
                "THEN ? ELSE finished_at END, "
                "stopped_because = COALESCE(?, stopped_because) WHERE id=?",
                (run.first, run.first, run.first, run.last, run.last, run.last, run.stopped,
                 run.row))
        self.conn.execute(
            "UPDATE downloads SET entry_id=(SELECT e.id FROM entries e WHERE e.fetch_url="
            "downloads.url AND e.kind='file' ORDER BY e.id LIMIT 1) WHERE entry_id IS NULL")
        self.conn.commit()
        self._pending = 0


# ----------------------------------------------------------------------- the readers


def _lines(path: str):
    """JSON objects from a JSONL file, one per line; None for a line that is not one."""
    with open(path, "rb") as fh:
        for raw in fh:
            if not raw.strip():
                continue
            if len(raw) > MAX_LINE:
                yield None
                continue
            try:
                yield json.loads(raw)
            except ValueError:
                yield None


def _custody_files(root: str):
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            if name.endswith(CUSTODY_SUFFIX):
                yield os.path.join(dirpath, name)


def ingest_dir(ix, path: str, rules: Optional[RuleSet] = None) -> Summary:
    """A crawl output directory (and any custody records under it) into the index."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"{path} does not exist")
    if not os.path.isdir(path):
        raise NotADirectoryError(f"{path} is not a directory; tree output needs --archive-url")
    ing = Ingester(ix, rules)
    stats_path = os.path.join(path, "stats.json")
    stats: dict = {}
    if os.path.isfile(stats_path):
        try:
            with open(stats_path, encoding="utf-8") as fh:
                loaded = json.load(fh)
            stats = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            stats = {}
    listing, dirs = (os.path.join(path, n) for n in ("listing.jsonl", "dirs.jsonl"))
    is_crawl = os.path.isfile(listing) or os.path.isfile(dirs)
    job_id = stats.get("job_id") if isinstance(stats.get("job_id"), str) and stats.get("job_id") \
        else os.path.basename(os.path.abspath(path))
    if is_crawl:
        ing._run(job_id, "crawl", "crawl")
        for name, kind in ((dirs, "crawl.dir"), (listing, "crawl.file")):
            if os.path.isfile(name):
                for rec in _lines(name):
                    if rec is None or not ing.apply(job_id, kind, rec):
                        ing.summary.skipped += 1
        ing.apply(job_id, "run.stop", {"stopped_because": stats.get("stopped_because")})
    for custody in _custody_files(path):
        try:
            with open(custody, encoding="utf-8") as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            ing.summary.skipped += 1
            continue
        if not ing.apply(rec.get("job_id", "") if isinstance(rec, dict) else "", "download.done", rec):
            ing.summary.skipped += 1
    ing.flush()
    ing.summary.job_id = job_id if is_crawl else None
    return ing.summary


def ingest_ndjson(ix, path: str, *, archive_url: str, job_id: Optional[str] = None,
                  rules: Optional[RuleSet] = None, at: Optional[str] = None,
                  stopped_because: Optional[str] = None) -> Summary:
    """rvtree `-f ndjson` output for one archive. rvtree does not say which archive it
    walked, so the caller must."""
    if not archive_url or _host_of(archive_url) is None:
        raise ValueError("tree output needs the archive it came from (archive URL)")
    st = os.stat(path)
    job = job_id or "tree-" + hashlib.sha256(
        f"{archive_url}|{st.st_mtime_ns}|{st.st_size}".encode()).hexdigest()[:12]
    ing = Ingester(ix, rules)
    ing.apply(job, "run.start", {"mode": "tree"})
    ing.apply(job, "tree.open", {"url": archive_url, "at": at})
    for rec in _lines(path):
        if rec is None or not ing.apply(job, "tree.entry", rec):
            ing.summary.skipped += 1
    ing.apply(job, "tree.done", {"url": archive_url, "at": at})
    ing.apply(job, "run.stop", {"stopped_because": stopped_because})
    ing.flush()
    ing.summary.job_id = job
    return ing.summary
