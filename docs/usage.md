# Using OnionAccelerator

## Key features

## Multiple Threads & SOCKS5 Proxies

- Discovers the live local SOCKS5 ports of a farm (by default `127.0.0.1:5000–127.0.0.1:5019`), falls back to a personal Tor client on `127.0.0.1:9150` when there is none, and stops with an error when there is neither — the same ladder for download, tree and crawl. `--socks` names your own endpoints.

## Progress Bars

- Utilizes `tqdm` to show clear download progress:
  - A single aggregated bar for multi-downloads.
  - A dedicated bar per file in partial-download mode, with its total filled in once the size is known.

## Retry Logic

- Failed downloads and individual chunks are retried inline (up to `--retries` attempts) without re-queuing, avoiding potential deadlocks when the worker pool empties mid-retry.

## Collision-Free Output Paths

- Downloaded files are stored under a hostname subdirectory (e.g., `downloads/example.onion/file.zip`), so URLs from different hosts that share the same filename never overwrite each other.

## Logging

- Generates a unique `job_id` each time the script is run.
- Writes detailed logs (DEBUG level) to a timestamped file in the `logs/` directory.
- Outputs essential logs (INFO level) to the console.

## User-Agents

- Loads random User-Agent strings from the first column of `UserAgents.tsv` (tab-separated; subsequent columns such as usage weights are ignored).

## Seamless Fallback

- In multi and partial mode, if none of the four size probes can establish a file size, the script automatically switches to a single GET request and proceeds with a standard download. The same applies when a response arrives under a `Content-Encoding`: the length it advertises describes the compressed transfer rather than the file, so it is not safe to split into ranges.

## Usage

## Say what to fetch

Either way works, and you never need both:

- **`--url URL`** on the command line. Repeat it for several. When `--url` is given at
  least once it replaces the file entirely — `URLs.txt` is not read and need not exist.
- **`URLs.txt`** in the working directory, one URL per line, blank lines ignored. This is
  the fallback when `--url` is absent, and the right shape for a long list.

`--url` puts your targets into shell history and `ps` output; `URLs.txt` does not. On a
shared or logged host that difference matters.

## Run the script:

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

- `--farm`: manage a farm of native Tor instances instead of Docker (see [Native Tor Farm](farm.md)).

- `--mode`:
  - `multi`: Many files at once, each cut into resumable pieces fetched by whichever lane is free.
  - `partial`: One file at a time with every lane on it, in resumable pieces written straight into place.
  - `speedtest`: Test download speed and basic health for each SOCKS5 proxy using the first target.
  - `tree`: List or extract members of a remote archive over the proxy pool without downloading it. Takes its target from `--url` or from the arguments after `--`, never from `URLs.txt` (see [Remote Archive Tree](modes/tree.md)).
  - `crawl`: Recursively walk the open directories seeded from `--url` or `URLs.txt` and write a manifest of every file found, spread across many Tor circuits at once (see [Open-Directory Crawl](modes/crawl.md)).

- `--url URL`: A target URL given on the command line instead of in `URLs.txt`. Repeatable, and when present it replaces the file entirely. Under `--mode tree` it becomes rvtree's URL argument wherever that argument belongs, so `--url X -- extract etc/hosts` runs `extract X etc/hosts`; with no `--` block at all, `--mode tree --url X` runs `list X`.

- `--full-help [MODE]`: Print the long-form reference — per mode, what it does, every flag that actually reaches it with its default and the reasoning, worked examples, and the exit codes. Give a mode name (or `farm`) to print just that section. Plain `--help` remains the short flag list.

- `--retries N`: Set how many times to retry if a download fails (default: 3).

- `--external`: Use remote `ip:port` SOCKS5 proxies fetched from a public list instead of local Docker Tor instances (see **External Proxy List** below). Works with any `--mode`.

- `--test-url URL`: URL used **only** to verify external-proxy liveness (overrides the `PROXY_TEST_URL` config constant). Point it at an endpoint you control or trust so that real target URLs are never revealed to proxies that end up discarded. If unset, a random one of your own targets — from `--url` or `URLs.txt` — is used and a warning is logged. Only relevant with `--external`.

## Examples

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
