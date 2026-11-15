"""Synthetic crawl output and stream events for the index tests.

Every record here has the shape crawler/report.py writes -- the fields are copied from
record_listing() and record_file() -- so an ingester that passes against these reads a
real crawl directory. tests/test_index_ingest.py also ingests the output of a real crawl
against a local server, which is what keeps these shapes honest.

Every host, organisation and path below is synthetic: `*.test` and `*.example` names, and
short onion-shaped labels that are not valid addresses. Nothing here names a real target.
"""

from __future__ import annotations

import json
import os
from typing import Iterable, Optional

HOST_A = "alpha.test"
HOST_B = "bravo.test"


def _stamp(n: int) -> str:
    return f"2026-11-{10 + n // 86400:02d}T{(n // 3600) % 24:02d}:{(n // 60) % 60:02d}:{n % 60:02d}Z"


def file_record(host, path, size=None, *, mtime="2025-01-02 03:04", at=0, download_url=None,
                scheme="http", parent=None, depth=1):
    url = f"{scheme}://{host}{path}"
    return {"url": url, "host": host, "path": path, "name": path.rsplit("/", 1)[-1],
            "size_bytes": size, "mtime_text": mtime, "download_url": download_url,
            "depth": depth, "parent": parent, "http_status": None, "content_type": None,
            "endpoint": "127.0.0.1:5000", "discovered_at": _stamp(at)}


def dir_record(host, path, *, at=0, scheme="http", depth=0):
    url = f"{scheme}://{host}{path.rstrip('/')}/"
    return {"url": url, "final_url": url, "status": 200, "endpoint": "127.0.0.1:5000",
            "bytes": 1234, "depth": depth, "parent": None, "attempts": 1, "n_dirs": 0,
            "n_files": 0, "is_index": True, "confidence": 0.9, "profile": "apache",
            "server": "test", "title": "Index of " + path, "discovered_at": _stamp(at)}


def write_crawl_dir(root, job_id, *, host=HOST_A, files: Iterable = (), dirs: Iterable = ("/",),
                    stopped_because="completed", at=0, extra_files: Iterable = ()):
    """A crawl output directory. `files` are (path, size) or (path, size, mtime) tuples.

    Every directory a file sits in is listed too, as a real crawl would have: a file is
    only ever discovered *by listing its directory*.
    """
    out = os.path.join(str(root), job_id)
    os.makedirs(out, exist_ok=True)
    files = [tuple(f) for f in files]
    listed = {d.rstrip("/") or "/" for d in dirs}
    for path, *_ in files:
        parent = path.rsplit("/", 1)[0] or "/"
        listed.add(parent)
    with open(os.path.join(out, "dirs.jsonl"), "w", encoding="utf-8") as fh:
        for i, d in enumerate(sorted(listed)):
            fh.write(json.dumps(dir_record(host, d, at=at + i)) + "\n")
    with open(os.path.join(out, "listing.jsonl"), "w", encoding="utf-8") as fh:
        for i, f in enumerate(files):
            path, size = f[0], f[1]
            mtime = f[2] if len(f) > 2 else "2025-01-02 03:04"
            fh.write(json.dumps(file_record(host, path, size, mtime=mtime, at=at + 100 + i)) + "\n")
        for rec in extra_files:
            fh.write(json.dumps(rec) + "\n")
    with open(os.path.join(out, "stats.json"), "w", encoding="utf-8") as fh:
        json.dump({"job_id": job_id, "stopped_because": stopped_because,
                   "totals": {"directories": len(listed), "files": len(files)}}, fh)
    return out


def tree_entries(members: Iterable):
    """rvtree `-f ndjson` records: (path, size) or (path, size, kind)."""
    out = []
    for m in members:
        path, size = m[0], m[1]
        kind = m[2] if len(m) > 2 else "file"
        out.append({"path": path, "size": size, "kind": kind, "mtime": 1700000000.0})
    return out


def write_ndjson(path, records):
    with open(path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
    return str(path)


def crawl_events(job_dir, *, mode="crawl") -> list:
    """The (kind, data) pairs a live run would have published for a crawl directory.

    The stream's contract is that `data` is byte-for-byte the record on disk, so this is
    just the directory read back as events -- dirs first, then files, then the stop, in
    the order a crawl produces them.
    """
    events = []
    for name, kind in (("dirs.jsonl", "crawl.dir"), ("listing.jsonl", "crawl.file")):
        with open(os.path.join(job_dir, name), encoding="utf-8") as fh:
            events += [(kind, json.loads(line)) for line in fh if line.strip()]
    stats = json.load(open(os.path.join(job_dir, "stats.json")))
    events.append(("run.stop", {"mode": mode, "stopped_because": stats["stopped_because"],
                                "totals": stats["totals"]}))
    return events


def dump(ix, ignore: Optional[Iterable[str]] = ()) -> dict:
    """Every table as rows with surrogate ids replaced by natural keys, order-independent.

    Two indexes are "row-for-row equal" when this is. Ids are an accident of insertion
    order -- a live consumer sees dirs and files interleaved, an offline ingest reads one
    file and then the other -- so comparing them would fail for a reason that is not a
    difference in what was indexed.
    """
    c = ix.conn
    skip = set(ignore)
    run = {r[0]: r[1] for r in c.execute("SELECT id, job_id FROM runs")}
    host = {r[0]: r[1] for r in c.execute("SELECT id, host FROM targets")}
    entry = {r[0]: (host[r[1]], r[2]) for r in c.execute("SELECT id, target_id, path FROM entries")}
    out = {}
    out["runs"] = sorted(c.execute(
        "SELECT job_id, mode, source, started_at, finished_at, stopped_because FROM runs"))
    out["targets"] = sorted(c.execute("SELECT host, label, grp, victim FROM targets"),
                            key=repr)
    out["run_targets"] = sorted((run[r], host[t]) for r, t in c.execute(
        "SELECT run_id, target_id FROM run_targets"))
    out["entries"] = sorted((
        host[t], p, n, s, m, k, src, fu, run[fr], run[lr])
        for t, p, n, s, m, k, src, fu, fr, lr in c.execute(
        "SELECT target_id, path, name, size, mtime_text, kind, source, fetch_url, first_run, "
        "last_run FROM entries"))
    out["entry_versions"] = sorted((*entry[e], run[r], s, run[lr]) for e, r, s, lr in c.execute(
        "SELECT entry_id, run_id, size, last_run FROM entry_versions"))
    out["hits"] = sorted((*entry[e], rule, score, reason, a, b) for e, rule, score, reason, a, b
                         in c.execute("SELECT entry_id, rule, score, reason, span_start, span_end "
                                      "FROM hits"))
    out["downloads"] = sorted(
        ((entry.get(e), u, sha, sz, et, lm, job) for e, u, sha, sz, et, lm, job in c.execute(
            "SELECT entry_id, url, sha256, size, etag, last_modified, job_id FROM downloads")),
        key=repr)
    return {k: v for k, v in out.items() if k not in skip}
