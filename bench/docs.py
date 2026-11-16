"""docs/benchmarks.md: the page the tables live on, generated so it cannot drift.

The prose here says what is measured and what is not; the numbers are only ever in the
tables, which `python -m bench --check` regenerates and compares. Nothing in the text quotes
a figure, so a rerun on different hardware cannot leave the page contradicting itself.
"""

from __future__ import annotations

import os

from .report import render_table
from .runners import ROOT

PAGE_PATH = os.path.join(ROOT, "docs", "benchmarks.md")

HEAD = """\
# Benchmarks

Everything on this page comes from `python -m bench`, runs offline, and can be regenerated
and checked on a clean checkout with no network:

```bash
python -m bench                    # run every suite, print the tables
python -m bench --check            # re-run and compare with this page; exit 1 if it differs
python -m bench --write-docs       # regenerate this page
python -m bench --suite resume     # one suite
python -m bench --quick            # a fast smoke run; its numbers are not published
```

## What is being measured, and what is not

The software is measured, not Tor. A content server and a set of SOCKS5 stubs run in a
process of their own on loopback (`bench/lab.py`), and each stub shapes traffic the way a
circuit does: a throughput cap per connection, optionally a cap shared by the whole daemon,
round-trip latency with a long tail, a setup delay, and connections that die part-way
through a transfer. Every system under test runs in its own subprocess against the same
stubs, and **a run only counts if the file it wrote has the right SHA-256**: a fast wrong
answer is not a result.

So the tables say how the program behaves when the network has these properties: how it
scales with lanes, what it does about a slow lane or a dying one, what a kill costs, what
hedging costs. They do not say how fast Tor is, and a real onion will not match them. The
shapes (a lane at a tenth of the speed costs the old code ten times what it costs the new)
are the transferable part; the absolute seconds are not.

The lab itself is checked against a stopwatch (`tests/test_bench_lab.py`), and relays well
over 400 MiB/s unshaped, several times the most any table here asks of it.

## The systems

* **engine**: this checkout's `partial_download_mode`: fixed pieces pulled from a shared
  queue by whichever lane is free, written at their offsets, journalled, resumable.
* **legacy**: the same function at an earlier commit (the one in the environment table),
  which gave each proxy one chunk and merged the chunks afterwards. It is extracted with
  `git archive` into a scratch directory, so the two never share a module. If the commit is
  not in your clone its column reads n/a.
* **aria2c**: only where it is installed. aria2c cannot use a SOCKS5 proxy, so each lane is
  also exposed as an HTTP mirror with the same shaping and aria2c is given every mirror for
  the one file: the same capacity, reached the way aria2c can reach it. Its column reads n/a
  where it is not installed, and an n/a is the honest value: nothing was run.

## Environment

{environment}

## Results

"""

TAIL = """\

## What is not here

* **A run through Tor.** `bench/onion.py` runs the same transfer against an onion service you
  operate, through a farm you started, and prints the table with its date, Tor version and
  lane count. It needs both, so it is not part of `python -m bench` and nothing from it is on
  this page. Numbers from one would be a statement about that onion and that day.
* **Contention.** The lab, the engine and the benchmark share a machine. The lab's capacity
  is far above what any table asks of it, but a busy machine will still move the seconds.
* **Statistics.** These are medians of a handful of runs, enough to show a 10x effect and not
  enough to resolve a 10% one. `--check` allows 35% by default for that reason.
"""


def environment_block(env: dict) -> str:
    rows = [("date (UTC)", env["date"]), ("commit", env["commit"]), ("python", env["python"]),
            ("platform", env["platform"]), ("cpus", env["cpus"]), ("requests", env["requests"]),
            ("PySocks", env["pysocks"]), ("legacy baseline", env["baseline"]),
            ("aria2c", env["aria2c"])]
    return "| | |\n|---|---|\n" + "\n".join(f"| {k} | {v} |" for k, v in rows)


def write_page(path: str, tables: list, env: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    body = HEAD.replace("{environment}", environment_block(env))
    body += "\n".join(render_table(t) for t in tables) + TAIL
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
