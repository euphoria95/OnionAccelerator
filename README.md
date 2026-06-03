# OnionAccelerator

OnionAccelerator is a multi-functional Python script designed for downloading files through multiple SOCKS5 proxies (commonly Tor instances). It supports three main modes:

## Modes

### Multi-Download Mode

- Simultaneously download multiple files listed in `URLs.txt`, one worker thread per proxy (capped at the number of URLs, so no idle threads are spawned).
- Automatically retries failed downloads a configurable number of times.
- Aggregates download progress via a single progress bar in the terminal.

### Partial-Download Mode

- Splits each file into parallel byte-range chunks — one per proxy, but never more chunks than the file size warrants (chunks are at least `MIN_PARTIAL_CHUNK_SIZE`, 1 MB by default) — each assigned to a different SOCKS5 proxy.
- If the server does not provide a `Content-Length` header, the script automatically falls back to a single, sequential download.
- Merges the downloaded chunks into a final file upon success, and performs retry logic if any chunk fails.

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

3. **Ensure you have multiple SOCKS5 proxies** (e.g., Tor instances on ports `5000..5019`), or adapt the code to your setup.

## Usage

### Prepare `URLs.txt`

- Put the URLs you want to download (one per line) into a file named `URLs.txt`.

### Run the script:

```bash
python3 OnionAccelerator.py --mode <multi|partial|speedtest> [--retries N] [--external]
```

- `--mode`:
  - `multi`: Parallel download of all URLs in `URLs.txt`, each in a separate worker thread with its own SOCKS5 proxy.
  - `partial`: Parallel chunk-based download for each URL, automatically merging chunks.
  - `speedtest`: Test download speed and basic health for each SOCKS5 proxy using the first URL from `URLs.txt`.
  
- `--retries N`: Set how many times to retry if a download fails (default: 3).

- `--external`: Use remote `ip:port` SOCKS5 proxies fetched from a public list instead of local Docker Tor instances (see **External Proxy List** below). Works with any `--mode`.

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
```

## Project Structure

- `OnionAccelerator.py`: The main script containing all modes (multi-download, partial-download, speedtest).
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

## Wrap Multiple Tor proxies with docker with Bash one-liner

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
- **Connectivity-checked before any work starts.** Each candidate proxy is tested in parallel by fetching a random URL from `URLs.txt`; only proxies that successfully return data are used. The run aborts if none pass.

## Contributing

Pull requests and suggestions for improvements are welcome. Feel free to open an issue if you encounter any problems or have questions regarding advanced Tor configurations.
