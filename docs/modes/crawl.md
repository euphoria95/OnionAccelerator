# Open-directory crawl (`--mode crawl`)

- Walks an **"Index of /" open directory** recursively and writes a manifest of every file
  it finds — the one mode that *discovers* URLs instead of consuming a list of them.
- Parses listings with a **server-agnostic** parser: Apache `<pre>` and `<table>`, nginx,
  lighttpd, Caddy, hand-rolled templates and nginx's JSON autoindex all come out as the
  same `directories` / `files` split, because the filtering is structural rather than
  template-matched (see **Open-Directory Crawl** below).
- Also walks the **file managers** leak sites run, which serve a whole tree from one
  route and encode the path into it — `/r/filemanager/TOK/dumps%2Fraw/part.bin` — rather
  than serving the tree at its own paths.
- Reads targets whose listing is **not links at all** — a path in `?p=`, a JSON API behind
  a JavaScript shell, a WebDAV collection, an S3 bucket, or the site's own published
  `tree` dump — through **listing templates**: one small TOML file per known logic.
  `--detect` says which one fits a target before you spend a crawl finding out.
- Spreads its requests across **many Tor circuits at once** — every SOCKS endpoint × N
  credential-isolated circuits — with per-endpoint failover, circuit rotation and
  exponential backoff for the timeouts, 503s and dropped circuits that Tor guarantees.
- Optionally hands the discovered URLs straight to the multi-download path (`--download`).

## Details

Every other mode needs to be told what to fetch. This one finds out. Seeds come from
`--url` (repeatable) or from `URLs.txt` — one open-directory URL per line — and the crawl
walks down from each of them, listing directories and recording files, until it runs out
of tree or out of budget.

```bash
# Map the whole tree under one seed (depth is unlimited by default).
python3 OnionAccelerator.py --mode crawl --url https://example.onion/dumps/

# Same, seeded from every line of URLs.txt.
python3 OnionAccelerator.py --mode crawl

# A quick reconnaissance pass: two levels, fifty directories, then stop.
python3 OnionAccelerator.py --mode crawl --max-depth 2 --max-pages 50

# Map the shape of the tree breadth-first, then dive once it is known.
python3 OnionAccelerator.py --mode crawl --order bfs --switch-after 200

# Only the dumps, never the thumbnails.
python3 OnionAccelerator.py --mode crawl --include '/(dumps?|leaks)/' --exclude '/thumbs/'

# Through Tor daemons this script does not manage, four circuits on each.
python3 OnionAccelerator.py --mode crawl --socks 127.0.0.1:9050,127.0.0.1:9052 --circuits-per-endpoint 4

# Map, then pull everything down, mirroring the remote tree under downloads/<host>/.
python3 OnionAccelerator.py --mode crawl --max-depth 3 --download
```

## Reading is separate from crawling

The crawler is two halves. The **engine** — frontier, circuit pool, fetcher, report —
moves bytes and knows nothing about what a listing looks like. The **listing layer**
(`crawler/listing/`) decides what a fetched page means, and is driven by **templates**:
one small TOML file per known open-directory logic.

That split exists because the two halves change for different reasons. The transport is
the same for every onion; the reading is different for every file manager. Keeping them
together is what made *"this target is unsupported"* indistinguishable from *"this
directory is empty"* — both produced a run that reported success and stopped early.

```bash
# What is this target, and how should it be read? One request per seed.
python3 OnionAccelerator.py --mode crawl --detect --url https://example.onion/

# What does it already know how to read?
python3 OnionAccelerator.py --list-profiles
```

`--detect` reports one row per template that could apply, ranked. A seed that could not be
*fetched* produces no rows at all and says so — every template is tried against a page that
was read, and the fallback matches unconditionally, so "no rows" can only ever mean the
request failed. Naming a profile there would read as though that template had been tried
and rejected.

## A parser with no server *markup* templates

The default reading has no per-server templates and never will. Open directories are
rendered by at least half a dozen web servers and any number of hand-rolled templates, so
matching markup per server is a losing game. The parser reads *every* `<a href>` on the
page and then discards junk by **structure**:

- Anything that does not resolve strictly *below* the directory being listed is
  navigation. That single rule kills `../`, `/`, "Parent Directory" and every breadcrumb
  on every server, without matching a word of link text.
- Anything whose path equals the page's own path but carries a query is a sort control.
  That covers Apache's `?C=N;O=D`, nginx's `?sort=`, lighttpd's `?N=D` and h5ai's
  `?view=` without hard-coding a single parameter name.
- A small junk-text set (`Name`, `Last modified`, `Size`, `Description`, …) is a secondary
  net for column headers that are links, not the primary filter.

What survives is split into directories and files: a trailing slash first, then link text,
then a `[DIR]` icon, then "no extension **and** no size in the row" — so a template that
strips the slash is still categorised correctly. Sizes (`4.1K`, `512M`, `1.2 GiB`, raw
bytes) and modification dates are recovered from the row where they exist and recorded in
the manifest. `application/json` bodies (nginx `autoindex_format json`, Caddy) are read as
JSON and produce identical entries.

Before recursing, each page is scored on whether it *is* an index: an "Index of" title, a
server `<address>` footer, column-sort links, a **parent-directory up-link**, an in-scope
link ratio, and the absence of forms. Below `0.5` it is never expanded — the guard that
stops a directory walk from turning into an unbounded crawl of somebody's forum over Tor.
Such a page is written to `dirs.jsonl` with the score that disqualified it, and is
deliberately *not* written to the file manifest: it was queued because a listing put it in
its directory column, and answering with HTML does not make it a file. Recording it as one
would put a directory into `urls.txt`, where `--download` fetches its markup and saves that
under the directory's name.

The score is a *guess*, and it is asked for only where nothing better is available: on a
page a named template matched, the template has already answered the question and the
threshold is not applied at all — see **Templates** below.

The parent-directory up-link (`../` / "Parent Directory", matched structurally as a link
to the page's immediate parent) carries real weight because it is the one signal a
filesystem listing always has and a web application never does. Without it, a custom
autoindex with no title and no footer — the common shape on a leak-site file manager —
scores below the bar the moment a directory holds only a single file or subdirectory, and
that directory is wrongly abandoned as a leaf, silently pruning whatever is beneath it. An
application does not gain from the signal: only a link to the *immediate*, segment-aligned
parent counts, so stray "up" links to `/` or a sibling section do not fire it.

## Templates: addressing, not markup

What a template describes is the axis the structural reading cannot express — **how a
listing is addressed and fetched**. Is a child directory a path segment, a query
parameter, a percent-encoded segment, or a field in a JSON request? Where are the rows,
if they are not links? Where does a file's *bytes* live, if not at the URL its row points
to? None of that is knowable from markup, and all of it decides whether a crawl goes
anywhere at all.

The built-in set covers five logics. `--list-profiles` prints them all:

| Logic | Templates |
|---|---|
| Path-addressed HTML autoindex | `generic-structural` (the fallback, priority 0), `apache-autoindex`, `nginx-autoindex`, `lighttpd-dirlisting`, `caddy-browse`, `iis-directory-browsing`, `python-http-server`, `fancyindex-theme`, `directory-lister` |
| Machine-readable over GET | `nginx-json`, `caddy-json`, `s3-bucket-xml` |
| Query / param-addressed managers | `tiny-file-manager` (`?p=`), `laravel-filemanager`, `laravel-encoded-segment`, `nextcloud-public-share` |
| API-driven (a POST per directory) | `alist`, `filebrowser`, `filegator`, `h5ai`, `elfinder`, `webdav-propfind` |
| The target's own published index | `tree-dump`, `ls-lr-dump`, `find-dump`, `sitemap-xml` |

A template is four short sections — recognise, extract, navigate, download:

```toml
name     = "tiny-file-manager"
title    = "Tiny File Manager (path in ?p=)"
priority = 70

[match]                                  # every rule present must hold
content_type = ["text/html"]
body_regex   = "tinyfilemanager"

[extract]                                # where the rows are
strategy = "rows"
row      = "table#main-table tbody tr"
link     = "td a.link"
dir_when = "i.fa-folder-o"

[navigate]                               # how a child is addressed
kind  = "query"
param = "p"

[download]                               # where the bytes are, if not the row's URL
url = "{root}?p={parent}&dl={name}"      # {parent} is the row's directory, not the row
```

Three properties make this safe to rely on:

- **A target no template matches is crawled exactly as it was before templates existed.**
  `generic-structural` has no match rules, sits at priority 0, and is the fallback. The
  whole existing test suite runs through it unchanged.
- **A template that fails to load is a fatal error naming the file** — including a
  `priority`, `status` or `max_pages` that is not a number, which names the file and the
  key rather than raising a bare conversion error. A silently ignored typo is a rule that
  stopped applying, which produces precisely the quiet failure this design exists to
  remove.
- **A matched template is believed, per page.** The confidence threshold exists because
  the structural reader has to guess; a template that fired has said what the target is,
  so a directory holding a single file is a directory rather than a leaf. That trust is
  re-checked against *each* page — a server that autoindexes most of its paths and runs an
  application at one of them is ordinary, and waiving the guard for the whole host on the
  strength of the root page would walk straight into it. A page the rules do not fire on
  gets the structural guard back, and a strategy can still veto outright: an empty WebDAV
  `multistatus` or a JSON error page scores zero and is not expanded, because "unreadable"
  and "empty" are different facts and the crawl acts on both.

`status = "verified"` in `--list-profiles` means a fixture in the test suite pins that
template against a real listing. `unverified` means it was written from documented request
shapes with no live target to check against — `--detect` says so rather than presenting a
guess as a fact.

Templates you write live wherever you like:

```bash
python3 OnionAccelerator.py --mode crawl --templates ~/profiles \
    --profile my-target --url https://example.onion/
```

A template in `--templates` replaces a built-in of the same name, so a profile for one
engagement's target never has to be committed. `--detect` carries the flag into the command
it suggests, because a profile loaded from a directory does not exist without it and advice
that does not run is worse than none. **How to write one:
[`crawler/listing/templates/README.md`](../../crawler/listing/templates/README.md)** — three
worked examples and the full key reference.

## The target's own index, instead of a crawl

Leak sites publish these. Where a target serves a `tree`, `find` or `ls -lR` dump of what
it holds, reading it is *one request* against the thousands the same tree costs over Tor
— and it is the only sound way to measure what a crawl missed:

```bash
python3 OnionAccelerator.py --mode crawl \
    --profile tree-dump --url https://example.onion/List_of_files.txt
```

Directories from a dump are recorded and never fetched: the dump already said what is
under them.

Paths out of a dump are cleaned one prefix at a time, not with a character set: stripping
`"./"` as a set of characters eats the dot that makes a dotfile a dotfile, so `./.env` came
back as `env` and `.git/config` as `git/config` — and a dump of a leak site is full of
both. A wrong path in a manifest is worse than a missing one, because `--download` will
fetch it and save something under the wrong name. The bare `.` that `tree` prints for the
root it was run in is the page itself, and is recorded as such rather than as a child named
`.`.

## Paths, as the server spells them

An autoindex serves a tree at the tree's own paths. A **file manager** — the shape leak
sites run — serves the whole tree from one route and passes the path as a parameter, which
it then encodes differently in different links:

```
/r/filemanager/TOK/dumps/raw              the page, served at its own URL
/r/filemanager/TOK/dumps%2Fraw/part.bin   a child, with the separator folded away
/r/filemanager/TOK/Q1%202019/photos       an up-link — the page itself is at Q1+2019
```

Read as raw path text, `dumps%2Fraw` is one directory named "dumps/raw" — below neither
the page that linked it nor the crawl's scope root. So every link on every page below the
first nested level is discarded as off-tree navigation, and the crawl stops two levels
down having reported success. On the onion this was found on it returned **418 files**
where the site's own index lists 86,992, with no error anywhere in the log. The `%20`/`+`
mismatch is the same failure a level quieter: the up-link stops looking like a parent, so
a directory whose only positive signal was that link scores 0.30 and is written off.

So every scope, parent, self-link and deduplication test in the crawler reads the path
through **one** function, which decodes it *before* splitting on `/` and reads `+` as the
space a form-style encoder meant by it. What gets fetched is untouched — `%2F` goes back
out on the wire exactly as the server published it — and what gets *written down* uses the
strict reading, so a file genuinely named `C++ notes.txt` is recorded under its own name.
Deduplication is on the decoded identity, so a directory reachable both ways costs one
crawl of that subtree rather than two. `--download`'s mirror under `downloads/<host>/`
reads paths the same way, so a folded segment lands as the directories it stands for
instead of as one directory named `dumps_raw` beside the `dumps` its siblings went into.

## Concurrency: circuits, not just threads

Endpoints resolve exactly like `--mode tree`: `--socks` if given, else `--external`, else a
live probe of the farm range, else the local Tor client, else an error. The download modes'
fallback to a fixed twenty-port list is deliberately *not* used — a crawl issues thousands
of requests and would spend all of them timing out on ports that were never there.

Each endpoint is opened `--circuits-per-endpoint` times (default 2), and **each circuit
gets its own SOCKS username/password**. That is what makes them separate Tor circuits
rather than one shared one, and it is the difference between real parallelism and twenty
sessions queueing behind a single guard. Every circuit also pins one User-Agent for its
lifetime — a UA that changes per request on a fixed circuit is itself a fingerprint.

Lanes are handed out round-robin across *daemons*, so consecutive requests land on
different guards. A failing endpoint is parked after three consecutive failures and every
later lease prefers a live one; a dropped circuit is rotated (new credential, new circuit)
rather than retried into the same hole. Errors are classified rather than counted:
timeouts and 429/502/503/504 back off exponentially with full jitter (honouring
`Retry-After`), circuit drops and SOCKS errors rotate, and 401/403/404/410 are recorded
once and never retried. A backed-off job is re-heaped with a `not_before` timestamp instead
of sleeping, so a slow host never occupies a worker.

`--per-host` (default 8) caps concurrent requests against any single target: an onion
service is usually one small process, and past that it starts refusing connections.

## Queueing and layers

The frontier is a depth-ordered heap with exact URL deduplication — every discovered
subdirectory becomes its own schedulable job, so a directory with 50 subdirectories becomes
50 units of work rather than one. For a target whose listing lives at **one endpoint**, the
URL is not the whole identity: every directory is the same address, differing only in the
path it asks for, so the request's path joins the dedup key and the frontier stops folding
a whole tree into a single job. That holds for a plain `GET` of an endpoint as much as for
a `POST` — half the file-manager APIs worth crawling spell the directory into a GET — so
the test is whether the URL is the target's own endpoint, not whether the method is GET.
Those requests are also the ones nothing may re-point at the job's own URL, which is the
directory they stand for and an address the server does not serve.

`--order bfs` maps the whole tree shallow-first; `--order dfs` finishes branches;
`--switch-after N` flips from one to the other mid-run, which re-orders work that is
*already queued* (an `asyncio.PriorityQueue` could not — it
fixes each item's key when it is pushed). Depth is **unlimited by default** — the point of
the mode is to harvest a whole open directory — and `--max-depth N` reinstates a finite
cap. A finite tree still terminates on its own, because deduplication means it cannot
recurse into itself; a directory that *contains* itself does not, since a symlink loop
produces a genuinely new URL at every level and deduplication cannot cut it, so those
runs need `--max-depth`, `--max-pages` or `--time-budget` to end.

Stops are `--max-depth`, `--max-pages`, `--time-budget` and `Ctrl-C` — all of them clean.
The report is streamed and flushed per line, so an interrupted run still leaves a valid,
complete-as-far-as-it-got manifest.

## Output

Everything lands in `crawls/<job_id>/` (gitignored):

| File | Contents |
|---|---|
| `listing.jsonl` | one record per file: `url, host, path, name, size_bytes, mtime_text, depth, parent, http_status, content_type, endpoint, discovered_at` |
| `dirs.jsonl` | one per directory: `url, depth, parent, status, n_dirs, n_files, is_index, confidence, server, title, elapsed_ms, attempts, endpoint`. `is_index: false` marks a page that was read and refused by the confidence guard |
| `failed.jsonl` | `url, depth, attempts, status, verdict, error, endpoint, final` (`final` marks the attempt that exhausted `--retries`) |
| `stats.json` | totals, per-layer counts, per-endpoint throughput and failures, wall time, why it stopped |
| `tree.txt` | the tree, rendered for eyeballing |
| `urls.txt` | bare file URLs, ready to feed straight back in |

```bash
python3 OnionAccelerator.py --mode crawl --max-depth 3
cp crawls/<job_id>/urls.txt URLs.txt
python3 OnionAccelerator.py --mode partial
```

`--download` does that for you as a second, sequential phase after the crawl — sequential
on purpose, because the download stack is threads over `requests` and running it alongside
the event loop would put two unrelated concurrency models on the same Tor daemons at once.
It downloads with the remote directory structure **preserved** under
`downloads/<host>/a/b/file.txt`; a crawled tree routinely holds many same-named files in
different directories, and the flat `downloads/<host>/file.txt` layout the other modes use
would silently overwrite all but one of them.

Logging goes into the usual `logs/OnionAccelerator_<job_id>.log`: one DEBUG line per
request (`lane`, `endpoint`, `depth`, `attempt`, `status`, `bytes`, `ms`, `verdict`, `url`),
one INFO line per listed directory, and a periodic progress line whose per-endpoint balance
(`127.0.0.1:9050=214 127.0.0.1:9052=209/3f`) is the one number that says whether the
multi-circuit spread is actually working.
