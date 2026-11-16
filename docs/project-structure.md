# Project structure

- `Dockerfile`: the farm as a container (`--farm run`, process backend).
- `OnionAccelerator.py`: The command line, speedtest, proxy discovery and the glue for tree, crawl and stream. Installed as the `onionaccelerator` command.
- `oacore/`: The download engine (`engine.py`) and the modes built on it (`download.py`), the remote size probe (`sizeprobe.py`), the native farm (`farm.py`: the backend interface and the systemd backend; `farm_process.py`: the rootless backend; `torcontrol.py`: its control-port client) and the settings and helpers they share. `OnionAccelerator.py` re-exports all of it.
- `remote_viewer/`: The `rvtree` package behind `--mode tree`. Usable on its own too — see its own README.
- `crawler/`: The asyncio package behind `--mode crawl` — multi-circuit proxy pool, frontier, fetcher and reporting. The only async code in the project; the other modes stay on threads over `requests`.
- `crawler/listing/`: The scraping layer, with no crawl in it: the structural reader, the JSON/XML/row/manifest strategies, the addressing rules, and the template engine behind `--profile` and `--detect`. Adding support for a target is a file in `crawler/listing/templates/`, not a change to the crawler — see that directory's [README](../crawler/listing/templates/README.md).
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
