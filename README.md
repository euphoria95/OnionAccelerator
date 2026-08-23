# OnionAccelerator

OnionAccelerator is a multi-functional Python script designed for discovering and downloading files through multiple SOCKS5 proxies (commonly Tor instances). It supports five modes:

## Modes

### Multi-Download Mode

- Simultaneously download multiple files listed in `URLs.txt`, with worker-thread concurrency bounded by the proxy count (capped at the number of URLs, so no idle threads are spawned).
- Automatically retries failed downloads a configurable number of times, drawing a fresh proxy from the pool on each retry so a dead endpoint doesn't doom a URL.
- Aggregates download progress via a single progress bar in the terminal.

### Partial-Download Mode

- Splits each file into parallel byte-range chunks — up to one per proxy, but never more chunks than the file size warrants (chunks are at least `MIN_PARTIAL_CHUNK_SIZE`, 1 MB by default) — each served by a SOCKS5 proxy drawn from the shared pool.
- Establishes the file size through a ladder of four probes, so a server that withholds `Content-Length` no longer costs you the parallelism: a `HEAD`, then the `size=` parameter of that same response's `Content-Disposition`, then the total in the `Content-Range` of a one-byte `Range: bytes=0-0` GET, and finally a plain streamed GET aborted the moment its headers arrive. No probe transfers a payload.
- If none of the four can establish a size, the script automatically falls back to a single, sequential download.
- Merges the downloaded chunks into a final file upon success, and performs retry logic if any chunk fails — each chunk retry lands on a different live proxy via the pool's failover.

### Remote Archive Tree Mode (`--mode tree`)

- Lists or extracts members of a **huge remote archive** (`.rar`, `.tar`, `.zip`, `.7z`,
  `.tar.xz`) using HTTP Range requests, **without downloading it**. A 100 GB archive costs
  kilobytes to read, because every supported format keeps its metadata somewhere a handful
  of range requests can reach.
- Backed by [`remote_viewer/rvtree`](remote_viewer/), driven over the same `host:port`
  SOCKS5 pool the download modes use — so a `--farm` of independent Tor daemons, or an
  `--external` proxy list, gives it real parallelism.
- Solves the **proof-of-work gates** some services put in front of a download, and says
  so — a challenge page answered with 200 otherwise looks like the archive itself, and
  every size and range it reports is a fact about the wrong entity.
- Falls back to `--spool` when a server refuses ranges outright, which is the one case
  the whole premise does not survive: it costs a full transfer, so it is offered with a
  time estimate rather than taken (see **Gates, and servers that refuse ranges** below).
- Everything after `--` is passed straight to rvtree, so the full `list` / `extract` /
  `probe` surface is available with no flags to keep in sync.

### Open-Directory Crawl Mode (`--mode crawl`)

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

### Speedtest & Healthcheck Mode

- Tests all SOCKS5 ports **in parallel** and shows a summary of per-port speeds and failures (with emoji indicators).
- Availability checks for the full URL list use a randomly selected working port rather than always defaulting to port 5000.

## Key Features

### Multiple Threads & SOCKS5 Proxies

- Dynamically assigns up to 20 local SOCKS5 ports (e.g., `127.0.0.1:5000–127.0.0.1:5019`) for parallel fetching, potentially speeding up downloads over Tor.

### Progress Bars

- Utilizes `tqdm` to show clear download progress:
  - A single aggregated bar for multi-downloads.
  - A dedicated bar per file in partial-download mode.
  - Fallback sequential downloads also show an inline progress bar.

### Retry Logic

- Failed downloads and individual chunks are retried inline (up to `--retries` attempts) without re-queuing, avoiding potential deadlocks when the worker pool empties mid-retry.

### Collision-Free Output Paths

- Downloaded files are stored under a hostname subdirectory (e.g., `downloads/example.onion/file.zip`), so URLs from different hosts that share the same filename never overwrite each other.

### Logging

- Generates a unique `job_id` each time the script is run.
- Writes detailed logs (DEBUG level) to a timestamped file in the `logs/` directory.
- Outputs essential logs (INFO level) to the console.

### User-Agents

- Loads random User-Agent strings from the first column of `UserAgents.tsv` (tab-separated; subsequent columns such as usage weights are ignored).

### Seamless Fallback

- In partial-download mode, if none of the four size probes can establish a file size, the script automatically switches to a single GET request and proceeds with a standard download. The same applies when a response arrives under a `Content-Encoding`: the length it advertises describes the compressed transfer rather than the file, so it is not safe to split into ranges.

## Installation

1. **Install Python 3** (3.7+ recommended).

2. **Install dependencies:**

    ```bash
    pip install -r requirements.txt
    ```

3. **Ensure you have multiple SOCKS5 proxies** (e.g., Tor instances on ports `5000..5019`). The easiest way is the built-in native farm — `python3 OnionAccelerator.py --farm up` (see **Native Tor Farm** below) — or bring your own proxies / Docker.

## Usage

### Say what to fetch

Either way works, and you never need both:

- **`--url URL`** on the command line. Repeat it for several. When `--url` is given at
  least once it replaces the file entirely — `URLs.txt` is not read and need not exist.
- **`URLs.txt`** in the working directory, one URL per line, blank lines ignored. This is
  the fallback when `--url` is absent, and the right shape for a long list.

`--url` puts your targets into shell history and `ps` output; `URLs.txt` does not. On a
shared or logged host that difference matters.

### Run the script:

```bash
# Provision / manage the native Tor proxy farm:
python3 OnionAccelerator.py --farm <up|status|down|destroy> [--count N] [--base-port PORT] [--bootstrap-timeout SEC]

# Download / speedtest through the proxies:
python3 OnionAccelerator.py --mode <multi|partial|speedtest> [--url URL ...] [--retries N] [--external] [--test-url URL]

# Read a remote archive's file tree without downloading it:
python3 OnionAccelerator.py --mode tree [--url URL] [--external] [-- <rvtree arguments>]

# Recursively map an open directory (and optionally download what it finds):
python3 OnionAccelerator.py --mode crawl [--url URL ...] [--max-depth N] [--order bfs|dfs] [--socks host:port,...] [--download]

# Ask what a target is before crawling it, and see what can be read:
python3 OnionAccelerator.py --mode crawl --detect [--url URL ...]
python3 OnionAccelerator.py --list-profiles

# The per-mode reference: what each mode does and which flags reach it.
python3 OnionAccelerator.py --full-help [<multi|partial|speedtest|tree|crawl|farm>]
```

Exactly one of `--farm` or `--mode` must be given.

- `--farm`: manage a farm of native Tor instances instead of Docker (see **Native Tor Farm** below).

- `--mode`:
  - `multi`: Parallel download of every target, each in a separate worker thread with its own SOCKS5 proxy.
  - `partial`: Parallel chunk-based download for each URL, automatically merging chunks.
  - `speedtest`: Test download speed and basic health for each SOCKS5 proxy using the first target.
  - `tree`: List or extract members of a remote archive over the proxy pool without downloading it. Takes its target from `--url` or from the arguments after `--`, never from `URLs.txt` (see **Remote Archive Tree** below).
  - `crawl`: Recursively walk the open directories seeded from `--url` or `URLs.txt` and write a manifest of every file found, spread across many Tor circuits at once (see **Open-Directory Crawl** below).

- `--url URL`: A target URL given on the command line instead of in `URLs.txt`. Repeatable, and when present it replaces the file entirely. Under `--mode tree` it becomes rvtree's URL argument wherever that argument belongs, so `--url X -- extract etc/hosts` runs `extract X etc/hosts`; with no `--` block at all, `--mode tree --url X` runs `list X`.

- `--full-help [MODE]`: Print the long-form reference — per mode, what it does, every flag that actually reaches it with its default and the reasoning, worked examples, and the exit codes. Give a mode name (or `farm`) to print just that section. Plain `--help` remains the short flag list.

- `--retries N`: Set how many times to retry if a download fails (default: 3).

- `--external`: Use remote `ip:port` SOCKS5 proxies fetched from a public list instead of local Docker Tor instances (see **External Proxy List** below). Works with any `--mode`.

- `--test-url URL`: URL used **only** to verify external-proxy liveness (overrides the `PROXY_TEST_URL` config constant). Point it at an endpoint you control or trust so that real target URLs are never revealed to proxies that end up discarded. If unset, a random one of your own targets — from `--url` or `URLs.txt` — is used and a warning is logged. Only relevant with `--external`.

### Examples

```bash
# One file, no URLs.txt anywhere in sight:
python3 OnionAccelerator.py --mode partial --url https://example.onion/backup.iso

# Multi-download mode, retrying up to 3 times:
python3 OnionAccelerator.py --mode multi --retries 3

# Partial-download mode, splitting each file into 20 chunks in parallel:
python3 OnionAccelerator.py --mode partial

# Speedtest mode, checks each proxy port:
python3 OnionAccelerator.py --mode speedtest

# Multi-download using the external proxy list instead of local Docker Tor:
python3 OnionAccelerator.py --mode multi --external

# External mode, verifying proxies against your own endpoint (keeps real
# targets private) instead of a random URL from URLs.txt:
python3 OnionAccelerator.py --mode multi --external --test-url http://your-own-service.onion/ping

# Map the open directories in URLs.txt three levels deep:
python3 OnionAccelerator.py --mode crawl --max-depth 3

# Two seeds on the command line, no file involved:
python3 OnionAccelerator.py --mode crawl --url https://a.onion/dumps/ --url https://b.onion/

# What does each mode actually take?
python3 OnionAccelerator.py --full-help crawl
```

## Remote Archive Tree (`--mode tree`)

Reading a 2.4 GB `.rar` on an onion service to find out what is in it means transferring
2.4 GB — unless you only fetch the headers. That is what `rvtree` does, and `--mode tree`
runs it over OnionAccelerator's proxy fleet.

```bash
# What is in this archive, and what did it cost to find out?
python3 OnionAccelerator.py --mode tree --url https://example.onion/backup.rar

# The same thing spelled the long way. --url is inserted exactly here.
python3 OnionAccelerator.py --mode tree -- list https://example.onion/backup.rar

# Server capabilities and archive shape first — about 65 KiB.
python3 OnionAccelerator.py --mode tree -- probe https://example.onion/backup.tar.xz

# Pull one member out of it. --url lands before the member path, where rvtree wants it.
python3 OnionAccelerator.py --mode tree --url https://example.onion/backup.rar -- extract etc/hosts -o hosts

# Stream a huge tree as it is discovered, so an interrupted run keeps what it had.
python3 OnionAccelerator.py --mode tree -- list https://example.onion/big.tar.xz -f ndjson > tree.ndjson

# Through the external proxy list instead of a local farm.
python3 OnionAccelerator.py --mode tree --external --test-url http://your-own/ping -- list https://example.onion/a.7z

# A target behind a proof-of-work gate that then refuses ranges: pay for one full pass
# and keep the file (--spool-temp throws it away once the listing is printed).
python3 OnionAccelerator.py --mode tree --url https://example.onion/DEADBEEF/addresses.7z -- list --spool addresses.7z
```

**Why the farm matters here.** rvtree's own default is a single Tor daemon with several
credential-isolated circuits, and that is what makes it slow: one daemon is one guard and
one single-threaded crypto path, so the circuits queue behind each other. A recorded
production run listed a 2.4 GB RAR in **2 h 3 m**, spending 97.7% of that wall clock
waiting on 7,540 sequential 4 KiB round trips — every one of them on the *same* circuit.

`--mode tree` replaces that with the concept the download modes already use:

- **Endpoints, not circuits.** Each `host:port` is an independent Tor daemon with its own
  guard and its own bandwidth. Discovered from the farm automatically.
- **Round-robin lanes with failover.** A failed request parks its endpoint and retries on
  a different one, exactly as `ProxyPool` does for downloads.
- **Size-aware range splitting.** A bulk read — an xz block, a solid 7z folder — is split
  across lanes using the same `min(lanes, size // 1 MB)` rule as partial-download mode.
- **Hedged small reads.** A header read under 64 KiB is raced across two or three
  *different* daemons and the first answer wins. This is the only lever that helps a
  header chain, because the chain cannot be parallelised — entry *n+1*'s offset is not
  known until entry *n* has been read.
- **Parallel segmented walking.** For RAR and plain tar, the archive is divided into
  segments, a verified header is located in each (RAR5 by its CRC32, RAR4 by its CRC plus
  its successor, tar by its magic and checksum), and several walkers run at once. Walker 0
  starts where the format says the chain starts, and every later walker is believed only
  once the walker before it *arrives* at the offset it began from — so a boundary that
  turns out to be wrong costs time, never entries.

**Without a farm** the mode falls back to a local Tor client on `127.0.0.1:9150`, warns
that it is a single daemon, and suggests bringing a farm up. It works, but one daemon is
the thing that made it slow in the first place:

```bash
sudo python3 OnionAccelerator.py --farm up --count 8
python3 OnionAccelerator.py --mode tree -- list https://example.onion/backup.rar
```

rvtree writes its own full debug log to `logs/rvtree_<job_id>.log` for every run. Add `-v`
to watch the pipeline, or `-vv` for one line per range request.

### Gates, and servers that refuse ranges

Some services do not serve a download directly. The first GET of `file.7z` answers **200
with an HTML challenge page** — a proof-of-work interstitial that hands out an input and a
difficulty and only produces the file once a client hashes its way to a nonce and posts it
back. Nothing about that response says "this is not your archive": it is a 200, it has a
Content-Length, and every capability read off it describes the challenge page. rvtree
solves the challenge — it is a cost function, not a secret, and spending the CPU is
exactly what the gate asks of any client — and then reports that it did:

```console
$ python3 OnionAccelerator.py --mode tree -- probe http://example.onion/DEADBEEF/addresses.7z
size           1,877,268,564 bytes (1.7 GiB)
gate           proof-of-work — solved; the size above is the real entity
accept-ranges  NO — only --spool can read this archive, at a full download
```

Passing the gate is the easy half. The URL it grants is served by the application rather
than the web server, so it is single-use, non-resumable, and answers a `Range` request
with 200 and the whole body. That removes the premise this mode is built on, because a 7z
end header, a zip central directory and an xz index all live at the *far end* of the
stream — there is no way to reach them but to receive everything before them.

So `list` refuses, exits **3**, and prints what the alternative costs: 1.7 GiB at the
throughput it just measured, with no resume if the circuit dies. `--spool FILE` accepts
that — one stream to disk, then the archive is read locally at local speed. The file is
kept and a complete one is reused rather than fetched twice, which matters precisely
because a broken transfer restarts at zero. `--spool-temp` deletes it afterwards, for when
the listing was all you wanted. More endpoints do not help: every stream starts at byte
zero, so a second lane would only fetch the same bytes again.

## Open-Directory Crawl (`--mode crawl`)

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

### Reading is separate from crawling

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

### A parser with no server *markup* templates

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

### Templates: addressing, not markup

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
[`crawler/listing/templates/README.md`](crawler/listing/templates/README.md)** — three
worked examples and the full key reference.

### The target's own index, instead of a crawl

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

### Paths, as the server spells them

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

### Concurrency: circuits, not just threads

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

### Queueing and layers

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

### Output

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

## Project Structure

- `OnionAccelerator.py`: The main script containing all modes (multi-download, partial-download, speedtest, tree, crawl).
- `remote_viewer/`: The `rvtree` package behind `--mode tree`. Usable on its own too — see its own README.
- `crawler/`: The asyncio package behind `--mode crawl` — multi-circuit proxy pool, frontier, fetcher and reporting. The only async code in the project; the other modes stay on threads over `requests`.
- `crawler/listing/`: The scraping layer, with no crawl in it: the structural reader, the JSON/XML/row/manifest strategies, the addressing rules, and the template engine behind `--profile` and `--detect`. Adding support for a target is a file in `crawler/listing/templates/`, not a change to the crawler — see that directory's [README](crawler/listing/templates/README.md).
- `fullhelp.py`: The long-form per-mode reference printed by `--full-help`. Pure prose and stdlib, imported eagerly precisely because it can never fail.
- `requirements.txt`: Python dependencies.
- `URLs.txt`: A text file with one URL per line. Optional — `--url` replaces it.
- `UserAgents.tsv`: Tab-separated file; first column is the User-Agent string.
- `logs/`: A directory automatically created to store timestamped log files.
- `downloads/<host>/`: Output directory for multi mode, organised by hostname. With `--mode crawl --download` the remote directory structure is mirrored underneath it.
- `partials/<host>/`: Output directory for partial mode; temporary chunk files are merged here.
- `crawls/<job_id>/`: Manifests written by crawl mode (`listing.jsonl`, `dirs.jsonl`, `failed.jsonl`, `stats.json`, `tree.txt`, `urls.txt`).

## Requirements

- Python 3.7+ (3.8+ for `--mode crawl`, which is `aiohttp`'s own floor; 3.11+ for its listing templates, which are TOML read with the standard library's `tomllib` — on 3.8–3.10 install `tomli`)
- `requests[socks]` or `PySocks` for SOCKS5 support
- `tqdm` for progress bars
- `aiohttp`, `aiohttp-socks`, `beautifulsoup4`, `lxml` — `--mode crawl` only; the other modes run without them

## Native Tor Farm (`--farm`)

Instead of the Docker one-liner, OnionAccelerator can provision a farm of **native
Tor instances** using the Debian/Ubuntu `tor-instance-create` + systemd `tor@<name>`
stack — no containers. Each instance is a **client-only** SOCKS5 proxy bound to
`127.0.0.1:<base-port + N>`, matching what the download modes expect.

```bash
# Create, start, and wait for 20 instances to bootstrap (needs root/sudo):
sudo python3 OnionAccelerator.py --farm up --count 20

# Report each instance's systemd state, SOCKS port, and bootstrap %:
python3 OnionAccelerator.py --farm status

# Stop the farm (configs/data kept):
sudo python3 OnionAccelerator.py --farm down

# Stop and purge the farm entirely:
sudo python3 OnionAccelerator.py --farm destroy
```

- **Namespaced & safe.** The farm owns only instances named `oaNN` (e.g. `oa00`..`oa19`).
  Every privileged action is gated by a name guard, so `--farm down`/`destroy` can
  **never** stop or delete your own Tor relay/client instances (`default`, or anything
  you named yourself) — only `oa*` instances are ever touched.
- **Client-only.** Farm torrc files set `ClientOnly 1` with no `ORPort`/`DirPort`, so
  instances never publish a relay descriptor.
- **Bootstrap-aware.** `--farm up` waits until each instance reports `Bootstrapped 100%`
  and its SOCKS port is listening (bounded by `--bootstrap-timeout`, default 120s).
- **Configurable size/ports.** `--count` (default 20) and `--base-port` (default 5000)
  control how many instances and which ports; instance `NN` binds `base_port + NN`.

### Auto-discovery

In local (non-`--external`) mode, OnionAccelerator **probes** `127.0.0.1:BASE_PORT..`
and uses only the ports that are actually listening. A farm of any size (10, 15, 20…)
— or a set of Docker/manual proxies — just works without editing constants:

```
[INFO] Discovered 12 live local Tor SOCKS ports (5000-5011).
```

If nothing is listening, it warns and suggests `--farm up`.

### Full pipeline

```bash
sudo python3 OnionAccelerator.py --farm up --count 20   # bring proxies up
python3 OnionAccelerator.py --mode speedtest            # sanity-check them
python3 OnionAccelerator.py --mode multi                # download
sudo python3 OnionAccelerator.py --farm destroy         # tear down when done
```

## Alternative: wrap Tor proxies with Docker (Bash one-liner)

```bash
for port in {5000..5020}; do     docker run -d --name "torproxy_$port" -p 127.0.0.1:$port:9050 dperson/torproxy; done
```

## External Proxy List (`--external`)

Instead of spinning up local Docker Tor instances, pass `--external` to pull a ready-made list of remote `ip:port` SOCKS5 proxies:

```bash
python3 OnionAccelerator.py --mode multi --external
```

- **Fetched fresh every run.** The list is downloaded from the public source on each invocation and is **never written to disk**, so every execution uses an up-to-date set of proxies.
- **Capped at 100 proxies.** If the list is larger, a random subset of 100 is selected (this is also the hard limit on worker threads).
- **Connectivity-checked before any work starts.** Each candidate proxy is tested in parallel against a single connectivity-check URL; only proxies that successfully return data are used. The run aborts if none pass. By default that check URL is a random one of your own targets, from `--url` or `URLs.txt` — set `--test-url` (or the `PROXY_TEST_URL` config constant) to a private endpoint you control so real targets are never exposed to discarded proxies. Note also that targets passed with `--url` are visible in shell history and in `ps` output, which a file is not.
- **Pooled with failover.** Proxies are drawn from a shared, thread-safe pool rather than pinned to a worker or chunk by index. When a download attempt fails, its proxy is parked and the retry lands on a *different* live proxy, so a single dead endpoint no longer sinks a whole file or URL. A proxy that later succeeds is rehabilitated, and if every proxy is parked the pool falls back to reusing them.

## Contributing

Pull requests and suggestions for improvements are welcome. Feel free to open an issue if you encounter any problems or have questions regarding advanced Tor configurations.
