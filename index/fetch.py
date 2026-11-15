"""Selective fetch: hand the best hits to the download engine, and archive members to rvtree.

`select` is the question -- which entries score at least N, are files, and have not been
downloaded -- and `run` is the act. The act is injected: this module does not import the
download engine or rvtree, so the index stays standard-library-only and testable without
a network. OnionAccelerator.py supplies the real `download` (the engine's
multi_download_mode) and `extract` (rvtree's `extract`, bounded by --max-fetch).

A hit inside an archive is never downloaded whole: the archive is far larger than the
member, and rvtree reads the member with range requests. `extract_dest` decides where it
lands, and a member name is hostile input -- a path of '../../x' or an absolute path must
not leave the output directory -- so every component is sanitised and the result checked.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import time
from typing import Callable, Optional

from .ingest import Ingester
from .rules import RuleSet

_UNSAFE = re.compile(r'[\x00-\x1f/\\:*?"<>|]')
MAX_COMPONENT = 120


@dataclasses.dataclass
class Candidate:
    entry_id: int
    host: str
    path: str
    url: str
    size: Optional[int]
    score: int
    kind: str
    source: str
    container_url: Optional[str] = None
    member: Optional[str] = None


@dataclasses.dataclass
class FetchSummary:
    selected: list
    dry_run: bool = False
    downloaded: list = dataclasses.field(default_factory=list)
    ok: int = 0
    failed: int = 0
    failures: list = dataclasses.field(default_factory=list)


def select(ix, min_score: int, host: Optional[str] = None, limit: Optional[int] = None,
           again: bool = False) -> list:
    """Files scoring at least `min_score`, best first, one per thing that would be fetched."""
    sql = ("SELECT e.id, t.host, e.path, e.fetch_url, e.size, m.best, e.kind, e.source "
           "FROM (SELECT entry_id, MAX(score) AS best FROM hits GROUP BY entry_id "
           "HAVING best >= ?) m JOIN entries e ON e.id = m.entry_id "
           "JOIN targets t ON t.id = e.target_id WHERE e.kind = 'file'")
    args: list = [min_score]
    if host:
        sql += " AND t.host = ?"
        args.append(host)
    if not again:
        sql += " AND NOT EXISTS (SELECT 1 FROM downloads d WHERE d.entry_id = e.id)"
    sql += " ORDER BY m.best DESC, t.host, e.path"
    chosen, seen = [], set()
    for eid, h, path, url, size, best, kind, source in ix.conn.execute(sql, args):
        if not url:
            continue
        cand = Candidate(eid, h, path, url, size, best, kind, source)
        if source == "tree" and "!/" in path:
            cand.container_url, cand.member = url, path.split("!/", 1)[1]
            key = (url, cand.member)
        else:
            key = (url, None)
        if key in seen:                       # two browse paths, one download URL
            continue
        seen.add(key)
        chosen.append(cand)
        if limit and len(chosen) >= limit:
            break
    return chosen


def _component(text: str) -> str:
    return _UNSAFE.sub("_", text).strip(". ")[:MAX_COMPONENT]


def _segments(text: str) -> list:
    parts = [_component(p) for p in text.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    return [p for p in parts if p]


def extract_dest(base_dir: str, host: str, container_path: str, member: str) -> str:
    """Where an extracted member lands: under base/<host>/<archive>.members/<member path>.

    Every component is sanitised, '.' and '..' are dropped, and the result is checked to be
    inside `base_dir` -- the invariant is that a hostile archive member name can never
    write outside the output directory, whatever it says.
    """
    parts = [_component(host) or "unknown-host"]
    archive = _segments(container_path.split("!", 1)[0])
    if archive:
        archive[-1] += ".members"
    parts += archive or ["archive.members"]
    parts += _segments(member) or ["_unnamed"]
    dest = os.path.join(base_dir, *parts)
    root = os.path.realpath(base_dir)
    if not os.path.realpath(dest).startswith(root + os.sep):
        raise ValueError(f"refusing an extraction path outside {base_dir}: {dest!r}")
    return dest


def _sha256(path: str) -> Optional[str]:
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                digest.update(block)
        return digest.hexdigest()
    except OSError:
        return None


def run(ix, *, min_score: int, download: Callable, extract: Callable, out_dir: str,
        host: Optional[str] = None, limit: Optional[int] = None, again: bool = False,
        dry_run: bool = False, max_fetch_mib: int = 512,
        engine_options: Optional[dict] = None, job_id: Optional[str] = None) -> FetchSummary:
    """Fetch what `select` chooses. `download(urls, **options)` returns one result per URL
    (`.url`, `.ok`, `.reason`, `.custody`); `extract(archive_url, member, dest, mib)` -> bool.
    """
    chosen = select(ix, min_score, host=host, limit=limit, again=again)
    summary = FetchSummary(selected=chosen, dry_run=dry_run)
    if dry_run or not chosen:
        return summary
    job = job_id or time.strftime("fetch-%Y%m%d_%H%M%S")
    ing = Ingester(ix, RuleSet(()))                 # custody only: nothing here is scored

    files = [c for c in chosen if c.container_url is None]
    if files:
        options = dict(engine_options or {})
        options["preserve_path"] = True
        results = download([c.url for c in files], **options)
        for res in results:
            if res.ok:
                summary.ok += 1
                if res.custody:
                    ing.apply(job, "download.done", res.custody)
            else:
                summary.failed += 1
                summary.failures.append(f"{res.url}: {res.reason or 'failed'}")
        summary.downloaded = list(results)

    for cand in (c for c in chosen if c.container_url is not None):
        dest = extract_dest(out_dir, cand.host, cand.path, cand.member)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if extract(cand.container_url, cand.member, dest, max_fetch_mib):
            summary.ok += 1
            sha = _sha256(dest)
            if sha:
                size = os.path.getsize(dest)
                ing.conn.execute(
                    "INSERT OR IGNORE INTO downloads(entry_id, url, sha256, size, job_id, "
                    "custody_json) VALUES (?,?,?,?,?,?)",
                    (cand.entry_id, f"{cand.container_url}!/{cand.member}", sha, size, job,
                     json.dumps({"kind": "extract", "path": dest})))
        else:
            summary.failed += 1
            summary.failures.append(f"{cand.container_url} ({cand.member}): extract failed")
    ing.flush()
    return summary

