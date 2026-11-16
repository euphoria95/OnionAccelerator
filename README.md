# OnionAccelerator

OnionAccelerator is a multi-functional Python script designed for discovering and downloading files through multiple SOCKS5 proxies (commonly Tor instances). It supports six modes:

## Modes

### Multi-Download Mode

- Downloads many files at once over a shared set of **lanes** — one lane is one isolated Tor circuit with its own SOCKS credential and its own User-Agent, pinned for the lane's life. Concurrency is the lane count (endpoints × `--circuits-per-endpoint`), not a fixed twenty.
- Cuts every file larger than `--piece-size` (4 MiB by default) into pieces that any free lane takes next, so a fast circuit does more of the work and a stalled one is abandoned (see **The download engine** below).
- Aggregates download progress via a single progress bar in the terminal.

### Partial-Download Mode

- The same engine, one file at a time with every lane working on it, into `partials/<host>/`. There is no merge pass: each piece is written at its offset into one preallocated file, so memory is lanes × piece size however large the file is.
- Establishes the file size through a ladder of four probes, so a server that withholds `Content-Length` no longer costs you the parallelism: a `HEAD`, then the `size=` parameter of that same response's `Content-Disposition`, then the total in the `Content-Range` of a one-byte `Range: bytes=0-0` GET, and finally a plain streamed GET aborted the moment its headers arrive. No probe transfers a payload.
- If none of the four can establish a size, or the file fits in one piece, it is fetched as a single GET with nothing to resume.
- A piece that runs out of `--retries` stops its file and leaves it resumable: nothing is deleted, and rerunning the same command carries on.

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

### Persistent Index Mode (`--mode index`)

Every crawl and archive listing lands in one SQLite file, scored and searchable, so the
question changes from "what is on this site" to "what is new, and what touches us". See
**The persistent index** below.

### Live Streaming API (`--stream`)

Any crawl, tree listing or detection can publish what it finds *while it runs*, as
newline-delimited JSON over HTTP on loopback, for an external script to grep or index —
so a keyword hit during a four-hour Tor crawl arrives at minute three instead of at the
end. See **Live Streaming API** below.

### Speedtest & Healthcheck Mode

- Tests all SOCKS5 ports **in parallel** and shows a summary of per-port speeds and failures (with emoji indicators).
- Availability checks for the full URL list use a randomly selected working port rather than always defaulting to port 5000.

## Key Features

### Multiple Threads & SOCKS5 Proxies

- Discovers the live local SOCKS5 ports of a farm (by default `127.0.0.1:5000–127.0.0.1:5019`), falls back to a personal Tor client on `127.0.0.1:9150` when there is none, and stops with an error when there is neither — the same ladder for download, tree and crawl. `--socks` names your own endpoints.

### Progress Bars

- Utilizes `tqdm` to show clear download progress:
  - A single aggregated bar for multi-downloads.
  - A dedicated bar per file in partial-download mode, with its total filled in once the size is known.

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

- In multi and partial mode, if none of the four size probes can establish a file size, the script automatically switches to a single GET request and proceeds with a standard download. The same applies when a response arrives under a `Content-Encoding`: the length it advertises describes the compressed transfer rather than the file, so it is not safe to split into ranges.

## Installation

1. **Install Python 3.11 or newer.**

2. **Install OnionAccelerator** from the checkout, into a virtualenv:

    ```bash
    python3 -m venv .venv && . .venv/bin/activate
    pip install -e .
    ```

    This installs the `onionaccelerator` command along with every dependency, and
    `rvtree` (for `--mode tree`) with it. `onionaccelerator ARGS` and
    `python3 OnionAccelerator.py ARGS` are the same program; the examples below use the
    second form, which also runs from an uninstalled checkout whose dependencies are
    already present. Add `[dev]` (`pip install -e '.[dev]'`) for pytest and ruff.

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
python3 OnionAccelerator.py --farm <up|status|down|destroy> [--count N] [--base-port PORT] [--bootstrap-timeout SEC] [--farm-tune off|onion|onion-aggressive]

# Download / speedtest through the proxies:
python3 OnionAccelerator.py --mode <multi|partial|speedtest> [--url URL ...] [--retries N] [--piece-size SIZE] [--fresh] [--external] [--test-url URL]

# Read a remote archive's file tree without downloading it:
python3 OnionAccelerator.py --mode tree [--url URL] [--external] [-- <rvtree arguments>]

# Recursively map an open directory (and optionally download what it finds):
python3 OnionAccelerator.py --mode crawl [--url URL ...] [--max-depth N] [--order bfs|dfs] [--socks host:port,...] [--download]

# Ask what a target is before crawling it, and see what can be read:
python3 OnionAccelerator.py --mode crawl --detect [--url URL ...]
python3 OnionAccelerator.py --list-profiles

# Mirror a crawl or a tree listing to an external script while it runs:
python3 OnionAccelerator.py --mode <crawl|tree> [--url URL] --stream [HOST:PORT]
curl -sN 'http://127.0.0.1:8787/events?kinds=crawl.file&match=(?i)keyword'

# The per-mode reference: what each mode does and which flags reach it.
python3 OnionAccelerator.py --full-help [<multi|partial|speedtest|tree|crawl|farm>]
```

Exactly one of `--farm` or `--mode` must be given.

- `--farm`: manage a farm of native Tor instances instead of Docker (see **Native Tor Farm** below).

- `--mode`:
  - `multi`: Many files at once, each cut into resumable pieces fetched by whichever lane is free.
  - `partial`: One file at a time with every lane on it, in resumable pieces written straight into place.
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

# Partial-download mode, every lane on one file at a time; killed? run it again and it resumes:
python3 OnionAccelerator.py --mode partial

# Bigger pieces over more lanes, or throw away a partial download and start over:
python3 OnionAccelerator.py --mode partial --piece-size 16M --circuits-per-endpoint 4
python3 OnionAccelerator.py --mode partial --url https://example.onion/backup.iso --fresh

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

## The download engine (`--mode multi`, `--mode partial`, `crawl --download`)

One engine moves every file, and it is built to be interrupted. A sized file is cut into
fixed **pieces** (`--piece-size`, 4 MiB by default). Each lane — one isolated Tor circuit —
pulls the next piece from a shared queue, so a fast lane does more of them, and writes it
at its offset into a single preallocated file. A lane holds at most one piece in memory, so
the footprint is lanes × piece size whatever the file weighs.

**On disk, while it runs** (`<name>` is the file's final path):

| File | What it is |
|---|---|
| `<name>.oa-part` | the file itself, preallocated, pieces written in place |
| `<name>.oa-state` | an append-only JSON-lines journal: a job header, then one line per finished piece with its SHA-256, lane and attempts. Each line is one `write()`, so a kill tears at most the last, which the reader drops |

**Resume.** Run the same command again. The journal is read, every piece it calls finished
is re-hashed against `<name>.oa-part`, and only the rest is fetched. A journal that no
longer fits (a different size, piece size or ETag) is discarded rather than trusted;
`--fresh` discards it on purpose. A piece that runs out of `--retries` stops that file and
**deletes nothing** — the process exits 1 and the file is resumable. No new pieces are taken
once one gives up, but the ones already in flight get up to ten seconds to land, since
over Tor each is minutes of someone's bandwidth and the rerun will not have to fetch it.

**A file that changes underneath it.** The size probe's `ETag` (or `Last-Modified` when
the ETag is weak or absent) is sent as `If-Range` with every piece. A server that sees a
different version answers `200` with the new entity; a server that ignores `If-Range` gives
itself away by a different `ETag` on the `206`, or a different total in `Content-Range`.
Either way the file starts over from byte zero **once**, so the result is never spliced
from two versions, and fails if it changes again. A server that answers ranged requests
with `200` regardless gets the same one restart and then a single plain GET. A server that
sends no validators at all can only be caught by a change of size, and the log says so.

**Slow and stalled lanes.** A piece that has run far below the median lane (under a tenth
of it, for 20 seconds) is cancelled, requeued, and its lane rotated onto a new circuit. The
rule is relative on purpose: when every lane is slow the median is low too and nothing is
cancelled. Among the last few pieces, one that is **running late** — twice the median finished
piece, and never before a second — is raced on a second lane, and the first to finish wins, so
one bad circuit cannot hold a finished file hostage. Only a late piece is raced: when the lanes
are equal the pieces finish together, and racing them anyway would just fetch the same bytes
twice (it did, until a benchmark measured it).

**Custody record.** When a file completes, the journal and the part file are gone and
`<name>.oa.json` is written beside it:

```json
{
  "kind": "custody", "job_id": "20261113_103000_ab12cd", "url": "http://example.onion/a.iso",
  "path": "partials/example.onion/a.iso", "size": 734003200,
  "sha256": "…whole-file SHA-256, computed from what is on disk…",
  "etag": "\"5d8c72a5edda8\"", "last_modified": "Wed, 01 Oct 2025 08:00:00 GMT",
  "piece_size": 4194304, "pieces": 175, "resumed_pieces": 0, "restarts": 0,
  "lanes": [{"lane": 0, "endpoint": "socks5h://127.0.0.1:5000", "pieces": 9}],
  "started_at": "2026-11-13T10:30:00Z", "finished_at": "2026-11-13T10:41:12Z"
}
```

A file with no discoverable size, or that fits in one piece, is a single GET: there is
nothing to resume, so a failure leaves nothing behind.

**Identity.** Every lane keeps one User-Agent for its whole life — one lane is one identity
as far as the far end can tell — and the size probe goes out on a lane's own credentialed
proxy, not the daemon's shared circuit. Endpoints are resolved the way tree and crawl do it
(`--socks`, `--external`, the farm probe, then a personal Tor client on `127.0.0.1:9150`);
the old fixed twenty-port fallback and the 20-worker cap are gone.

With `--stream`, the transfer publishes `download.start`, `download.piece`, `download.done`
and `download.fail` (see **Live Streaming API**). A piece event is the journal line written
for it and `download.done` is the custody record, byte for byte; the start and fail events
of a single-GET file have no journal to match.

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

## The persistent index (`--mode index`)

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

### The schema

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

### Scoring, and saying why

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

### Diff

`diff --target HOST` compares the last two sightings (`--since RUN` / `--until RUN` to
choose). **Removed** is a claim, so it is made carefully: a file is removed when its
directory was listed again and it was not in it. A directory the later crawl never reached
— it was cut short (`max_pages`, `time_budget`), or gave up on a subtree
(`incomplete`) — leaves its contents **unknown**, never removed. An archive's members are
removed only if the archive was walked again. The index sees paths, not intent: a directory
renamed from `/old` to `/new` is `/old` removed and `/new` added, and the same names and
sizes on both sides are the tell.

### Selective fetch

`fetch --min-score N` selects files scoring at least N (best first, one per download URL,
skipping what is already downloaded unless `--again`) and hands them to the download engine
with the flags the download modes take (`--piece-size`, `--retries`, `--fresh`, `--socks`,
`--circuits-per-endpoint`) and the same proxy ladder as crawl and tree. A hit **inside an
archive** is never downloaded whole: it goes to rvtree's `extract`, bounded by
`--max-fetch MiB` (default 64), and lands under `downloads/<host>/<archive>.members/` with
every path component sanitised, so a hostile member name cannot write outside the output
directory. Custody records are linked back to the entry they fetched. `--dry-run` lists
without fetching.

### Export

`export --format jsonl|csv` writes each entry at or above `--min-score` with its host,
your label/group/victim, every rule that fired, the runs that saw it, and the SHA-256 of
anything downloaded. `--out FILE` is created `0600` and is not overwritten without
`--force`. **CSV cells beginning `=`, `+`, `-`, `@`, tab or carriage return are prefixed
with an apostrophe**: every path was written by the server being investigated, and the
file is the one an analyst opens in a spreadsheet. STIX export is deliberately not
provided.

### Scale

A million entries ingest in minutes on a laptop and a path search returns in well under a
second (`tests/test_index_scale.py`, marked `slow`; the measured numbers are in the pull
request that added it, not estimated here).

## Live Streaming API (`--stream`)

The runs this tool is for take hours. A crawl of a leak site's file manager over Tor, or
a listing of a 2 GB archive by range requests, writes everything it learns to disk — but
not until it stops. `--stream` publishes each finding as it happens, over HTTP, for
something else to read while the run is still going:

```bash
python3 OnionAccelerator.py --mode crawl --url http://target.onion/ --stream &

# every file whose name or path matches, as it is discovered
curl -sN 'http://127.0.0.1:8787/events?kinds=crawl.file&match=(?i)payroll'
```

That is the whole point: during an incident, a hit at minute three can steer the rest of
the collection, and a hit at hour four cannot. It works the same way for `--mode tree`,
where each member of a remote archive is published as it is walked out of the headers,
and for `--detect`.

### Endpoints

| Endpoint | What it answers |
|---|---|
| `GET /events` | one JSON object per line, forever, until the run ends |
| `GET /status` | the run's counters, the current cursor, and who is attached |
| `GET /schema` | every event kind and what it means — a consumer can be written against the running server rather than against this file |
| `GET /health` | liveness |
| `GET /` | a plain-text reminder of all of the above |

`/events` takes:

| Parameter | Meaning |
|---|---|
| `since=SEQ` | replay everything after this sequence number, then follow. This is how a consumer reconnects without a hole |
| `kinds=A,B` | only these kinds. A family prefix works: `kinds=crawl` takes every `crawl.*` |
| `match=REGEX` | server-side filter, applied to the whole line |
| `shape=ndjson\|line` | `line` emits only the URL or path — a list you can pipe into `wget -i` or `sort -u` |
| `heartbeat=S` | seconds between keepalives on an idle stream; `0` disables |

### The events

Every line carries the same envelope, so a consumer routes on `kind` without knowing
which producer it came from:

```json
{"seq":412,"ts":"2026-08-23T18:22:01Z","run":"20260823_182034","mode":"crawl",
 "kind":"crawl.file","data":{"url":"http://target.onion/hr/2019/payroll.xlsx",
 "name":"payroll.xlsx","size_bytes":48219,"depth":3,"parent":"http://target.onion/hr/2019/",
 "endpoint":"127.0.0.1:5001","discovered_at":"2026-08-23T18:22:01Z"}}
```

`data` is **byte-for-byte the record the run writes to disk** — a `crawl.file` event is a
line of `listing.jsonl`, a `tree.entry` is what `-f ndjson` prints. An index built from
the stream and one built from the artefacts afterwards cannot disagree, which is the only
reason a live stream is safe to base a conclusion on.

| Kind | Carried |
|---|---|
| `run.start` / `run.stop` | the target and configuration; then why it stopped, and the totals |
| `crawl.dir` / `crawl.file` | a listed directory / a discovered file — the hunting events |
| `crawl.skip` | a page that was fetched and read but refused by the index-confidence guard |
| `crawl.fail` | one failed attempt, `final` marking the one that exhausted `--retries` |
| `crawl.progress` | periodic counters, so a consumer can tell "finding nothing" from "stuck" |
| `crawl.page` | fetched page text, truncated — only with `--stream-bodies`, never written to disk |
| `tree.open` / `tree.entry` / `tree.done` | the archive, each member as it is walked, and the cost |
| `download.start` / `download.piece` / `download.done` / `download.fail` | a file's transfer under `--mode multi`, `partial` or `crawl --download`: the job, each finished piece, the custody record, and why a file stopped |
| `detect.result` | which listing templates read a target, and how well |
| `stream.hello` / `stream.gap` / `stream.heartbeat` | the protocol's own |

### Falling behind

A consumer that reads slower than the run publishes loses its oldest events — the run is
never blocked by a grep — and is told so in band:

```json
{"kind":"stream.gap","data":{"lost":57,"resuming_at":958,"hint":"this consumer fell behind; the run's JSONL on disk is complete"}}
```

Silent loss would be the worst failure this feature could have: an incomplete keyword
index that looks complete. So the loss is counted exactly, reported once, and the JSONL
in `crawls/<job_id>/` remains the complete record to reconcile against.

### Flags

| Flag | Effect |
|---|---|
| `--stream [HOST:PORT]` | enable; default `127.0.0.1:8787`. A bare port works, and port `0` picks a free one and logs which |
| `--stream-token TOKEN` | require `Authorization: Bearer TOKEN` (or `?token=`). **Mandatory for any bind that is not loopback** |
| `--stream-buffer N` | events kept for replay (default 10000) |
| `--stream-bodies` | also publish page text, capped by `--stream-body-bytes` (default 64 KiB) |
| `--stream-wait` | hold the run until a consumer has attached, so the index sees the first finding |

The stream carries the target's URLs and the names of its files, so the defaults are the
careful ones: loopback only, no CORS, the `Host` header checked so a page in a browser
cannot reach it by rebinding DNS, and a flat refusal — an error, not a warning — to bind
anywhere but loopback without a token. To watch a run on another machine, forward the
port over SSH rather than binding it wide.

### A consumer to start from

`contrib/streamhunt.py` is a working example and about a hundred lines long: it takes a
keyword file (one per line, `/regex/` for a regex), matches every finding against it,
writes hits as JSONL, reconnects from its last sequence number if the socket drops, and
records any gap as a hit of its own.

```bash
python3 OnionAccelerator.py --mode crawl --url http://target.onion/ --stream --stream-wait &
python3 contrib/streamhunt.py --keywords scope.txt --out hits.jsonl
```

```
[+] Acme Ltd.: http://target.onion/clients/acme/
[+] /payroll_\d{4}/: http://target.onion/hr/payroll_2019.xlsx
[*] 4812 event(s) searched, 2 hit(s), 0 lost, 143s
```

The stream is read-only. There is no way to steer a run through it — no seed injection,
no stop command — by design.

### Waiting for a target to go live (`contrib/sensor.py`)

Some targets — a leak site's per-victim "final" endpoint, most often — do not serve
anything crawlable the moment they are known about. `contrib/sensor.py` polls one URL
with `--mode crawl --detect` on an interval, and the moment that call reports the target
usable, it stops polling and feeds the newest job directory under `crawls/` to
`--resume` — so a crawl started early (or cut short because the target was not up yet)
continues instead of starting over:

```bash
python3 OnionAccelerator.py --mode crawl --url http://target.onion/dl/1/ --max-pages 1 &
wait   # let the first pass fail/park, leaving a frontier journal to resume from

python3 contrib/sensor.py --url http://target.onion/site/data?uuid=... \
    --interval 300 -- --socks 127.0.0.1:5000,127.0.0.1:5001
```

Everything after the `--` is passed through unchanged to both the `--detect` poll and
the `--resume` run it triggers. `--resume-dir JOB_DIR` picks a job explicitly instead of
the newest one; `--max-polls N` gives up instead of polling forever. It never opens a
socket itself — detection and the resumed crawl both go through
`OnionAccelerator.py`, so proxy selection and the farm's own safety rails apply exactly
as they would to a run started by hand.

## Project Structure

- `Dockerfile`: the farm as a container (`--farm run`, process backend).
- `OnionAccelerator.py`: The command line, speedtest, proxy discovery and the glue for tree, crawl and stream. Installed as the `onionaccelerator` command.
- `oacore/`: The download engine (`engine.py`) and the modes built on it (`download.py`), the remote size probe (`sizeprobe.py`), the native farm (`farm.py`: the backend interface and the systemd backend; `farm_process.py`: the rootless backend; `torcontrol.py`: its control-port client) and the settings and helpers they share. `OnionAccelerator.py` re-exports all of it.
- `remote_viewer/`: The `rvtree` package behind `--mode tree`. Usable on its own too — see its own README.
- `crawler/`: The asyncio package behind `--mode crawl` — multi-circuit proxy pool, frontier, fetcher and reporting. The only async code in the project; the other modes stay on threads over `requests`.
- `crawler/listing/`: The scraping layer, with no crawl in it: the structural reader, the JSON/XML/row/manifest strategies, the addressing rules, and the template engine behind `--profile` and `--detect`. Adding support for a target is a file in `crawler/listing/templates/`, not a change to the crawler — see that directory's [README](crawler/listing/templates/README.md).
- `index/`: The persistent index behind `--mode index` — the SQLite store, the ingester shared by the offline and live paths, rule packs (`index/packs/`), diff, search, export and the selective-fetch hand-off. Standard library only.
- `eventstream/`: The live streaming API behind `--stream` — the event bus, the query filters and the HTTP server. Standard library only, so switching it on cannot fail on a host that has installed nothing.
- `contrib/streamhunt.py`: A reference consumer of that stream: keyword hunting with reconnect and gap accounting, in stdlib Python. The minimal example; `--mode index live` is the same idea with a database behind it.
- `contrib/sensor.py`: Polls a target with `--detect` until it looks live, then `--resume`s the newest job under `crawls/` at it.
- `fullhelp.py`: The long-form per-mode reference printed by `--full-help`. Pure prose and stdlib, imported eagerly precisely because it can never fail.
- `pyproject.toml`: Packaging, dependency floors, and the pytest and ruff configuration.
- `URLs.txt`: A text file with one URL per line. Optional — `--url` replaces it.
- `UserAgents.tsv`: Tab-separated file; first column is the User-Agent string.
- `logs/`: A directory automatically created to store timestamped log files.
- `downloads/<host>/`: Output directory for multi mode, organised by hostname. With `--mode crawl --download` the remote directory structure is mirrored underneath it.
- `partials/<host>/`: Output directory for partial mode. Unfinished files sit here as `<name>.oa-part` beside `<name>.oa-state`.
- `crawls/<job_id>/`: Manifests written by crawl mode (`listing.jsonl`, `dirs.jsonl`, `failed.jsonl`, `stats.json`, `tree.txt`, `urls.txt`).

## Requirements

- Python 3.11+, for every mode. The crawler's listing templates are TOML read with the standard library's `tomllib`.
- `requests` and `PySocks` for SOCKS5 support
- `tqdm` for progress bars
- `aiohttp`, `aiohttp-socks`, `beautifulsoup4`, `lxml` — used by `--mode crawl` and `--detect` only. `pip install` brings them anyway, but they are imported lazily, so the other modes still run on a host where they fail to import.

The version floors are in `pyproject.toml`. CI tests them as written, on the oldest supported Python.

## Native Tor Farm (`--farm`)

Instead of the Docker one-liner, OnionAccelerator can provision a farm of **native
Tor instances** — no containers. Each instance is a **client-only** SOCKS5 proxy bound to
`127.0.0.1:<base-port + N>`, matching what the download modes expect. Two backends run it:

| `--farm-backend` | What it is | Needs |
|---|---|---|
| `process` (default unless root) | plain `tor -f` children under `~/.local/state/onionaccelerator/farm` (or `$ONIONACCELERATOR_FARM_DIR`), one DataDirectory, pidfile and control socket each | any Linux or macOS user with a `tor` binary (`$ONIONACCELERATOR_TOR` names one) |
| `systemd` | the original: `tor-instance-create` + `tor@<name>` units | root, Debian or Ubuntu |

```bash
# Create, start, and wait for 20 instances to bootstrap — as an ordinary user:
python3 OnionAccelerator.py --farm up --count 20

# Report each instance's state, SOCKS port, bootstrap % and circuits:
python3 OnionAccelerator.py --farm status

# Stop the farm (configs/data kept):
python3 OnionAccelerator.py --farm down

# Stop and purge the farm entirely:
python3 OnionAccelerator.py --farm destroy

# Stay in the foreground, restart what dies, stop cleanly on SIGTERM (containers, user services):
python3 OnionAccelerator.py --farm run --count 8

# The systemd stack, as it always was:
sudo python3 OnionAccelerator.py --farm up --count 20 --farm-backend systemd
```

**Health comes from tor, not from a log.** Each process-backend instance has a
`ControlSocket` with cookie authentication, mode `0600` in a `0700` directory. `--farm up`
waits for `GETINFO status/bootstrap-phase` to say `PROGRESS=100` and the SOCKS port to be
listening, and `--farm status` shows each instance's pid, bootstrap tag and built/total
circuits from `GETINFO circuit-status`. A tor that is stuck at 40% while its log claims
100% is reported at 40%. The client behind this is `oacore/torcontrol.py`: standard
library only, and it speaks exactly `AUTHENTICATE`, those two `GETINFO` keys, and
`SIGNAL NEWNYM` / `SIGNAL SHUTDOWN` — nothing else. The control port is for bootstrap,
health and telemetry; it is not a second way to rotate circuits, since a fresh SOCKS
credential already buys one. A crawl run over a farm instance records its circuit counts
under `tor_circuits` in `stats.json`.

**What it will and will not touch.** Beyond the `oaNN` name guard below, the process
backend refuses a state directory it did not create (no `.oa-farm` marker), removes only
instance directories that carry its own marker and sit directly inside the state
directory (a symlink out of it, or a hand-made `oa07`, is left alone), and signals a pid
only after confirming its command line is `tor -f <this instance's torrc>` — a pidfile
outlives its process and pids get reused. A port that something else already holds
refuses the whole `up` before anything is created.

- **Namespaced & safe.** The farm owns only instances named `oaNN` (e.g. `oa00`..`oa19`).
  Every privileged action is gated by a name guard, so `--farm down`/`destroy` can
  **never** stop or delete your own Tor relay/client instances (`default`, or anything
  you named yourself) — only `oa*` instances are ever touched.
- **Client-only.** Farm torrc files set `ClientOnly 1` with no `ORPort`/`DirPort`, so
  instances never publish a relay descriptor.
- **Bootstrap-aware.** `--farm up` waits until each instance reports bootstrap 100% and
  its SOCKS port is listening (bounded by `--bootstrap-timeout`, default 120s). The
  process backend asks tor over its control socket; the systemd backend still reads the
  tail of `notices.log`.
- **Configurable size/ports.** `--count` (default 20) and `--base-port` (default 5000)
  control how many instances and which ports; instance `NN` binds `base_port + NN`.

### Tuning the farm for onion crawling (`--farm-tune`)

```bash
sudo python3 OnionAccelerator.py --farm up --count 20 --farm-tune onion
python3 OnionAccelerator.py --farm status        # shows tune=onion per instance
```

`--farm-tune` appends torrc options to every instance. The default, `off`, is the farm's
torrc exactly as before, so upgrading restarts nothing. The candidates came from the
tuning guide *Tor Client Performance Tuning* (section numbers are its); what made the cut
is what fixes something observed on a real farm, after an A/B benchmark (below) showed
the guide's latency knobs make no measurable difference here:

| Line | Profile | Why |
|---|---|---|
| `SocksPort … ExtendedErrors` | onion | tor says *why* an onion failed (`0xF0` descriptor gone, `0xF2` intro failed, `0xF3` rendezvous failed, `0xF6` bad address…) instead of `host unreachable` for all of them. The crawler acts on it, see below. §7.3 |
| `DormantClientTimeout 7 days`, `DormantCanceledByStartup 1` | onion | with the 24 h default every instance went dormant between jobs (visible in each `notices.log`), so the next job started cold. §4.4 |
| `CircuitsAvailableTimeout 21600` | onion | keeps predicted onion circuits built across gaps between jobs (default 30–60 min). §5.2 |
| `MaxMemInQueues <N> MB` | onion | half the host's RAM split across `--count` instances (256 MB–1 GB each); otherwise every instance budgets for the whole host. §10.2 |
| `MaxCircuitDirtiness 1800` | onion-aggressive | **Trade-off**: reuses a circuit for 30 min instead of 10 — fewer builds, more linkable. §5 |

Deliberately **not** included: `MaxClientCircuitsPending` and `SocksTimeout` (measured no
gain, see below; every mode here already times out sooner than tor would), Conflux (exit
traffic only; onions never use it), `CircuitStreamTimeout` (a PoW-defended onion
legitimately takes tens of seconds), and anything that narrows path selection —
`ExitNodes`, `EntryNodes`, `NumEntryGuards`, padding cuts. The target is an observer too,
and a crawler with a pinned path clusters in its logs. Congestion control, onion PoW and
vanguards-lite are already on by default in tor 0.4.8+, so there is nothing to add for
them; `--farm up` only warns if this tor lacks the `pow` module.

**What the crawler does with ExtendedErrors.** A missing descriptor, failed or timed-out
introduction backs off and retries without burning a circuit rotation (the fault is at
the far end); a failed rendezvous rotates; a malformed address, broken descriptor or
missing client auth is dropped at once. None of them count against the daemon's health,
so a dead target can no longer park every healthy farm instance. The download modes
translate the codes in their log lines (PySocks prints `0xf0: Unknown error`).

**Applying it to a running farm.** `--farm up` compares each instance's torrc with the
new one. Changed options are applied with a reload (SIGHUP), which keeps the instance's
circuits and descriptor cache. A changed `SocksPort` line — turning ExtendedErrors on or
off — needs a restart, because tor keeps a listener's flags across a reload; those
instances are restarted two seconds apart so a crawl running on the farm loses one
endpoint at a time.

**What was measured (2026-09-29).** A self-hosted onion (Range-capable static server, a
351-directory open index) on the same host as two sets of four user-level tor 0.4.9.13
clients: the farm's baseline torrc versus a tuned one (everything above plus
`MaxClientCircuitsPending 96` and `SocksTimeout 90`). Arms were interleaved request by
request, then **crossed over** — configs swapped between the two sets, keeping each
set's DataDirectory and guard — so a difference that is really guard luck flips sign
while a config effect does not.

| Test | Baseline | Tuned | Verdict |
|---|---|---|---|
| Cold TTFB, fresh rendezvous per request (80 per config) | p50 1.60 s | p50 1.68 s | no difference (p=0.85) |
| Warm TTFB, reused circuit (80 per config) | p50 0.77 s | p50 0.82 s | no difference pooled; each phase alone was "significant" (p≈0.004) **in opposite directions** — it follows the guard |
| 10 MiB throughput, warm circuit (8 per config) | 404 KiB/s | 341 KiB/s | no difference (p=0.19); congestion control confirmed active on onion circuits (`CWND` in CIRC_BW) |
| 64 concurrent cold connects on one instance (16 bursts per config) | median 10.4 s, 4/16 bursts with failures | median 10.6 s, 3/16 | no difference (p=0.79 / 1.0). Stalled bursts end at `SocksTimeout`: 120 s vs 90 s |
| `--mode crawl`, 351 dirs, 4×8 lanes (4 per config) | 87 / 33 / 28 / 32 s | 32 / 26 / 136 / 172 s | follows the instances, not the config |
| `--mode partial`, 64 MiB over 4 instances (6 per config) | 94–286 s | 67–92 s | tuned faster in both phases, but back-to-back runs share one uncredentialed circuit per instance, so this is ~2 independent samples a side: not evidence |
| Dead onion (valid v3 address, no descriptor) | `0x04` host unreachable, 5/5 | `0xF0` descriptor not found, 5/5 | ExtendedErrors works; time to fail no different |
| First onion request 20 s after a warm restart, with/without a warm-up request to another onion | — | — | no difference (p=0.86); 3/10 failed outright either way, which is why `--farm up` reloads rather than restarts where it can |

The honest summary: against an onion, per-request speed is set by the path the guard
and rendezvous point give you, and no client torrc line moved it. What `--farm-tune`
buys is a farm that is awake and not over-committing memory when a job starts, and a
crawler that can tell a dead target from a dead circuit.

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

## Alternative: a container (`Dockerfile`)

The `Dockerfile` at the repository root runs the process backend as the container's main
process (`--farm run`): an unprivileged user, no systemd, the instances stopped cleanly when
the container is.

```bash
docker build -t onionaccelerator .
# The SOCKS ports are bound to the container's loopback, so share the host's network
# namespace to use them from the host (and keep them off every other interface):
docker run -d --name oa-farm --network host -e OA_COUNT=8 onionaccelerator
python3 OnionAccelerator.py --mode speedtest          # finds 127.0.0.1:5000-5007
docker stop oa-farm                                   # SIGTERM: instances shut down cleanly
```

`OA_COUNT` and `OA_BASE_PORT` set the farm's size and first port (8 and 5000). Mount a
volume at `/home/oa/.local/state/onionaccelerator` to keep the instances' consensus and
guards between runs. This replaces the earlier one-liner that started a `dperson/torproxy`
container per port: one container per proxy cannot share a guard set or be health-checked
from tor, and the image it pulled is not this project's to vouch for.

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
