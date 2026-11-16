# Live streaming API (`--stream`)

Any crawl, tree listing or detection can publish what it finds *while it runs*, as
newline-delimited JSON over HTTP on loopback, for an external script to grep or index —
so a keyword hit during a four-hour Tor crawl arrives at minute three instead of at the
end. See **Live Streaming API** below.

## Details

The runs this tool is for take hours. A crawl of a leak site's file manager over Tor, or
a listing of a 2 GB archive by range requests, writes everything it learns to disk — but
not until it stops. `--stream` publishes each finding as it happens, over HTTP, for
something else to read while the run is still going:

```bash
python3 OnionAccelerator.py --mode crawl --url http://target.onion/ --stream &

# every file whose name or path matches, as it is discovered
curl -sN 'http://127.0.0.1:8787/events?kinds=crawl.file&match=(?i)payroll'
```

That is the whole point: during an incident, a hit at minute three can steer the rest of
the collection, and a hit at hour four cannot. It works the same way for `--mode tree`,
where each member of a remote archive is published as it is walked out of the headers,
and for `--detect`.

## Endpoints

| Endpoint | What it answers |
|---|---|
| `GET /events` | one JSON object per line, forever, until the run ends |
| `GET /status` | the run's counters, the current cursor, and who is attached |
| `GET /schema` | every event kind and what it means — a consumer can be written against the running server rather than against this file |
| `GET /health` | liveness |
| `GET /` | a plain-text reminder of all of the above |

`/events` takes:

| Parameter | Meaning |
|---|---|
| `since=SEQ` | replay everything after this sequence number, then follow. This is how a consumer reconnects without a hole |
| `kinds=A,B` | only these kinds. A family prefix works: `kinds=crawl` takes every `crawl.*` |
| `match=REGEX` | server-side filter, applied to the whole line |
| `shape=ndjson\|line` | `line` emits only the URL or path — a list you can pipe into `wget -i` or `sort -u` |
| `heartbeat=S` | seconds between keepalives on an idle stream; `0` disables |

## The events

Every line carries the same envelope, so a consumer routes on `kind` without knowing
which producer it came from:

```json
{"seq":412,"ts":"2026-08-23T18:22:01Z","run":"20260823_182034","mode":"crawl",
 "kind":"crawl.file","data":{"url":"http://target.onion/hr/2019/payroll.xlsx",
 "name":"payroll.xlsx","size_bytes":48219,"depth":3,"parent":"http://target.onion/hr/2019/",
 "endpoint":"127.0.0.1:5001","discovered_at":"2026-08-23T18:22:01Z"}}
```

`data` is **byte-for-byte the record the run writes to disk** — a `crawl.file` event is a
line of `listing.jsonl`, a `tree.entry` is what `-f ndjson` prints. An index built from
the stream and one built from the artefacts afterwards cannot disagree, which is the only
reason a live stream is safe to base a conclusion on.

| Kind | Carried |
|---|---|
| `run.start` / `run.stop` | the target and configuration; then why it stopped, and the totals |
| `crawl.dir` / `crawl.file` | a listed directory / a discovered file — the hunting events |
| `crawl.skip` | a page that was fetched and read but refused by the index-confidence guard |
| `crawl.fail` | one failed attempt, `final` marking the one that exhausted `--retries` |
| `crawl.progress` | periodic counters, so a consumer can tell "finding nothing" from "stuck" |
| `crawl.page` | fetched page text, truncated — only with `--stream-bodies`, never written to disk |
| `tree.open` / `tree.entry` / `tree.done` | the archive, each member as it is walked, and the cost |
| `download.start` / `download.piece` / `download.done` / `download.fail` | a file's transfer under `--mode multi`, `partial` or `crawl --download`: the job, each finished piece, the custody record, and why a file stopped |
| `detect.result` | which listing templates read a target, and how well |
| `stream.hello` / `stream.gap` / `stream.heartbeat` | the protocol's own |

## Falling behind

A consumer that reads slower than the run publishes loses its oldest events — the run is
never blocked by a grep — and is told so in band:

```json
{"kind":"stream.gap","data":{"lost":57,"resuming_at":958,"hint":"this consumer fell behind; the run's JSONL on disk is complete"}}
```

Silent loss would be the worst failure this feature could have: an incomplete keyword
index that looks complete. So the loss is counted exactly, reported once, and the JSONL
in `crawls/<job_id>/` remains the complete record to reconcile against.

## Flags

| Flag | Effect |
|---|---|
| `--stream [HOST:PORT]` | enable; default `127.0.0.1:8787`. A bare port works, and port `0` picks a free one and logs which |
| `--stream-token TOKEN` | require `Authorization: Bearer TOKEN` (or `?token=`). **Mandatory for any bind that is not loopback** |
| `--stream-buffer N` | events kept for replay (default 10000) |
| `--stream-bodies` | also publish page text, capped by `--stream-body-bytes` (default 64 KiB) |
| `--stream-wait` | hold the run until a consumer has attached, so the index sees the first finding |

The stream carries the target's URLs and the names of its files, so the defaults are the
careful ones: loopback only, no CORS, the `Host` header checked so a page in a browser
cannot reach it by rebinding DNS, and a flat refusal — an error, not a warning — to bind
anywhere but loopback without a token. To watch a run on another machine, forward the
port over SSH rather than binding it wide.

## A consumer to start from

`contrib/streamhunt.py` is a working example and about a hundred lines long: it takes a
keyword file (one per line, `/regex/` for a regex), matches every finding against it,
writes hits as JSONL, reconnects from its last sequence number if the socket drops, and
records any gap as a hit of its own.

```bash
python3 OnionAccelerator.py --mode crawl --url http://target.onion/ --stream --stream-wait &
python3 contrib/streamhunt.py --keywords scope.txt --out hits.jsonl
```

```
[+] Acme Ltd.: http://target.onion/clients/acme/
[+] /payroll_\d{4}/: http://target.onion/hr/payroll_2019.xlsx
[*] 4812 event(s) searched, 2 hit(s), 0 lost, 143s
```

The stream is read-only. There is no way to steer a run through it — no seed injection,
no stop command — by design.

## Waiting for a target to go live (`contrib/sensor.py`)

Some targets — a leak site's per-victim "final" endpoint, most often — do not serve
anything crawlable the moment they are known about. `contrib/sensor.py` polls one URL
with `--mode crawl --detect` on an interval, and the moment that call reports the target
usable, it stops polling and feeds the newest job directory under `crawls/` to
`--resume` — so a crawl started early (or cut short because the target was not up yet)
continues instead of starting over:

```bash
python3 OnionAccelerator.py --mode crawl --url http://target.onion/dl/1/ --max-pages 1 &
wait   # let the first pass fail/park, leaving a frontier journal to resume from

python3 contrib/sensor.py --url http://target.onion/site/data?uuid=... \
    --interval 300 -- --socks 127.0.0.1:5000,127.0.0.1:5001
```

Everything after the `--` is passed through unchanged to both the `--detect` poll and
the `--resume` run it triggers. `--resume-dir JOB_DIR` picks a job explicitly instead of
the newest one; `--max-polls N` gives up instead of polling forever. It never opens a
socket itself — detection and the resumed crawl both go through
`OnionAccelerator.py`, so proxy selection and the farm's own safety rails apply exactly
as they would to a run started by hand.
