# OnionAccelerator

Discover and download files through a pool of SOCKS5 proxies, normally Tor: a download engine
that resumes and proves what it wrote, an open-directory crawler, a lister for huge remote
archives that never downloads them, a persistent index of everything found, and a farm of Tor
instances to run it all on.

Built for defensive work on services you are authorised to examine. Routing through Tor hides
where you are, not what you do, and does not make an action lawful.

## Install

```bash
pipx install onionaccelerator          # or: pip install onionaccelerator
onionaccelerator --full-help           # the whole reference, per mode
```

Until the first release is on PyPI, install from a checkout instead:
`python3 -m venv .venv && . .venv/bin/activate && pip install -e .`. Python 3.11 or newer.
`onionaccelerator ARGS` and `python3 OnionAccelerator.py ARGS` are the same program.

You also need SOCKS5 proxies to send traffic through. The easiest is the built-in farm, which
needs no root and only a `tor` binary (tested on Linux; macOS is supported in the code and untested):

```bash
onionaccelerator --farm up --count 8          # eight client-only Tor instances on 127.0.0.1:5000-5007
onionaccelerator --farm status
```

## Five minutes

```bash
# Map an open directory, as it runs, into a searchable index:
onionaccelerator --mode crawl --url http://EXAMPLE.onion/dumps/ --stream &
onionaccelerator --mode index live --stream-url http://127.0.0.1:8787

# What is new since last time, and what touches us:
onionaccelerator --mode index diff --target EXAMPLE.onion
onionaccelerator --mode index hits --min-score 60 --explain

# Download one big file: resumable, verified, over every circuit. Kill it and run it again.
onionaccelerator --mode partial --url http://EXAMPLE.onion/backup.iso

# Read the file tree of a 100 GB archive for a few kilobytes:
onionaccelerator --mode tree --url http://EXAMPLE.onion/backup.7z
```

## Modes

| Mode | What it does | Page |
|---|---|---|
| `multi` | many files at once over shared lanes, in resumable pieces | [multi](https://github.com/euphoria95/OnionAccelerator/blob/main/docs/modes/multi.md) |
| `partial` | one file at a time, every lane on it | [partial](https://github.com/euphoria95/OnionAccelerator/blob/main/docs/modes/partial.md) |
| `tree` | list or extract members of a remote archive with HTTP Range requests | [tree](https://github.com/euphoria95/OnionAccelerator/blob/main/docs/modes/tree.md) |
| `crawl` | walk an open directory across many circuits, write a manifest | [crawl](https://github.com/euphoria95/OnionAccelerator/blob/main/docs/modes/crawl.md) |
| `index` | keep every listing: scored, searchable, diffable; fetch the hits | [index](https://github.com/euphoria95/OnionAccelerator/blob/main/docs/modes/index.md) |
| `speedtest` | grade every proxy and check the targets answer | [speedtest](https://github.com/euphoria95/OnionAccelerator/blob/main/docs/modes/speedtest.md) |
| `--farm` | run, grade and tear down a farm of Tor instances | [farm](https://github.com/euphoria95/OnionAccelerator/blob/main/docs/farm.md) |

Also: the [download engine](https://github.com/euphoria95/OnionAccelerator/blob/main/docs/download-engine.md)
behind `multi`, `partial` and `crawl --download`; the
[live streaming API](https://github.com/euphoria95/OnionAccelerator/blob/main/docs/streaming.md)
(`--stream`); [`--external`](https://github.com/euphoria95/OnionAccelerator/blob/main/docs/external.md)
proxies; [usage and examples](https://github.com/euphoria95/OnionAccelerator/blob/main/docs/usage.md);
the [project structure](https://github.com/euphoria95/OnionAccelerator/blob/main/docs/project-structure.md);
and the [benchmarks](https://github.com/euphoria95/OnionAccelerator/blob/main/docs/benchmarks.md),
every table of which `python -m bench` regenerates offline.

## Requirements

- Python 3.11+, for every mode.
- `requests`, `PySocks` and `tqdm`. `--mode crawl` and `--detect` also use `aiohttp`,
  `aiohttp-socks`, `beautifulsoup4` and `lxml`; `pip install` brings them, and they are imported
  lazily, so the other modes still run where they fail to import.
- A `tor` binary for `--farm`, or proxies of your own (`--socks host:port,...`).
- Optional: `7z` and `unrar`, to read compressed RAR members with `--mode tree`.

The version floors are in `pyproject.toml`, and CI tests them as written on the oldest
supported Python.

## Contributing

Pull requests and suggestions are welcome. Open an issue for problems or for questions about
advanced Tor configurations. `CHANGELOG.md` lists what changed in each release, and
`docs/RELEASING.md` how a release is made.

## License

MIT. See `LICENSE`.
