# Changelog

All notable changes are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/) with one caveat: **before 1.0, a minor release
(0.N) may change a flag, a default or the index schema**, and says so below. The index
migrates its own schema forward and refuses a file written by a newer version.

## [Unreleased]

The first release. Everything below is new relative to the project as it stood before it had
a version.

### Added
- **Packaging.** `pip install` / `pipx install onionaccelerator` gives an `onionaccelerator`
  command; `python3 OnionAccelerator.py` still works from a checkout. One supported Python
  floor, 3.11, and dependency floors that CI installs exactly as written. The download modes,
  the size probe and the farm moved out of the script into `oacore/`; every name the tests
  patch is still reachable (and patchable) as `OnionAccelerator.<name>`.
- **A piece-based, resumable download engine** behind `--mode multi`, `--mode partial` and
  `crawl --download`. Files are cut into `--piece-size` pieces (4 MiB) pulled from a shared
  queue by whichever lane is free and written at their offsets; an append-only journal
  (`<name>.oa-state`) lets the same command resume after a kill, re-verifying finished pieces
  by SHA-256; `ETag`/`Last-Modified` go out as `If-Range` so a file that changes mid-transfer
  is never spliced from two versions; a late piece is raced on a second lane and a very slow
  lane is rotated onto a new circuit; a custody record (`<name>.oa.json`) is written for every
  completed file. New flags `--piece-size` and `--fresh`. `download.start`, `download.piece`,
  `download.done` and `download.fail` events on `--stream`.
- **A rootless Tor farm.** `--farm-backend process` (the default unless you are root) runs
  plain `tor -f` children under your own state directory, no systemd and no root, with health
  read from each instance's control socket instead of a log tail; `--farm run` supervises them
  in the foreground; the systemd backend is unchanged and selectable. Per-endpoint circuit
  counts go into a crawl's `stats.json`. A `Dockerfile` runs the farm as a container.
- **`--mode index`**: a persistent SQLite index of everything crawls and archive walks found,
  with rule packs (credentials, mail, directory-service dumps, remote access, databases, HR,
  finance, legal) and your own `scope.toml`, explainable hits, a diff between two sightings of
  a target that distinguishes *removed* from *not re-listed*, path search, JSONL and CSV
  export, live ingest from `--stream`, and `fetch --min-score` to hand the best hits to the
  download engine. `--index PATH`.
- **`python -m bench`**: offline benchmarks of the engine against the code it replaced
  (and `aria2c` where installed) over shaped SOCKS5 stubs, and `docs/benchmarks.md`, every
  table of which `python -m bench --check` regenerates and compares.
- Documentation split into one page per mode under `docs/`; a short README.
- CI: lint, both test suites on Python 3.11-3.14, a job that installs every dependency at its
  declared floor, and a clean-wheel smoke test.

### Changed
- **Python 3.11 or newer** (it was documented as 3.7+ and 3.9+ in different places).
- **`requirements.txt` is gone**; dependencies are declared in `pyproject.toml`.
- **`--mode multi` and `--mode partial` exit 1 when a file was left incomplete** (they
  exited 0 and logged). The file stays resumable.
- **A failed piece no longer deletes anything**; rerun the same command to resume.
- **The download modes resolve their proxies as `tree` and `crawl` do** (`--socks`,
  `--external`, a live probe of the farm range, a personal Tor client on `127.0.0.1:9150`,
  then an error). The fixed twenty-port fallback and the 20-worker cap are gone from them.
- Each download lane keeps one User-Agent for its whole life (it used to draw a fresh one per
  attempt), and the size probe goes out on a lane's own circuit.
- `--stream` is accepted by `--mode multi` and `--mode partial`.
- `--farm` defaults to the process backend for a non-root user.
- `rvtree` is bundled in the `onionaccelerator` distribution; `remote_viewer/pyproject.toml`
  still builds it on its own.

### Removed
- `ProxyPool`, `partial_download_chunk_to_file`, `fallback_sequential_download` and
  `partial_download_file`: replaced by the engine. `download_file` remains as a one-URL entry
  point.

[Unreleased]: https://github.com/euphoria95/OnionAccelerator/commits/main
