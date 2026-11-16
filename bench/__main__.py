"""python -m bench: run the suites, print the tables, or write / check docs/benchmarks.md."""

from __future__ import annotations

import argparse
import os
import sys

from . import report, runners, suites
from .docs import PAGE_PATH, write_page


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bench", description=__doc__)
    ap.add_argument("--suite", choices=[*suites.SUITES, "all"], default="all")
    ap.add_argument("--quick", action="store_true",
                    help="small and fast, to check the harness runs; not numbers to publish")
    ap.add_argument("--baseline", default=runners.DEFAULT_BASELINE, metavar="REF",
                    help="the commit the 'legacy' column runs (default %(default)s)")
    ap.add_argument("--write-docs", nargs="?", const=PAGE_PATH, metavar="PATH",
                    help=f"write the results page (default {PAGE_PATH})")
    ap.add_argument("--check", nargs="?", const=PAGE_PATH, metavar="PATH",
                    help="re-run and compare with a results page; exit 1 if it does not reproduce")
    ap.add_argument("--tolerance", type=float, default=0.35,
                    help="relative difference allowed per number when checking (default %(default)s)")
    args = ap.parse_args(argv)
    if args.quick and (args.write_docs or args.check):
        ap.error("--quick numbers are not published and cannot be checked against the page")
    if args.write_docs and args.suite != "all":
        ap.error("--write-docs needs every suite, or the page would be missing tables")

    ctx = suites.Context("quick" if args.quick else "full", args.baseline)
    chosen = list(suites.SUITES) if args.suite == "all" else [args.suite]
    tables = []
    try:
        for name in chosen:
            print(f"[bench] {name} ...", file=sys.stderr, flush=True)
            tables += suites.SUITES[name](ctx)
        env = suites.environment(ctx)
    finally:
        ctx.close()

    for t in tables:
        print(report.render_table(t))
    if args.write_docs:
        write_page(args.write_docs, tables, env)
        print(f"[bench] wrote {args.write_docs}", file=sys.stderr)
    if args.check:
        with open(args.check, encoding="utf-8") as fh:
            published = report.parse_tables(fh.read())
        wanted = {t.title for t in tables}
        problems = report.compare(tables, [t for t in published if t.title in wanted],
                                  args.tolerance)
        if problems:
            print("[bench] does NOT reproduce:", *problems, sep="\n  ", file=sys.stderr)
            return 1
        print(f"[bench] reproduces {os.path.relpath(args.check)} within {args.tolerance:.0%}",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
