# The persistent index (`--mode index`)

Every crawl and archive listing lands in one SQLite file, scored and searchable, so the
question changes from "what is on this site" to "what is new, and what touches us".

## Details

A crawl writes `crawls/<job_id>/`; a tree listing writes ndjson; neither remembers the
last one. The index does: every path, once per target, with the runs that saw it, what
each matched, and what has been downloaded. It is standard-library Python over SQLite, in
the `index/` package, and `--mode index ACTION` is its command line.

```bash
# Index a finished crawl, or watch one as it runs:
python3 OnionAccelerator.py --mode index ingest crawls/20261113_103000_ab12cd
python3 OnionAccelerator.py --mode index live --stream-url http://127.0.0.1:8787

# An rvtree listing (rvtree does not say which archive it walked, so you must):
python3 OnionAccelerator.py --mode index ingest tree.ndjson --archive-url https://example.onion/b.7z

# Ask it things:
python3 OnionAccelerator.py --mode index hits --min-score 60 --explain
python3 OnionAccelerator.py --mode index search payroll 2024
python3 OnionAccelerator.py --mode index diff --target example.onion

# Pull down what matters, then hand it on:
python3 OnionAccelerator.py --mode index fetch --min-score 85 --dry-run
python3 OnionAccelerator.py --mode index fetch --min-score 85
python3 OnionAccelerator.py --mode index export --format csv --min-score 60 --out case-12.csv
```

`--full-help index` lists every action and option.

**Where it lives and who can read it.** `--index PATH`, else `$ONIONACCELERATOR_INDEX`, else
`~/.local/share/onionaccelerator/index.db` — outside the repository, created `0600` in a
`0700` directory *before* SQLite sees it (SQLite gives the WAL files the main file's
permissions, so the order matters), in WAL mode so you can query while a live ingest
writes. A file that is not an index, or that a newer version wrote, is refused untouched.

**Idempotent.** Ingesting a run twice changes nothing, and neither does ingesting an old
run after a newer one. An index built **live** from `--stream` and one built **afterwards**
from the same run's directory are row-for-row equal, because a stream event's `data` is
the record on disk and both paths feed one function. If the live consumer falls behind
(`stream.gap`), the loss is reported and — when the run's output directory is readable,
from `--reconcile DIR` or the run's `/status` — mended from that directory after the run
stops.

## The schema

| Table | One row per | Notes |
|---|---|---|
| `runs` | crawl, tree walk | `job_id` is the stream's `run`; times are the first and last record's, not the wall clock; `stopped_because` says whether it finished |
| `targets` | host | `label`, `grp`, `victim`: what *you* know (`--mode index target HOST --label …`) |
| `run_targets` | (run, target) | which hosts a run covered: a run that crawled B says nothing about A |
| `entries` | (target, path) | name, size (`NULL` is unknown, never `0`), mtime as the server wrote it, `kind`, `source` (`crawl` or `tree`), `fetch_url`, first and last run. An archive member is `…/backup.7z!/dir/file` |
| `entry_versions` | presence segment | (entry, first run, size, last run). A segment ends when the size changes or the entry goes missing from a run, so "was it there in run N" is exact |
| `entries_fts` | entry | FTS5 over `path` |
| `hits` | (entry, rule) | score, reason, and the span of the path that matched |
| `downloads` | custody record | the engine's `<name>.oa.json`, linked to the entry |

```sql
-- new entries since the last run of a target
SELECT e.path, e.size FROM entries e JOIN targets t ON t.id = e.target_id
 WHERE t.host = :host AND e.first_run > :run;
-- top hits by score
SELECT t.host, e.path, MAX(h.score) AS score FROM hits h JOIN entries e ON e.id = h.entry_id
  JOIN targets t ON t.id = e.target_id GROUP BY e.id ORDER BY score DESC LIMIT 50;
-- every sighting of a path pattern across targets
SELECT t.host, e.path, e.first_run, e.last_run FROM entries_fts
  JOIN entries e ON e.id = entries_fts.rowid JOIN targets t ON t.id = e.target_id
 WHERE entries_fts MATCH 'payroll AND 2024';
```

## Scoring, and saying why

Rule packs are TOML under `index/packs/` — credential and key material, mail stores,
directory-service dumps, remote-access configs, database dumps, and HR, finance and legal
paths — following the pattern of `crawler/listing/templates/`: coverage is a file, not a
code change. Each rule has a score from 1 to 100, a reason, and `positive` / `negative`
example paths that a test runs, so a rule cannot ship matching less or more than it
claims. An entry's score is its best hit; every hit stores the rule, the reason and the
part of the path that matched (`hits --explain`).

Your own terms go in a `scope.toml` (`--scope`, `$ONIONACCELERATOR_SCOPE`, or one beside
the index file):

```toml
[scope]
organisations = ["Example Corp"]        # literal text, any case: never read as a pattern
domains       = ["example.test"]
suppliers     = ["Widget Supply Ltd"]
regexes       = ['(?i)\bPO-\d{6}\b']      # only this list is regular expressions

[scope.scores]                          # defaults: 70, 70, 50, 60
organisations = 75
```

Paths are written by the server you are investigating, so matching is defended: a regex
with a repeat inside an unbounded repeat (`(a+)+`) is refused when the file loads, because
it backtracks exponentially on input built for it, and only the last 1,024 characters of a
path are matched. That is a guard, not a proof — alternations with overlapping branches can
still be slow; a regex of your own is yours. `rescore` re-applies the current rules to
everything already indexed, and a term you withdrew loses its hits.

## Diff

`diff --target HOST` compares the last two sightings (`--since RUN` / `--until RUN` to
choose). **Removed** is a claim, so it is made carefully: a file is removed when its
directory was listed again and it was not in it. A directory the later crawl never reached
— it was cut short (`max_pages`, `time_budget`), or gave up on a subtree
(`incomplete`) — leaves its contents **unknown**, never removed. An archive's members are
removed only if the archive was walked again. The index sees paths, not intent: a directory
renamed from `/old` to `/new` is `/old` removed and `/new` added, and the same names and
sizes on both sides are the tell.

## Selective fetch

`fetch --min-score N` selects files scoring at least N (best first, one per download URL,
skipping what is already downloaded unless `--again`) and hands them to the download engine
with the flags the download modes take (`--piece-size`, `--retries`, `--fresh`, `--socks`,
`--circuits-per-endpoint`) and the same proxy ladder as crawl and tree. A hit **inside an
archive** is never downloaded whole: it goes to rvtree's `extract`, bounded by
`--max-fetch MiB` (default 64), and lands under `downloads/<host>/<archive>.members/` with
every path component sanitised, so a hostile member name cannot write outside the output
directory. Custody records are linked back to the entry they fetched. `--dry-run` lists
without fetching.

## Export

`export --format jsonl|csv` writes each entry at or above `--min-score` with its host,
your label/group/victim, every rule that fired, the runs that saw it, and the SHA-256 of
anything downloaded. `--out FILE` is created `0600` and is not overwritten without
`--force`. **CSV cells beginning `=`, `+`, `-`, `@`, tab or carriage return are prefixed
with an apostrophe**: every path was written by the server being investigated, and the
file is the one an analyst opens in a spreadsheet. STIX export is deliberately not
provided.

## Scale

A million entries ingest in minutes on a laptop and a path search returns in well under a
second (`tests/test_index_scale.py`, marked `slow`; the measured numbers are in the pull
request that added it, not estimated here).
