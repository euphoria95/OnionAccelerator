"""Reading the index: path search and the best hits."""

from __future__ import annotations

import re
import sqlite3
from typing import Optional


class QueryError(ValueError):
    """A query the index cannot run, with what was wrong with it."""


def _entry_row(r) -> dict:
    return {"id": r[0], "host": r[1], "path": r[2], "size": r[3], "kind": r[4], "source": r[5],
            "url": r[6], "first_run": r[7], "last_run": r[8], "label": r[9], "group": r[10],
            "victim": r[11]}


_ENTRY_COLUMNS = ("e.id, t.host, e.path, e.size, e.kind, e.source, e.fetch_url, "
                  "fr.job_id, lr.job_id, t.label, t.grp, t.victim")
_ENTRY_JOINS = ("JOIN targets t ON t.id = e.target_id JOIN runs fr ON fr.id = e.first_run "
                "JOIN runs lr ON lr.id = e.last_run")


_WORD = re.compile(r"[^\W_]+\*?")


def plain_query(text: str) -> str:
    """What the user typed as an FTS5 query: every word must be in the path.

    The index tokenises paths on anything that is not a letter or digit, so
    /Backups/Payroll_2024 is the words backups, payroll and 2024 -- and the search must
    read its input the same way. Pasted into FTS5 as it is, a path is a syntax error
    ('-' is an operator there, '/' and '.' are not allowed), and a search box that rejects
    the thing it exists to search for is not worth having. A trailing '*' is a prefix.
    """
    words = _WORD.findall(text)
    return " ".join(f'"{w[:-1]}"*' if w.endswith("*") else f'"{w}"' for w in words)


def search(ix, pattern: str, limit: int = 1000, host: Optional[str] = None,
           raw: bool = False) -> list:
    """Entries whose path contains every word of `pattern` (a trailing * makes a prefix).

    With raw=True the pattern is FTS5 syntax as it is -- "a phrase", AND / OR / NOT, NEAR --
    and one that does not parse is a QueryError.
    """
    query = pattern if raw else plain_query(pattern)
    if not query.strip():
        return []
    sql = (f"SELECT {_ENTRY_COLUMNS} FROM entries_fts JOIN entries e ON e.id = entries_fts.rowid "
           f"{_ENTRY_JOINS} WHERE entries_fts MATCH ?")
    args: list = [query]
    if host:
        sql += " AND t.host = ?"
        args.append(host)
    sql += " ORDER BY e.id LIMIT ?"
    args.append(limit)
    try:
        return [_entry_row(r) for r in ix.conn.execute(sql, args)]
    except sqlite3.OperationalError as exc:
        raise QueryError(f"cannot run that search ({exc})") from exc


def hits_for(ix, entry_ids: list) -> dict:
    out: dict = {i: [] for i in entry_ids}
    for start in range(0, len(entry_ids), 500):
        chunk = entry_ids[start:start + 500]
        marks = ",".join("?" * len(chunk))
        for eid, rule, score, reason, a, b in ix.conn.execute(
                f"SELECT entry_id, rule, score, reason, span_start, span_end FROM hits "
                f"WHERE entry_id IN ({marks}) ORDER BY score DESC, rule", chunk):
            out[eid].append({"rule": rule, "score": score, "reason": reason, "span": [a, b]})
    return out


def top_hits(ix, min_score: int = 1, limit: int = 100, host: Optional[str] = None) -> list:
    """Entries by their best hit, highest first; each carries all of its hits."""
    sql = (f"SELECT {_ENTRY_COLUMNS}, m.best FROM (SELECT entry_id, MAX(score) AS best FROM hits "
           f"GROUP BY entry_id HAVING best >= ?) m JOIN entries e ON e.id = m.entry_id "
           f"{_ENTRY_JOINS}")
    args: list = [min_score]
    if host:
        sql += " WHERE t.host = ?"
        args.append(host)
    sql += " ORDER BY m.best DESC, t.host, e.path"
    if limit:
        sql += " LIMIT ?"
        args.append(limit)
    rows = ix.conn.execute(sql, args).fetchall()
    out = []
    by_id = hits_for(ix, [r[0] for r in rows])
    for r in rows:
        entry = _entry_row(r)
        entry["score"] = r[12]
        entry["hits"] = by_id[r[0]]
        out.append(entry)
    return out
