# OnionAccelerator: implementation roadmap for Claude Code

Nov 12, 2026

## How to run this plan

Run one phase per Claude Code session, on its own branch, merged by PR before the next phase starts. Export this doc as Markdown and commit it as `docs/ROADMAP.md`, with the `CLAUDE.md` from the end of this doc at the repo root.

| Phase | Outcome | Depends on | Size |
| --- | --- | --- | --- |
| 0 | Installable package, CI, recorded test baseline | none | S |
| 1 | Resumable, piece-based download engine | 0 | L |
| 2 | Rootless Tor farm with control-port health | 0 | M |
| 3 | Persistent index, path scoring, selective fetch, export | 1 | L |
| 4 | Parser fuzzing, extract limits, proxy cross-checks | 0 (1 for cross-checks) | M |
| 5 | Benchmarks, docs, tagged release | 1, 2 | S |
| Spike | Rust core on `arti-client`, timeboxed | 1, 2 | S |

Phases 2 and 4 can run in parallel with Phase 1 in separate worktrees, since they touch different files.

Rules for every session:

- Start in plan mode. Read the phase, read the files it names, post a plan, and wait for approval before editing.
- Both suites are green before and after: `python3 -m pytest` against the tests directory from the repo root, and again inside `remote_viewer/`.
- No test touches the network unless it carries the `network` marker.
- No live onion address, victim name or real hostname appears in code, fixtures, logs or commit messages.
- Behaviour changes land test-first, against the local servers in `tests/` and `remote_viewer/tests/`.
- A flag that changes updates `README.md` and `fullhelp.py` in the same commit.
- No new runtime dependency without asking.
- Stop and ask at every item marked **Decision**.

A phase is done when its acceptance checks pass and the PR description lists what changed, what was skipped, and why.

## Baseline

The crawler, circuit isolation and live stream already exist; the download modes, the farm and cross-run persistence are where the work is. This is read from [euphoria95/OnionAccelerator](https://github.com/euphoria95/OnionAccelerator) at commit `6d8a1d8` (2026-10-24), about 28,800 lines with 512 test functions.

| Area | State today | Gap |
| --- | --- | --- |
| Crawl (`crawler/`) | asyncio, credential-isolated circuits, classified backoff, 26 listing templates, JSONL output per run | No cross-run index or diff |
| Tree (`remote_viewer/rvtree/`) | Range-only listing and extract, hedged small ranges, split large ones, proof-of-work gates, spool fallback | `extract` returns the member as `bytes` in memory; `--max-fetch` bounds download cost, not output size |
| Stream (`eventstream/`) | NDJSON over loopback, replay, gap accounting, `contrib/streamhunt.py` keyword consumer | Hits are not persisted or scored |
| Download (`OnionAccelerator.py`, `partial_download_file`) | Threads over `requests`; at most one chunk per proxy; one failed chunk deletes every part; merge reads each chunk whole into memory | No resume, no validators, no hashes, no circuit isolation, a new random User-Agent per attempt |
| Proxy pools | Three: `ProxyPool` in the main script, `LanePool` in rvtree, the async pool in `crawler/proxypool.py` | Download modes use the weakest one |
| Farm (`--farm`) | `tor-instance-create` plus systemd, root, Debian or Ubuntu only; bootstrap read by tailing `notices.log` | No rootless path, no control port |
| Packaging | `remote_viewer/pyproject.toml` only; the main script imports siblings by editing `sys.path`; unpinned `requirements.txt` | No root package, no CI, no lint config |

Tuesday's suggestions for an autoindex crawler, SOCKS-auth circuit isolation and a first test harness are already implemented, so they are dropped from this plan.

## Phase 0: package, CI, baseline

Goal: `pip install -e .` gives one installable project with a console script and a CI run, with no behaviour change.

- [ ] Run both suites and record pass, skip and duration counts in the PR as the baseline every later phase is compared against.
- [ ] Add a root `pyproject.toml` for `onionaccelerator`, packaging `crawler`, `eventstream` and the main module, with `rvtree` as a dependency that stays installable on its own.
- [ ] Add a console script `onionaccelerator` that calls `main()`. Keep `python3 OnionAccelerator.py` working.
- [ ] Keep the `sys.path` fallbacks in `_import_rvtree`, `_import_crawler` and `_import_eventstream` for uninstalled checkouts, and prefer the installed packages.
- [ ] Move the download, size-probe and farm code out of the 2,339-line `OnionAccelerator.py` into modules. Phases 1 and 2 rewrite exactly these parts.
- [ ] Keep every name the tests patch reachable from `OnionAccelerator`, for example `make_proxies` in `tests/conftest.py`. A moved function that is patched on the old module silently stops being patched.
- [ ] Set one supported Python floor. The README says 3.7+, `remote_viewer/pyproject.toml` says 3.9+.
- [ ] Replace `requirements.txt` with dependency ranges in `pyproject.toml`, with the crawl dependencies as they are today.
- [ ] Add `ruff` with a config that accepts the current code, fixing only real errors.
- [ ] Add a GitHub Actions workflow: lint, then both suites on the supported Python versions. Cache the archive corpus that `remote_viewer/tests/make_fixtures.sh` generates.

Acceptance:

- `pip install -e . && onionaccelerator --full-help` prints the same reference as before.
- Test counts match the baseline, with zero new skips.
- CI is green on a PR from a fork, with no secrets needed.

Out of scope: renaming flags, changing defaults, touching `crawler/` or `rvtree/` internals.

## Phase 1: download engine

Goal: replace `partial_download_file` and `download_file` with one engine that survives interruption, steals work from slow circuits and proves what it wrote.

**Decision** before starting: build the engine on rvtree's threaded `LanePool` (recommended: it already has credential-isolated lanes, `Content-Range` validation and gate handling, and keeps `requests`), or on the async pool in `crawler/proxypool.py` (lets `--download` run during a crawl, but rewrites more).

Design:

- **Pieces, not chunks per proxy.** Split every sized file into fixed pieces, default 4 MiB, set by `--piece-size`. Lanes pull the next piece from a shared queue, so a fast lane does more work.
- **One output file.** Preallocate `<name>.oa-part` and write each piece at its offset. No per-chunk part files, no merge pass, and never more than one piece per lane in memory.
- **State beside the file.** `<name>.oa-state` holds URL, size, validators, piece size, and per piece: status, SHA-256, lane, attempts. Flush it after every completed piece.
- **Resume.** A rerun of the same command finds the state, re-verifies completed pieces by hash, and fetches only the rest. Add `--fresh` to discard state.
- **Validators.** Record `ETag` and `Last-Modified` from the size probe and send `If-Range` on every piece. A `200` to a ranged request, or a changed validator, marks the state stale and restarts the file once.
- **Slow lanes.** Track bytes per second per lane. A piece running far below the median for a sustained window is cancelled, requeued and its lane rotated to a new credential.
- **Endgame.** Race the last few pieces on two lanes and keep the first to finish.
- **Failure is partial.** A piece that exhausts its retries leaves the file resumable. Nothing is deleted.
- **One User-Agent per lane lifetime**, as `crawler/proxypool.py` already does. Remove the per-attempt `random_user_agent` from the download path.
- **Custody record.** On completion write `<name>.oa.json`: URL, size, validators, whole-file SHA-256, start and finish times, lanes used, job id. Phase 3 ingests it.
- **Stream events.** Publish `download.start`, `download.piece`, `download.done` and `download.fail`, with `data` identical to what is written to disk, matching the existing stream contract.

Tasks:

- [ ] Add fault injection to the local range server: stall mid-piece, drop the connection, change the `ETag` mid-transfer, answer `200` to a range, return an HTML error page with `200`.
- [ ] Write the tests below against it, failing, before the engine.
- [ ] Implement piece planning, the state file, the offset writer and the lane workers.
- [ ] Route `--mode partial`, `--mode multi` and crawl `--download` through the engine. A file with no discoverable size is one piece, fetched sequentially, as today.
- [ ] Keep the four-probe size ladder and `_check_response` unchanged and reused.
- [ ] Resolve endpoints the way tree and crawl do. Drop the fixed twenty-port fallback and the `MAX_WORKERS = 20` cap from the download modes.
- [ ] Update `README.md` and `fullhelp.py`.

Acceptance:

- Killing the process with `SIGKILL` mid-transfer and rerunning yields a file whose SHA-256 equals the source's.
- With one lane throttled to a tenth of the others, it completes under a tenth of the pieces.
- A mid-transfer `ETag` change never produces a mixed file.
- Peak resident memory on a 2 GiB local transfer stays below 200 MiB with eight lanes.
- `tests/test_download_integrity.py` and `tests/test_size_probe.py` pass unmodified, or each change is justified in the PR.

Out of scope: BitTorrent, clearnet file hosts, any change to crawl or tree request scheduling.

## Phase 2: rootless farm and control port

Goal: `--farm up` works as an ordinary user on any Linux or macOS host with a `tor` binary, and reports health from Tor itself instead of a log tail.

Design:

- **Two backends.** `--farm-backend systemd` is today's code, unchanged. `--farm-backend process` is new and becomes the default when the caller is not root.
- **Process backend.** Each instance is a plain `tor -f <torrc>` child with its own `DataDirectory` under the user's state directory, a pidfile, and the same `oaNN` naming and port mapping.
- **Control channel.** Each torrc adds a `ControlSocket` with cookie authentication. A small stdlib client speaks only what is needed: authenticate, `GETINFO status/bootstrap-phase`, `GETINFO circuit-status`, `SIGNAL NEWNYM`, `SIGNAL SHUTDOWN`.
- **Health from Tor.** `wait_bootstrap` and `--farm status` read bootstrap phase over the control socket. The `notices.log` tail stays as the fallback for the systemd backend.
- **Same safety rule.** The process backend only ever touches directories it created, matched by the existing `FARM_RE`, and refuses to act on anything else.
- **Container image.** A `Dockerfile` that runs the process backend replaces the Bash one-liner in the README.

Credential rotation already buys a fresh circuit in tree and crawl, so the control port is for bootstrap, health and telemetry. It is not a second rotation mechanism.

Tasks:

- [ ] Extract a `FarmBackend` interface from the existing functions: `up`, `status`, `down`, `destroy`.
- [ ] Implement the process backend and the control client.
- [ ] Test with a stub `tor` executable that binds the SOCKS port and answers the control commands, so the suite stays offline.
- [ ] Add one `network`-marked test that bootstraps two real instances.
- [ ] Add per-endpoint circuit counts to `stats.json` when a control socket is available.
- [ ] Check every torrc option against the current Tor manual before adding it. Add nothing speculative.

Acceptance:

- `onionaccelerator --farm up --count 4` succeeds as a non-root user and `--mode speedtest` discovers all four ports.
- `--farm destroy` removes only the farm's own state directory.
- The systemd backend's behaviour and tests are unchanged.

Out of scope: Windows, relay or bridge configuration, managing Tor instances the farm did not create.

## Phase 3: index, scoring, selective fetch

Goal: every crawl and tree listing lands in one queryable store, so the question changes from "what is on this site" to "what is new and what touches us".

Design:

- **Store.** A new `index/` package over SQLite in WAL mode, at `--index PATH` with a default in the user's data directory, outside the repo, created with mode `0600`.
- **Schema.** `runs`, `targets` (host, operator-supplied label, group, victim), `entries` (target, path, name, size, mtime text, kind, source, first-seen run, last-seen run), `hits` (entry, rule, score, reason), `downloads` (the Phase 1 custody record). Full-text search over `path` with FTS5.
- **Ingest after the fact.** `--mode index ingest <dir>` reads `listing.jsonl`, `dirs.jsonl` and rvtree's `-f ndjson` output.
- **Ingest live.** The same ingester attaches to `--stream` as a consumer. The stream's contract that `data` equals the on-disk record means both paths build the same index.
- **Diff.** `--mode index diff --target T [--since RUN]` reports added, removed and resized entries between two sightings of a target.
- **Scoring.** A `scope.toml` holds the operator's own terms: organisation names, domains, suppliers, regexes. Built-in rule packs score by category: credential and key material, mail stores, directory-service dumps, VPN and remote-access configs, database dumps, HR, finance and legal paths.
- **Explainable hits.** Every hit stores the rule that fired and the matched span.
- **Selective fetch.** `--mode index fetch --min-score N` hands hit URLs to the Phase 1 engine. A hit inside an archive goes to rvtree `extract` and respects `--max-fetch`.
- **Export.** JSONL and CSV first.

**Decision**: STIX 2.1 export. Claude Code proposes an object mapping for a leak-site sighting in the PR and waits for approval before implementing it, behind an optional dependency.

Tasks:

- [ ] Schema, migrations table and ingester, tested against the fixtures in `tests/fixtures/listings/`.
- [ ] Idempotent ingest: the same run ingested twice changes nothing.
- [ ] Diff, with a test that covers a renamed directory and a re-uploaded file of a different size.
- [ ] Rule engine with rule packs as data files, following the pattern of `crawler/listing/templates/`.
- [ ] Live consumer with reconnect from the last sequence number and `stream.gap` reconciliation against the JSONL, as `contrib/streamhunt.py` does.
- [ ] `fetch`, wired to the engine and to rvtree.
- [ ] Keep `contrib/streamhunt.py` as the minimal example and point its docstring at the index.

Acceptance:

- An index built live and one built from the same run's artefacts are row-for-row equal.
- Ingesting 1,000,000 synthetic entries completes on a laptop and a path search returns in under a second.
- No test fixture or rule pack contains a real organisation or onion address.

Out of scope: fetching leak-site feeds in core. A feed-to-seeds script can live in `contrib/` later, after checking each feed's terms.

## Phase 4: hardening

Goal: a hostile server can waste the operator's time but cannot crash a run, exhaust memory, write outside the output directory or alter a download unnoticed.

Tasks:

- [ ] **Fuzz the archive parsers.** Property tests with Hypothesis for `formats/rar.py`, `sevenzip.py`, `xz.py`, `tarwalk.py`, `zipfmt.py`, `detect.py` and `segment.py`, over a fake transport that serves mutated bytes.
- [ ] Fuzz invariants: only `ArchiveError` or `TransportError` escapes; no range is requested past the entity size; requests per parse are capped; time and memory are bounded.
- [ ] **Bound the parse budget.** Add a cap on range requests and bytes per listing, since offsets and counts in headers are attacker-controlled. Exceeding it is a clean error that names the cap.
- [ ] **Stream `extract` to disk.** `archive.extract` returns `bytes` today. Write to the output file as data is decoded, and add `--max-output` for decompressed size beside the existing `--max-fetch`.
- [ ] `extract --output` refuses to overwrite an existing file without `--force`.
- [ ] **Audit the XML path.** `crawler/listing/page.py` parses XML through BeautifulSoup. Confirm entity expansion and external entities are off for WebDAV, S3 and sitemap bodies, and add a test with an entity-expansion payload.
- [ ] **Cap page bodies** before parsing in crawl, if not already capped, with a test.
- [ ] **Cross-check under `--external`.** An external SOCKS proxy sees and can alter plain-HTTP onion traffic. Re-fetch a random sample of pieces through a second proxy and compare hashes; a mismatch fails the file and parks both proxies.
- [ ] **One User-Agent policy.** A single module owns the choice for all modes. Decide between a pinned Tor Browser string, as rvtree's `DEFAULT_UA` uses, and the weighted `UserAgents.tsv`.
- [ ] **Leak guard.** A test or pre-commit check fails if a tracked file contains a v3 onion address outside an allowlist of synthetic ones.
- [ ] Output directories for downloads, crawls and the index default to mode `0700`.

Acceptance:

- The fuzz suite runs for a fixed budget in CI with no uncaught exception.
- A crafted archive that declares an enormous member cannot push `extract` past `--max-output`.
- A tampering stub proxy is detected by the cross-check test.

Out of scope: sandboxing or scanning downloaded content. That belongs to the analysis host, not this tool.

## Phase 5: benchmarks, docs, release

Goal: a tagged release whose performance claims anyone can reproduce offline.

- [ ] Add `bench/`: a harness that serves a synthetic file through local SOCKS stubs with configurable per-lane latency, throughput and failure rate.
- [ ] Measure throughput against lane count, time to first listing entry, bytes wasted on hedging, and resume overhead after a kill.
- [ ] Compare the Phase 1 engine with the pre-Phase-1 tag and with `aria2c` over the same stubs.
- [ ] Add one `network`-marked run against the operator's own test onion. Publish its numbers with date, Tor version and lane count.
- [ ] Move the 847-line `README.md` to a short quickstart plus one page per mode under `docs/`.
- [ ] Add `CHANGELOG.md`, tag the release, and publish to PyPI from GitHub Actions with trusted publishing.

Acceptance:

- `python -m bench` reproduces every table in the docs on a clean checkout with no network.
- `pipx install onionaccelerator` gives a working `onionaccelerator --full-help`.

**Decision**: version number and whether `rvtree` is published as its own PyPI package.

## Optional spike: Rust core on arti

Goal: answer in two days of work whether an in-process Tor client can replace the farm.

- [ ] Confirm from the current Arti documentation whether onion-service client support and vanguards are stable and on by default. Do not rely on memory for this.
- [ ] Build a throwaway binary that opens N isolated `arti-client` instances and runs the Phase 1 piece scheduler against the test onion.
- [ ] Compare throughput, memory and bootstrap time with an equal-sized process-backend farm from Phase 2.

Exit with a one-page write-up and a recommendation. Nothing from the spike merges into `main`.

## CLAUDE.md

Commit this at the repo root so every session starts with the project's invariants. Root `CLAUDE.md` is not ignored; `.claude/` is already in `.gitignore`.

```text
# OnionAccelerator

Python tool for discovering and downloading files over pools of Tor SOCKS5
circuits. Used for defensive threat-intelligence work on leak sites.
The phased plan is docs/ROADMAP.md. Work on one phase at a time.

## Layout
- OnionAccelerator.py        CLI, download modes, speedtest, farm, glue for tree/crawl/stream
- remote_viewer/rvtree/      remote archive list/extract over HTTP Range (own package, own tests)
- crawler/                   asyncio open-directory crawler
- crawler/listing/           listing parsers and TOML templates
- eventstream/               live NDJSON stream (stdlib only)
- fullhelp.py                per-mode reference (stdlib only)
- contrib/streamhunt.py      example stream consumer

## Commands
- python3 -m pytest tests
- cd remote_viewer && python3 -m pytest     # first run builds fixtures, needs 7z and xz
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

## Style
- Comments explain why, in the existing long-form style. Keep them when moving code.
- No new runtime dependency without asking.
- Names that tests monkeypatch must stay patchable where the tests patch them.

## Workflow
- Plan first and wait for approval. Write the failing test first.
- One phase per branch: phase-N-<slug>. Small commits.
- Stop and ask at every **Decision** marker in the roadmap.
- Finish with both suites green and a PR description: changed, skipped, why.
```

## Kickoff prompts

Paste one of these to open a session. The first is the template; the rest add what is specific to a phase.

```text
Read CLAUDE.md and docs/ROADMAP.md. We are doing Phase N only.

1. Run both test suites and report the counts.
2. Read every file the phase names, plus its tests.
3. In plan mode, give me: the files you will add or change, the tests you will
   write first, anything in the phase you think is wrong or underspecified,
   and your answer to each Decision with reasoning.
4. Wait for my approval. Then work test-first in small commits on branch
   phase-N-<slug>.
5. Finish by running the phase's acceptance checks and writing the PR
   description: changed, skipped, why.

Do not start work from any other phase, even if it looks adjacent.
```

Phase 0:

```text
Phase 0. This is a no-behaviour-change refactor. Before moving any function out
of OnionAccelerator.py, grep tests/ for monkeypatch and patch targets and list
the names that must stay reachable on the old module. Show me that list in the plan.
```

Phase 1:

```text
Phase 1. Start by reading partial_download_file, download_file, ProxyPool and
the size-probe ladder in the main module, then LanePool in
remote_viewer/rvtree/transport/pooled.py and crawler/proxypool.py. Your plan
must recommend which pool the engine builds on and say what the other choice
would have cost. Build the fault-injecting range server and the failing tests
before any engine code.
```

Phase 2:

```text
Phase 2. Do not run tor, systemctl or tor-instance-create in this session.
Build the stub tor executable first and drive everything through it. Fetch the
current Tor manual and cite the section for every torrc option you add.
```

Phase 3:

```text
Phase 3. Design the SQLite schema first and show it to me with three example
queries: new entries since the last run of a target, top hits by score, and
all sightings of a path pattern across targets. All rule packs and fixtures use
synthetic names only.
```

Phase 4:

```text
Phase 4. Treat every byte from the network as hostile. For each parser, tell me
in the plan which header fields control a size, count or offset, and how each is
bounded today. Report anything exploitable you find before fixing it.
```

Phase 5:

```text
Phase 5. Benchmarks must run fully offline by default. Every number that goes
into the docs comes from a command I can rerun; no estimated figures.
```
