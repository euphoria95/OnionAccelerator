# OnionAccelerator

OnionAccelerator is a multi-functional Python script designed for downloading files through multiple SOCKS5 proxies (commonly Tor instances). It supports three main modes:

## Modes

### Multi-Download Mode

- Simultaneously download multiple files listed in `URLs.txt`, with worker-thread concurrency bounded by the proxy count (capped at the number of URLs, so no idle threads are spawned).
- Automatically retries failed downloads a configurable number of times, drawing a fresh proxy from the pool on each retry so a dead endpoint doesn't doom a URL.
- Aggregates download progress via a single progress bar in the terminal.

### Partial-Download Mode

- Splits each file into parallel byte-range chunks — up to one per proxy, but never more chunks than the file size warrants (chunks are at least `MIN_PARTIAL_CHUNK_SIZE`, 1 MB by default) — each served by a SOCKS5 proxy drawn from the shared pool.
- If the server does not provide a `Content-Length` header, the script automatically falls back to a single, sequential download.
- Merges the downloaded chunks into a final file upon success, and performs retry logic if any chunk fails — each chunk retry lands on a different live proxy via the pool's failover.

### Remote Archive Tree Mode (`--mode tree`)

- Lists or extracts members of a **huge remote archive** (`.rar`, `.tar`, `.zip`, `.7z`,
  `.tar.xz`) using HTTP Range requests, **without downloading it**. A 100 GB archive costs
  kilobytes to read, because every supported format keeps its metadata somewhere a handful
  of range requests can reach.
- Backed by [`remote_viewer/rvtree`](remote_viewer/), driven over the same `host:port`
  SOCKS5 pool the download modes use — so a `--farm` of independent Tor daemons, or an
  `--external` proxy list, gives it real parallelism.
- Everything after `--` is passed straight to rvtree, so the full `list` / `extract` /
  `probe` surface is available with no flags to keep in sync.

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

- In partial-download mode, if no file size is advertised by the server (`Content-Length`), the script automatically switches to a single GET request and proceeds with a standard download.

## Installation

1. **Install Python 3** (3.7+ recommended).

2. **Install dependencies:**

    ```bash
    pip install -r requirements.txt
    ```

3. **Ensure you have multiple SOCKS5 proxies** (e.g., Tor instances on ports `5000..5019`). The easiest way is the built-in native farm — `python3 OnionAccelerator.py --farm up` (see **Native Tor Farm** below) — or bring your own proxies / Docker.

## Usage

### Prepare `URLs.txt`

- Put the URLs you want to download (one per line) into a file named `URLs.txt`.

### Run the script:

```bash
# Provision / manage the native Tor proxy farm:
python3 OnionAccelerator.py --farm <up|status|down|destroy> [--count N] [--base-port PORT] [--bootstrap-timeout SEC]

# Download / speedtest through the proxies:
python3 OnionAccelerator.py --mode <multi|partial|speedtest> [--retries N] [--external] [--test-url URL]

# Read a remote archive's file tree without downloading it:
python3 OnionAccelerator.py --mode tree [--external] -- <rvtree arguments>
```

Exactly one of `--farm` or `--mode` must be given.

- `--farm`: manage a farm of native Tor instances instead of Docker (see **Native Tor Farm** below).

- `--mode`:
  - `multi`: Parallel download of all URLs in `URLs.txt`, each in a separate worker thread with its own SOCKS5 proxy.
  - `partial`: Parallel chunk-based download for each URL, automatically merging chunks.
  - `speedtest`: Test download speed and basic health for each SOCKS5 proxy using the first URL from `URLs.txt`.
  - `tree`: List or extract members of a remote archive over the proxy pool without downloading it. Takes its target from the arguments after `--`, not from `URLs.txt` (see **Remote Archive Tree** below).
  
- `--retries N`: Set how many times to retry if a download fails (default: 3).

- `--external`: Use remote `ip:port` SOCKS5 proxies fetched from a public list instead of local Docker Tor instances (see **External Proxy List** below). Works with any `--mode`.

- `--test-url URL`: URL used **only** to verify external-proxy liveness (overrides the `PROXY_TEST_URL` config constant). Point it at an endpoint you control or trust so that real target URLs are never revealed to proxies that end up discarded. If unset, a random URL from `URLs.txt` is used and a warning is logged. Only relevant with `--external`.

### Examples

```bash
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
```

## Remote Archive Tree (`--mode tree`)

Reading a 2.4 GB `.rar` on an onion service to find out what is in it means transferring
2.4 GB — unless you only fetch the headers. That is what `rvtree` does, and `--mode tree`
runs it over OnionAccelerator's proxy fleet.

```bash
# What is in this archive, and what did it cost to find out?
python3 OnionAccelerator.py --mode tree -- list https://example.onion/backup.rar

# Server capabilities and archive shape first — about 65 KiB.
python3 OnionAccelerator.py --mode tree -- probe https://example.onion/backup.tar.xz

# Pull one member out of it.
python3 OnionAccelerator.py --mode tree -- extract https://example.onion/backup.rar etc/hosts -o hosts

# Stream a huge tree as it is discovered, so an interrupted run keeps what it had.
python3 OnionAccelerator.py --mode tree -- list https://example.onion/big.tar.xz -f ndjson > tree.ndjson

# Through the external proxy list instead of a local farm.
python3 OnionAccelerator.py --mode tree --external --test-url http://your-own/ping -- list https://example.onion/a.7z
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

## Project Structure

- `OnionAccelerator.py`: The main script containing all modes (multi-download, partial-download, speedtest, tree).
- `remote_viewer/`: The `rvtree` package behind `--mode tree`. Usable on its own too — see its own README.
- `requirements.txt`: Python dependencies.
- `URLs.txt`: A text file with one URL per line.
- `UserAgents.tsv`: Tab-separated file; first column is the User-Agent string.
- `logs/`: A directory automatically created to store timestamped log files.
- `downloads/<host>/`: Output directory for multi mode, organised by hostname.
- `partials/<host>/`: Output directory for partial mode; temporary chunk files are merged here.

## Requirements

- Python 3.7+
- `requests[socks]` or `PySocks` for SOCKS5 support
- `tqdm` for progress bars

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
- **Connectivity-checked before any work starts.** Each candidate proxy is tested in parallel against a single connectivity-check URL; only proxies that successfully return data are used. The run aborts if none pass. By default that check URL is a random entry from `URLs.txt` — set `--test-url` (or the `PROXY_TEST_URL` config constant) to a private endpoint you control so real targets are never exposed to discarded proxies.
- **Pooled with failover.** Proxies are drawn from a shared, thread-safe pool rather than pinned to a worker or chunk by index. When a download attempt fails, its proxy is parked and the retry lands on a *different* live proxy, so a single dead endpoint no longer sinks a whole file or URL. A proxy that later succeeds is rehabilitated, and if every proxy is parked the pool falls back to reusing them.

## Contributing

Pull requests and suggestions for improvements are welcome. Feel free to open an issue if you encounter any problems or have questions regarding advanced Tor configurations.
