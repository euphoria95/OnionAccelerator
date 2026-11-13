# OnionAccelerator

Python tool for discovering and downloading files over pools of Tor SOCKS5
circuits. Used for defensive threat-intelligence work on leak sites.
The phased plan is docs/ROADMAP.md. Work on one phase at a time.

## Layout
- OnionAccelerator.py        CLI, speedtest, proxy discovery, glue for tree/crawl/stream;
                             re-exports oacore/ (console script `onionaccelerator`)
- oacore/                    download modes, size probe, farm, shared config and helpers
- remote_viewer/rvtree/      remote archive list/extract over HTTP Range (own package, own tests)
- crawler/                   asyncio open-directory crawler
- crawler/listing/           listing parsers and TOML templates
- eventstream/               live NDJSON stream (stdlib only)
- fullhelp.py                per-mode reference (stdlib only)
- contrib/streamhunt.py      example stream consumer

## Commands
- pip install -e '.[dev]'
- python3 -m pytest                          # root suite, tests/ only
- cd remote_viewer && python3 -m pytest     # first run builds fixtures, needs 7z and xz
- ruff check .
- Tests marked `network` are excluded by default. Never run them unless asked.

## Hard rules
- Never contact a real onion service, leak site or external proxy list.
  Use the local servers in tests/ and remote_viewer/tests/.
- Never write a real onion address, organisation name or hostname into code,
  fixtures, logs, commit messages or PR text. Use synthetic names.
- URLs.txt, *.jsonl, rv.log, crawls/, downloads/, partials/ are local evidence.
  Do not open them, quote them or commit them.
- Do not run --farm (system changes) or --external (third-party proxies).
- TLS verification is off by default on purpose: an onion address authenticates
  the peer. Do not change that default.

## Invariants
- eventstream/ and fullhelp.py import only the standard library.
- A stream event's `data` is byte-identical to the record written to disk.
- The farm touches only instances named oaNN.
- A hostile URL or archive member name can never write outside the output directory.
- A flag change updates README.md and fullhelp.py in the same commit.
  tests/test_cli.py checks the mirrored rvtree subcommands.
- A name tests patch as `OnionAccelerator.<name>` must be re-exported there and
  forwarded to its oacore module; tests/test_facade.py enforces it.

## Style
- Comments explain why, in the existing long-form style. Keep them when moving code.
- No new runtime dependency without asking.
- Names that tests monkeypatch must stay patchable where the tests patch them.

## Workflow
- Plan first and wait for approval. Write the failing test first.
- One phase per branch: phase-N-<slug>. Small commits.
- Stop and ask at every **Decision** marker in the roadmap.
- Finish with both suites green and a PR description: changed, skipped, why.
