"""`--mode index ACTION ...`: what the index can be asked to do.

    ingest PATH...      crawl directories, custody records, or rvtree ndjson (--archive-url)
    live                attach to a running job's --stream (--stream-url) and index it as it goes
    diff                what changed on a target between two sightings (--target)
    search WORDS        which paths contain these words
    hits                the best-scoring entries, with why (--explain)
    runs                every run the index knows
    target HOST         label a target, or show it (--label --group --victim)
    rescore             apply the current rules and scope to everything already indexed
    export              JSONL or CSV of the hits
    fetch               hand hits at or above a score to the download engine / rvtree extract

Exit codes: 0 done, 1 the action could not be completed (nothing found to diff, a path that
does not exist, a download that failed), 2 usage.

The index file is --index, else $ONIONACCELERATOR_INDEX, else the user's data directory.
The operator's scope file is --scope, else $ONIONACCELERATOR_SCOPE, else a scope.toml next
to the index file when there is one. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional, Sequence

from . import diff as diffmod
from . import export as exportmod
from . import fetch as fetchmod
from . import live as livemod
from . import query, store
from .ingest import ingest_dir, ingest_ndjson
from .rules import RuleError, load_rules, rescore

ACTIONS = ("ingest", "live", "diff", "search", "hits", "runs", "target", "rescore", "export",
           "fetch")


class UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    """argparse that raises instead of exiting, so the caller owns the exit code."""

    def error(self, message):
        raise UsageError(message)

    def exit(self, status=0, message=None):
        raise UsageError(message or "")


def _parser() -> argparse.ArgumentParser:
    top = _Parser(prog="--mode index", description=__doc__.split("\n\n")[0], add_help=False)
    sub = top.add_subparsers(dest="action", parser_class=_Parser)

    def add(name, help_):
        return sub.add_parser(name, help=help_, add_help=False)

    scope = argparse.ArgumentParser(add_help=False)
    scope.add_argument("--scope", metavar="FILE")

    p = sub.add_parser("ingest", parents=[scope], add_help=False)
    p.add_argument("paths", nargs="+", metavar="PATH")
    p.add_argument("--archive-url", metavar="URL")
    p.add_argument("--job-id", metavar="ID")
    p.add_argument("--partial", action="store_true")

    p = sub.add_parser("live", parents=[scope], add_help=False)
    p.add_argument("--stream-url", required=True, metavar="URL")
    p.add_argument("--token", metavar="TOKEN")
    p.add_argument("--reconcile", metavar="DIR")
    p.add_argument("--follow", action="store_true")

    p = add("diff", "")
    p.add_argument("--target", required=True, metavar="HOST")
    p.add_argument("--since", metavar="RUN")
    p.add_argument("--until", metavar="RUN")
    p.add_argument("--format", choices=("text", "json"), default="text")

    p = add("search", "")
    p.add_argument("words", nargs="+", metavar="WORD")
    p.add_argument("--fts", action="store_true")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--target", metavar="HOST")
    p.add_argument("--format", choices=("text", "json"), default="text")

    p = add("hits", "")
    p.add_argument("--min-score", type=int, default=1, metavar="N")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--target", metavar="HOST")
    p.add_argument("--explain", action="store_true")
    p.add_argument("--format", choices=("text", "json"), default="text")

    add("runs", "")

    p = add("target", "")
    p.add_argument("host")
    p.add_argument("--label")
    p.add_argument("--group", dest="grp")
    p.add_argument("--victim")

    sub.add_parser("rescore", parents=[scope], add_help=False)

    p = add("export", "")
    p.add_argument("--format", choices=exportmod.FORMATS, required=True)
    p.add_argument("--min-score", type=int, default=1, metavar="N")
    p.add_argument("--target", metavar="HOST")
    p.add_argument("--out", metavar="FILE")
    p.add_argument("--force", action="store_true")

    p = add("fetch", "")
    p.add_argument("--min-score", type=int, required=True, metavar="N")
    p.add_argument("--target", metavar="HOST")
    p.add_argument("--limit", type=int)
    p.add_argument("--again", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--max-fetch", type=int, default=64, metavar="MiB")
    return top


def _rules(args, index_path: str):
    path = (getattr(args, "scope", None) or os.environ.get("ONIONACCELERATOR_SCOPE")
            or None)
    if path is None:
        beside = os.path.join(os.path.dirname(index_path), "scope.toml")
        path = beside if os.path.isfile(beside) else None
    return load_rules(scope_path=path)


def _say(out, text=""):
    out.write(text + "\n")


def main(argv: Sequence[str], *, index_path: Optional[str] = None, hooks=None,
         out=None, err=None) -> int:
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    argv = list(argv)
    if not argv or argv[0] not in ACTIONS:
        what = f"unknown action {argv[0]!r}" if argv else "no action given"
        _say(err, f"--mode index: {what}; choose one of: {', '.join(ACTIONS)}")
        return 2
    try:
        args = _parser().parse_args(argv)
    except UsageError as exc:
        _say(err, f"--mode index {argv[0]}: {exc}")
        return 2

    path = index_path or store.default_index_path()
    try:
        with store.Index.open(path) as ix:
            return _dispatch(args, ix, path, hooks, out, err)
    except store.IndexStoreError as exc:
        _say(err, f"index: {exc}")
        return 1
    except RuleError as exc:
        _say(err, f"rules: {exc}")
        return 1
    except UsageError as exc:
        _say(err, f"--mode index {args.action}: {exc}")
        return 2


def _dispatch(args, ix, path, hooks, out, err) -> int:
    action = args.action
    if action == "ingest":
        return _ingest(args, ix, path, out, err)
    if action == "live":
        try:
            summary = livemod.follow(ix, args.stream_url, token=args.token,
                                     rules=_rules(args, path), reconcile=args.reconcile,
                                     keep_following=args.follow)
        except livemod.StreamError as exc:
            _say(err, f"live: {exc}")
            return 1
        _say(out, f"indexed {summary.events} event(s) from run(s) {', '.join(summary.runs) or '-'}"
                  f"; {summary.gaps} gap(s) lost {summary.lost} event(s), "
                  f"{len(summary.reconciled)} mended from the run's directory")
        return 1 if summary.lost and not summary.reconciled else 0
    if action == "diff":
        return _diff(args, ix, out, err)
    if action == "search":
        try:
            rows = query.search(ix, " ".join(args.words), limit=args.limit, host=args.target,
                                raw=args.fts)
        except query.QueryError as exc:
            raise UsageError(f"search: {exc}") from exc
        if args.format == "json":
            _say(out, json.dumps(rows))
        else:
            for r in rows:
                _say(out, f"{r['host']}{r['path']}  {r['kind']}  "
                          f"{'' if r['size'] is None else r['size']}")
        return 0
    if action == "hits":
        rows = query.top_hits(ix, min_score=args.min_score, limit=args.limit, host=args.target)
        if args.format == "json":
            _say(out, json.dumps(rows))
            return 0
        for r in rows:
            _say(out, f"{r['score']:3d}  {r['host']}{r['path']}  "
                      f"[{', '.join(h['rule'] for h in r['hits'])}]")
            if args.explain:
                for h in r["hits"]:
                    a, b = h["span"]
                    _say(out, f"       {h['score']:3d} {h['rule']}: {h['reason']}  "
                              f"(matched {r['path'][a:b]!r})")
        return 0
    if action == "runs":
        for job, mode, started, finished, why, n in ix.conn.execute(
                "SELECT r.job_id, r.mode, r.started_at, r.finished_at, r.stopped_because, "
                "(SELECT COUNT(*) FROM run_targets rt WHERE rt.run_id=r.id) FROM runs r "
                "ORDER BY r.id"):
            _say(out, f"{job}  {mode}  {started or '-'} .. {finished or '-'}  "
                      f"{why or 'no stop reason'}  {n} target(s)")
        return 0
    if action == "target":
        return _target(args, ix, out, err)
    if action == "rescore":
        counts = rescore(ix, _rules(args, path))
        _say(out, f"rescored {counts.entries} entries: {counts.hits} hit(s)")
        return 0
    if action == "export":
        try:
            if args.out:
                n = exportmod.export_to_path(ix, args.format, args.out, min_score=args.min_score,
                                             host=args.target, force=args.force)
                _say(err, f"wrote {n} entr{'y' if n == 1 else 'ies'} to {args.out}")
            else:
                exportmod.export_hits(ix, args.format, out, min_score=args.min_score,
                                      host=args.target)
        except FileExistsError as exc:
            _say(err, f"export: {exc}")
            return 1
        return 0
    return _fetch(args, ix, hooks, out, err)


def _ingest(args, ix, path, out, err) -> int:
    rules = _rules(args, path)
    status = 0
    for target in args.paths:
        try:
            if os.path.isdir(target):
                s = ingest_dir(ix, target, rules=rules)
            elif os.path.isfile(target):
                if not args.archive_url:
                    raise UsageError(f"{target} is a file: rvtree output needs --archive-url "
                                     f"(rvtree does not say which archive it walked)")
                s = ingest_ndjson(ix, target, archive_url=args.archive_url, job_id=args.job_id,
                                  rules=rules,
                                  stopped_because="partial" if args.partial else None)
            else:
                raise FileNotFoundError(target)
        except FileNotFoundError:
            _say(err, f"ingest: {target} does not exist")
            status = 1
            continue
        except ValueError as exc:
            raise UsageError(str(exc)) from exc
        _say(out, f"{s.job_id or target}: {s.entries_new} new, {s.entries_seen} already known, "
                  f"{s.skipped} skipped, {s.hits} hit(s)"
                  + (f", {s.custody} custody record(s)" if s.custody else ""))
    return status


def _diff(args, ix, out, err) -> int:
    try:
        d = diffmod.diff(ix, args.target, since=args.since, until=args.until)
    except diffmod.DiffError as exc:
        _say(err, f"diff: {exc}")
        return 1
    if args.format == "json":
        _say(out, json.dumps({"target": d.host, "since": d.since_job, "until": d.until_job,
                              "added": d.added, "removed": d.removed, "resized": d.resized,
                              "unknown": d.unknown, "warnings": d.warnings}))
        return 0
    _say(out, f"{d.host}: {d.since_job} -> {d.until_job}")
    for e in d.added:
        _say(out, f"+ {e['path']}  {e['kind']}  {'' if e['size'] is None else e['size']}")
    for e in d.removed:
        _say(out, f"- {e['path']}  {e['kind']}")
    for e in d.resized:
        _say(out, f"~ {e['path']}  {e['old_size']} -> {e['size']}")
    for e in d.unknown:
        _say(out, f"? {e['path']}  (not re-listed: unknown, not removed)")
    for w in d.warnings:
        _say(out, f"note: {w}")
    _say(out, f"{len(d.added)} added, {len(d.removed)} removed, {len(d.resized)} resized, "
              f"{len(d.unknown)} unknown")
    return 0


def _target(args, ix, out, err) -> int:
    row = ix.conn.execute("SELECT id FROM targets WHERE host=?", (args.host,)).fetchone()
    if row is None:
        _say(err, f"target: no target {args.host!r} in the index")
        return 1
    changes = {k: v for k, v in (("label", args.label), ("grp", args.grp),
                                 ("victim", args.victim)) if v is not None}
    if changes:
        ix.conn.execute("UPDATE targets SET " + ", ".join(f"{k}=?" for k in changes)
                        + " WHERE id=?", (*changes.values(), row[0]))
        ix.conn.commit()
    label, grp, victim = ix.conn.execute(
        "SELECT label, grp, victim FROM targets WHERE id=?", (row[0],)).fetchone()
    _say(out, f"{args.host}  label={label or '-'}  group={grp or '-'}  victim={victim or '-'}")
    return 0


def _fetch(args, ix, hooks, out, err) -> int:
    if args.dry_run:
        chosen = fetchmod.select(ix, args.min_score, host=args.target, limit=args.limit,
                                 again=args.again)
        for c in chosen:
            how = "extract from" if c.container_url else "download"
            _say(out, f"would fetch  {c.score:3d}  {c.host}{c.path}  ({how} {c.url})")
        _say(out, f"{len(chosen)} would be fetched")
        return 0
    if hooks is None:
        _say(err, "fetch: needs the program's download engine and proxies; run it as "
                  "'OnionAccelerator.py --mode index fetch ...'")
        return 1
    try:
        summary = fetchmod.run(
            ix, min_score=args.min_score, host=args.target, limit=args.limit, again=args.again,
            download=hooks.download, extract=hooks.extract, out_dir=hooks.out_dir,
            max_fetch_mib=args.max_fetch, engine_options=hooks.engine_options)
    except RuntimeError as exc:
        _say(err, f"fetch: {exc}")
        return 1
    _say(out, f"selected {len(summary.selected)}: {summary.ok} fetched, {summary.failed} failed")
    for failure in summary.failures:
        _say(err, f"fetch: {failure}")
    return 1 if summary.failed else 0
