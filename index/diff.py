"""What changed on a target between two sightings of it.

Finding what is new is easy. The care here goes into not claiming a file was *removed*
when the later crawl simply never looked where it lived. A crawl that was cut short, or
gave up on a directory, has said nothing about that directory's contents.

For an entry present at `since` and absent at `until`:

* if its parent directory was listed at `until`, the entry is **removed**: the directory
  was read and it was not in it. A directory that was itself absent is removed only if the
  later run is *complete* (stopped_because == "completed", which the crawler reports only
  when no subtree was abandoned) -- otherwise it is **unknown**, because the crawl may
  never have reached it.
* if the parent was not listed at `until`, the entry inherits its parent's verdict: gone
  with a removed directory, unknown beside an unknown one.
* members of an archive follow the archive: removed if the archive was walked again at
  `until`, unknown if it was not.

The index sees paths, not intent. A directory renamed from /old to /new is reported as
what it is to the index -- /old removed, /new added -- and the same names and sizes on both
sides are the tell.
"""

from __future__ import annotations

import dataclasses
from typing import Optional


class DiffError(ValueError):
    """The diff cannot be made, and why."""


@dataclasses.dataclass
class DiffResult:
    host: str
    since_job: str
    until_job: str
    added: list
    removed: list
    resized: list
    unknown: list
    warnings: list


def _runs_of(ix, tid: int) -> list:
    """(run id, job id, stopped_because) of every run that covered the target, oldest first."""
    return ix.conn.execute(
        "SELECT r.id, r.job_id, r.stopped_because FROM run_targets rt JOIN runs r ON r.id=rt.run_id "
        "WHERE rt.target_id=? ORDER BY r.id", (tid,)).fetchall()


def _parent(path: str, source: str) -> Optional[str]:
    if source == "tree":
        return path.split("!", 1)[0] + "!"                    # the archive's root
    if path == "/":
        return None
    return path.rsplit("/", 1)[0] or "/"


def diff(ix, host: str, since: Optional[str] = None, until: Optional[str] = None) -> DiffResult:
    conn = ix.conn
    row = conn.execute("SELECT id FROM targets WHERE host=?", (host,)).fetchone()
    if row is None:
        raise DiffError(f"no target {host!r} in the index")
    tid = row[0]
    runs = _runs_of(ix, tid)
    by_job = {job: (rid, stopped) for rid, job, stopped in runs}

    def pick(name: Optional[str], default_index: int, what: str):
        if name is None:
            return runs[default_index]
        if name not in by_job:
            exists = conn.execute("SELECT 1 FROM runs WHERE job_id=?", (name,)).fetchone()
            raise DiffError(f"run {name!r} did not see {host}" if exists
                            else f"no run {name!r} in the index")
        return (by_job[name][0], name, by_job[name][1])

    if len(runs) < 2 and (since is None or until is None):
        raise DiffError(f"{host} has only one sighting ({runs[0][1] if runs else 'none'}); "
                        f"there is nothing to compare it with")
    new = pick(until, -1, "until")
    if since is None:
        older = [r for r in runs if r[0] < new[0]]
        if not older:
            raise DiffError(f"{host} has only one sighting up to {new[1]}; nothing to compare")
        old = older[-1]
    else:
        old = pick(since, -2, "since")
    if old[0] >= new[0]:
        raise DiffError(f"--since {old[1]} is not older than {new[1]}")
    (a, since_job, _), (b, until_job, until_stop) = old, new
    complete = until_stop == "completed"

    present = ("EXISTS (SELECT 1 FROM entry_versions v WHERE v.entry_id=e.id AND v.run_id<=? "
               "AND v.last_run>=?)")
    size_at = ("(SELECT v.size FROM entry_versions v WHERE v.entry_id=e.id AND v.run_id<=? "
               "AND v.last_run>=? ORDER BY v.run_id DESC LIMIT 1)")
    base = "SELECT e.id, e.path, e.name, e.kind, e.source, e.size FROM entries e WHERE e.target_id=? "

    def record(r, **extra):
        out = {"path": r[1], "name": r[2], "kind": r[3], "source": r[4], "size": r[5]}
        out.update(extra)
        return out

    added = [record(r) for r in conn.execute(
        base + f"AND {present} AND NOT {present} ORDER BY e.path", (tid, b, b, a, a))]

    resized = []
    for r in conn.execute(
            base + f"AND {present} AND {present} ORDER BY e.path", (tid, a, a, b, b)):
        old_size = conn.execute(f"SELECT {size_at.replace('e.id', '?')}", (r[0], a, a)).fetchone()[0]
        new_size = conn.execute(f"SELECT {size_at.replace('e.id', '?')}", (r[0], b, b)).fetchone()[0]
        if old_size != new_size and r[3] == "file":
            resized.append(record(r, old_size=old_size, size=new_size))

    gone = conn.execute(base + f"AND {present} AND NOT {present} ORDER BY LENGTH(e.path), e.path",
                        (tid, a, a, b, b)).fetchall()
    listed_at_b = {p for (p,) in conn.execute(
        "SELECT e.path FROM entries e WHERE e.target_id=? AND e.kind='dir' AND " + present,
        (tid, b, b))}
    verdict: dict = {}
    removed, unknown = [], []
    for r in gone:                                # shallowest first, so a parent is judged first
        path, kind, source = r[1], r[3], r[4]
        parent = _parent(path, source)
        if source == "tree" or parent is None:
            state = "removed" if parent in listed_at_b else "unknown"
        elif parent in listed_at_b:
            state = "removed" if (kind == "file" or complete) else "unknown"
        else:
            state = verdict.get(parent, "unknown")
        verdict[path] = state
        (removed if state == "removed" else unknown).append(record(r))
    removed.sort(key=lambda e: e["path"])
    unknown.sort(key=lambda e: e["path"])

    warnings = []
    if not complete:
        warnings.append(f"run {until_job} stopped because {until_stop or 'unknown'}, not "
                        f"'completed': directories it did not list are reported as unknown, "
                        f"not removed")
    return DiffResult(host, since_job, until_job, added, removed, resized, unknown, warnings)
