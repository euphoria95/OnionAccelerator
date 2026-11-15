"""Hits out of the index: JSONL for machines, CSV for people.

Both carry the same facts per entry -- score, where it is, who it is about, which rules
fired and why, which runs saw it, and the SHA-256 of anything downloaded. JSONL keeps the
hits as a list; CSV flattens them to `rules` and `reasons` columns.

CSV cells are defused. Every path in this index was written by the server being
investigated, and the file this module writes is the one an analyst double-clicks into a
spreadsheet; a cell beginning with = + - @ (or a tab or carriage return) is a formula to
that program. Such cells are prefixed with an apostrophe, the convention spreadsheets
read as "this is text". JSONL is not touched: a consumer of JSON is a program, not a person
with a mouse.

The file is created 0600 and not overwritten without `force`: an export is evidence too.
"""

from __future__ import annotations

import csv
import json
import os
from typing import Optional, TextIO

from . import query

FORMATS = ("jsonl", "csv")
CSV_COLUMNS = ["score", "host", "label", "group", "victim", "path", "size", "kind", "source",
               "url", "rules", "reasons", "first_run", "last_run", "sha256"]
FORMULA_LEADERS = ("=", "+", "-", "@", "\t", "\r")


def defuse(value) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(FORMULA_LEADERS) else text


def _downloads(ix, entry_ids: list) -> dict:
    out: dict = {i: [] for i in entry_ids}
    for start in range(0, len(entry_ids), 500):
        chunk = entry_ids[start:start + 500]
        marks = ",".join("?" * len(chunk))
        for eid, sha, size, etag, modified, job in ix.conn.execute(
                f"SELECT entry_id, sha256, size, etag, last_modified, job_id FROM downloads "
                f"WHERE entry_id IN ({marks}) ORDER BY id", chunk):
            out[eid].append({"sha256": sha, "size": size, "etag": etag,
                             "last_modified": modified, "job_id": job})
    return out


def export_hits(ix, fmt: str, out: TextIO, *, min_score: int = 1,
                host: Optional[str] = None) -> int:
    """Write every entry scoring at least `min_score`, best first. Returns how many."""
    if fmt not in FORMATS:
        raise ValueError(f"unknown export format {fmt!r}; choose from {', '.join(FORMATS)}")
    entries = query.top_hits(ix, min_score=min_score, limit=0, host=host)
    downloads = _downloads(ix, [e["id"] for e in entries])
    writer = None
    if fmt == "csv":
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(CSV_COLUMNS)
    for e in entries:
        got = downloads[e["id"]]
        if writer is not None:
            writer.writerow([defuse(c) for c in (
                e["score"], e["host"], e["label"], e["group"], e["victim"], e["path"], e["size"],
                e["kind"], e["source"], e["url"],
                "; ".join(h["rule"] for h in e["hits"]),
                "; ".join(h["reason"] for h in e["hits"]),
                e["first_run"], e["last_run"], got[0]["sha256"] if got else "")])
        else:
            out.write(json.dumps({
                "score": e["score"], "host": e["host"], "label": e["label"], "group": e["group"],
                "victim": e["victim"], "path": e["path"], "size": e["size"], "kind": e["kind"],
                "source": e["source"], "url": e["url"], "first_run": e["first_run"],
                "last_run": e["last_run"], "hits": e["hits"], "downloads": got,
            }, ensure_ascii=False, sort_keys=False) + "\n")
    return len(entries)


def export_to_path(ix, fmt: str, path: str, *, min_score: int = 1, host: Optional[str] = None,
                   force: bool = False) -> int:
    """Export to a new 0600 file. An existing file is an error unless `force`."""
    if fmt not in FORMATS:
        raise ValueError(f"unknown export format {fmt!r}; choose from {', '.join(FORMATS)}")
    parent = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(parent):
        os.makedirs(parent)
        os.chmod(parent, 0o700)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if force else os.O_EXCL)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        raise FileExistsError(f"{path} exists; pass --force to overwrite it") from None
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
        return export_hits(ix, fmt, fh, min_score=min_score, host=host)
