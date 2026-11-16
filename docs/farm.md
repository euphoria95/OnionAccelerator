# The Tor farm (`--farm`)

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

## Tuning the farm for onion crawling (`--farm-tune`)

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

## Auto-discovery

In local (non-`--external`) mode, OnionAccelerator **probes** `127.0.0.1:BASE_PORT..`
and uses only the ports that are actually listening. A farm of any size (10, 15, 20…)
— or a set of Docker/manual proxies — just works without editing constants:

```
[INFO] Discovered 12 live local Tor SOCKS ports (5000-5011).
```

If nothing is listening, it warns and suggests `--farm up`.

## Full pipeline

```bash
sudo python3 OnionAccelerator.py --farm up --count 20   # bring proxies up
python3 OnionAccelerator.py --mode speedtest            # sanity-check them
python3 OnionAccelerator.py --mode multi                # download
sudo python3 OnionAccelerator.py --farm destroy         # tear down when done
```

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
