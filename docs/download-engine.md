# The download engine (`--mode multi`, `--mode partial`, `crawl --download`)

One engine moves every file, and it is built to be interrupted. A sized file is cut into
fixed **pieces** (`--piece-size`, 4 MiB by default). Each lane — one isolated Tor circuit —
pulls the next piece from a shared queue, so a fast lane does more of them, and writes it
at its offset into a single preallocated file. A lane holds at most one piece in memory, so
the footprint is lanes × piece size whatever the file weighs.

**On disk, while it runs** (`<name>` is the file's final path):

| File | What it is |
|---|---|
| `<name>.oa-part` | the file itself, preallocated, pieces written in place |
| `<name>.oa-state` | an append-only JSON-lines journal: a job header, then one line per finished piece with its SHA-256, lane and attempts. Each line is one `write()`, so a kill tears at most the last, which the reader drops |

**Resume.** Run the same command again. The journal is read, every piece it calls finished
is re-hashed against `<name>.oa-part`, and only the rest is fetched. A journal that no
longer fits (a different size, piece size or ETag) is discarded rather than trusted;
`--fresh` discards it on purpose. A piece that runs out of `--retries` stops that file and
**deletes nothing** — the process exits 1 and the file is resumable. No new pieces are taken
once one gives up, but the ones already in flight get up to ten seconds to land, since
over Tor each is minutes of someone's bandwidth and the rerun will not have to fetch it.

**A file that changes underneath it.** The size probe's `ETag` (or `Last-Modified` when
the ETag is weak or absent) is sent as `If-Range` with every piece. A server that sees a
different version answers `200` with the new entity; a server that ignores `If-Range` gives
itself away by a different `ETag` on the `206`, or a different total in `Content-Range`.
Either way the file starts over from byte zero **once**, so the result is never spliced
from two versions, and fails if it changes again. A server that answers ranged requests
with `200` regardless gets the same one restart and then a single plain GET. A server that
sends no validators at all can only be caught by a change of size, and the log says so.

**Slow and stalled lanes.** A piece that has run far below the median lane (under a tenth
of it, for 20 seconds) is cancelled, requeued, and its lane rotated onto a new circuit. The
rule is relative on purpose: when every lane is slow the median is low too and nothing is
cancelled. Among the last few pieces, one that is **running late** — twice the median finished
piece, and never before a second — is raced on a second lane, and the first to finish wins, so
one bad circuit cannot hold a finished file hostage. Only a late piece is raced: when the lanes
are equal the pieces finish together, and racing them anyway would just fetch the same bytes
twice (it did, until a benchmark measured it).

**Custody record.** When a file completes, the journal and the part file are gone and
`<name>.oa.json` is written beside it:

```json
{
  "kind": "custody", "job_id": "20261113_103000_ab12cd", "url": "http://example.onion/a.iso",
  "path": "partials/example.onion/a.iso", "size": 734003200,
  "sha256": "…whole-file SHA-256, computed from what is on disk…",
  "etag": "\"5d8c72a5edda8\"", "last_modified": "Wed, 01 Oct 2025 08:00:00 GMT",
  "piece_size": 4194304, "pieces": 175, "resumed_pieces": 0, "restarts": 0,
  "lanes": [{"lane": 0, "endpoint": "socks5h://127.0.0.1:5000", "pieces": 9}],
  "started_at": "2026-11-13T10:30:00Z", "finished_at": "2026-11-13T10:41:12Z"
}
```

A file with no discoverable size, or that fits in one piece, is a single GET: there is
nothing to resume, so a failure leaves nothing behind.

**Identity.** Every lane keeps one User-Agent for its whole life — one lane is one identity
as far as the far end can tell — and the size probe goes out on a lane's own credentialed
proxy, not the daemon's shared circuit. Endpoints are resolved the way tree and crawl do it
(`--socks`, `--external`, the farm probe, then a personal Tor client on `127.0.0.1:9150`);
the old fixed twenty-port fallback and the 20-worker cap are gone.

With `--stream`, the transfer publishes `download.start`, `download.piece`, `download.done`
and `download.fail` (see [Live Streaming API](streaming.md)). A piece event is the journal line written
for it and `download.done` is the custody record, byte for byte; the start and fail events
of a single-GET file have no journal to match.
