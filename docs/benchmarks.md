# Benchmarks

Everything on this page comes from `python -m bench`, runs offline, and can be regenerated
and checked on a clean checkout with no network:

```bash
python -m bench                    # run every suite, print the tables
python -m bench --check            # re-run and compare with this page; exit 1 if it differs
python -m bench --write-docs       # regenerate this page
python -m bench --suite resume     # one suite
python -m bench --quick            # a fast smoke run; its numbers are not published
```

## What is being measured, and what is not

The software is measured, not Tor. A content server and a set of SOCKS5 stubs run in a
process of their own on loopback (`bench/lab.py`), and each stub shapes traffic the way a
circuit does: a throughput cap per connection, optionally a cap shared by the whole daemon,
round-trip latency with a long tail, a setup delay, and connections that die part-way
through a transfer. Every system under test runs in its own subprocess against the same
stubs, and **a run only counts if the file it wrote has the right SHA-256**: a fast wrong
answer is not a result.

So the tables say how the program behaves when the network has these properties: how it
scales with lanes, what it does about a slow lane or a dying one, what a kill costs, what
hedging costs. They do not say how fast Tor is, and a real onion will not match them. The
shapes (a lane at a tenth of the speed costs the old code ten times what it costs the new)
are the transferable part; the absolute seconds are not.

The lab itself is checked against a stopwatch (`tests/test_bench_lab.py`), and relays well
over 400 MiB/s unshaped, several times the most any table here asks of it.

## The systems

* **engine**: this checkout's `partial_download_mode`: fixed pieces pulled from a shared
  queue by whichever lane is free, written at their offsets, journalled, resumable.
* **legacy**: the same function at an earlier commit (the one in the environment table),
  which gave each proxy one chunk and merged the chunks afterwards. It is extracted with
  `git archive` into a scratch directory, so the two never share a module. If the commit is
  not in your clone its column reads n/a.
* **aria2c**: only where it is installed. aria2c cannot use a SOCKS5 proxy, so each lane is
  also exposed as an HTTP mirror with the same shaping and aria2c is given every mirror for
  the one file: the same capacity, reached the way aria2c can reach it. Its column reads n/a
  where it is not installed, and an n/a is the honest value: nothing was run.

## Environment

| | |
|---|---|
| date (UTC) | 2026-11-16 |
| commit | 26790fa |
| python | 3.14.4 |
| platform | Linux x86_64 |
| cpus | 8 |
| requests | 2.34.2 |
| PySocks | 1.7.1 |
| legacy baseline | 6d8a1d8 |
| aria2c | not installed |

## Results

### Throughput against lane count

| lanes | engine s | engine % of ideal | legacy s | legacy % of ideal | aria2c s | aria2c % of ideal |
|---|---|---|---|---|---|---|
| 1 | 18.6 | 86% | 17.3 | 92% | n/a | n/a |
| 2 | 9.4 | 85% | 9.0 | 89% | n/a | n/a |
| 4 | 4.9 | 81% | 4.8 | 83% | n/a | n/a |
| 8 | 2.7 | 74% | 2.7 | 73% | n/a | n/a |
| 16 | 1.6 | 61% | 1.7 | 59% | n/a | n/a |

*A 128 MiB file, each lane capped at 8 MiB/s, so the ideal time is 128 / (lanes × 8) seconds. Median of 3.*

*At 16 lanes both systems fall away from ideal together, so the limit is more likely the lab or the single Python interpreter driving the lanes than either design; aria2c, where installed, is the control.*

*aria2c: n/a, it is not installed on the machine that produced this page.*

### Bytes served on a clean network

| lanes | engine | legacy | aria2c |
|---|---|---|---|
| 1 | 1.00x | 1.00x | n/a |
| 2 | 1.00x | 1.00x | n/a |
| 4 | 1.00x | 1.00x | n/a |
| 8 | 1.00x | 1.00x | n/a |
| 16 | 1.00x | 1.00x | n/a |

*What the content server sent, divided by the file size, on a clean network where every lane is identical. 1.00x is no duplicate bytes; anything above it is bandwidth spent twice. On Tor that is the number to watch.*

### One lane at a tenth of the speed

| system | all lanes healthy s | one slow lane s | slowdown |
|---|---|---|---|
| engine | 2.7 | 3.7 | 1.4x |
| legacy | 2.7 | 20.6 | 7.6x |

*8 lanes at 8 MiB/s, then the same with lane 0 at one tenth of that. The slowdown is the second time over the first; lower is better. Median of 3.*

### Connections that die mid-transfer

| system | finished | mean s | mean bytes served |
|---|---|---|---|
| engine | 15/15 | 3.3 | 1.16x |
| legacy | 13/15 | 7.7 | 2.27x |

*8 lanes at 8 MiB/s; every connection is reset after an average of 24 MiB, so a piece or a chunk is regularly cut off mid-transfer. 'Bytes served' is what the content server sent divided by the file size: 1.00x is no waste. These are **means** over 15 runs, not medians: a download cut into per-proxy chunks has a two-humped outcome here (either no chunk runs out of retries, or the whole file starts again), and a median flips between the humps from one run of the benchmark to the next. How many resets each run met is the lab's dice, not a result, and is not tabulated.*

### Killed halfway, then run again

| system | killed at | bytes served, both runs | second run s | uninterrupted s | second run vs uninterrupted |
|---|---|---|---|---|---|
| engine | 50% | 1.25x | 2.1 | 2.7 | 0.77x |
| legacy | 76% | 1.78x | 2.7 | 2.7 | 1.00x |

*8 lanes at 8 MiB/s, 128 MiB. The first run is killed with SIGKILL halfway through the time an uninterrupted run takes; the same command is then run again. 'Killed at' is how much of the file the server had sent by then. 'Bytes served' counts both runs over the file size: 1.00x would be a resume that re-fetches nothing, and 1.00x plus the killed-at share is a restart from scratch. A resume cannot be perfect: whatever was in flight when the process died (up to lanes x piece size) was never written down. Median of 3.*

### Time to the first listing entry, and what hedging costs

|  | first entry s (median) | first entry s (p90) | first 25 entries s | requests | KiB served |
|---|---|---|---|---|---|
| hedging off | 1.51 | 1.93 | 9.35 | 22 | 448 |
| hedging on | 0.87 | 1.03 | 6.01 | 48 | 849 |

*A tar of 50,000 members, 4 stubs with a mean round trip of 0.15 s and a long tail (mean extra 0.225 s). Hedging sends a small read to several daemons and keeps the first answer. Cost of it here: 89% more bytes and 118% more requests for the first 25 entries. Median of 12 fresh runs.*

## What is not here

* **A run through Tor.** `bench/onion.py` runs the same transfer against an onion service you
  operate, through a farm you started, and prints the table with its date, Tor version and
  lane count. It needs both, so it is not part of `python -m bench` and nothing from it is on
  this page. Numbers from one would be a statement about that onion and that day.
* **Contention.** The lab, the engine and the benchmark share a machine. The lab's capacity
  is far above what any table asks of it, but a busy machine will still move the seconds.
* **Statistics.** These are medians of a handful of runs, enough to show a 10x effect and not
  enough to resolve a 10% one. `--check` allows 35% by default for that reason.
