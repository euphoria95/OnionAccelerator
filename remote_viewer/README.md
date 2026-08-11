# rvtree

List the file tree of a very large remote archive — 100 GB is fine — over Tor, without
downloading it. Supports `.tar.xz`, `.zip`, `.7z`, `.rar` and plain `.tar`, and can pull
out a single member once the tree is known.

```bash
rvtree probe   https://example.onion/backup.tar.xz
rvtree list    https://example.onion/backup.tar.xz
rvtree extract https://example.onion/backup.tar.xz path/inside/file.txt -o file.txt
```

Everything goes through `socks5h://127.0.0.1:9150` by default. `socks5://` is refused:
it resolves DNS locally, which leaks the target host and cannot reach `.onion` names.

## Requirements

Python 3.9+, plus `requests` and `PySocks` (both usually already present). Listing any
supported format, and extracting from all of them except a *compressed* RAR member, needs
nothing else — see [RAR](#rar) for that one exception. A Tor client must be listening on
`127.0.0.1:9150`:

```bash
ss -ltnp | grep 9150
```

If that is empty, check which Tor instance is actually running. A host that also runs a
Tor **relay** will have an `/etc/tor/torrc` declaring `SocksPort 9050` with nothing
listening on it — the client instance (`/etc/tor/instances/torclient/torrc` on Debian
and Ubuntu) is the one that serves SOCKS. Point `--proxy` at whichever port is live.

## Running it

No install needed — run it as a module from the directory holding the `rvtree/` package:

```bash
cd /path/to/remote_viewer
python3 -m rvtree probe https://example.onion/backup.tar.xz
```

Or install it to get a real command that works from anywhere:

```bash
pip install -e .
rvtree probe https://example.onion/backup.tar.xz
```

On Debian and Ubuntu, a bare `pip install` may refuse with
`externally-managed-environment`. Use a virtualenv, or `pip install --user -e .`, or
add `--break-system-packages`.

## Start with `probe`

It costs about 65 KiB and tells you whether the job ahead is cheap or expensive:

```console
$ rvtree probe https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.6.tar.xz
url            https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.6.tar.xz
size           140,064,536 bytes (133.6 MiB)
accept-ranges  yes
server         nginx
content-type   application/x-xz
etag           -
last-modified  Mon, 30 Oct 2023 06:14:38 GMT
tls            not verified (default; --verify-tls to enforce)
format         tar.xz (tar inside an XZ stream) via magic bytes
xz blocks      57 in 1 stream(s)
uncompressed   1,419,089,920 bytes (1.3 GiB)
random access  yes — 23.7 MiB granularity
fetched        64.7 KiB in 6 requests
```

Two lines decide everything. `accept-ranges: no` means the server cannot serve partial
content and rvtree cannot work against it at all. `random access: NO` means the archive
is a single-block `.xz`, and `list` will refuse with a time estimate until you pass
`--force` — see [The case with no shortcut](#the-case-with-no-shortcut).

`format ... via` says what identified the archive, and `tls` says whether the
certificate was checked — see [Knowing what it is](#knowing-what-it-is) and [TLS](#tls).

## Options

| Flag | What it is for |
|---|---|
| `--limit-blocks N` | Read only the first N xz blocks — a cheap way to sample a huge archive |
| `-f ndjson` | Stream entries as they are found; survives interruption, suits millions of files |
| `-f tree \| long \| json` | Other output shapes; `long` is `ls -l`-like |
| `--circuits N` | xz block prefetch depth (default 4) |
| `--proxy none` | Direct connection with no Tor, for local testing |
| `--endpoints LIST` | `host:port[,host:port…]` — one independent Tor daemon each. This is where parallelism actually comes from; see [Endpoints](#endpoints) |
| `--endpoint-file PATH` | The same list, one per line |
| `--circuits-per-endpoint N` | Circuits opened on each endpoint (default 1) |
| `--hedge N` | Copies of a small (<64 KiB) range raced across *different* daemons (default 3; 1 disables) |
| `--walkers N` | Walkers to split a RAR or tar header chain between (default: one per lane; 1 walks serially) |
| `--user-agents PATH` | TSV whose first column is a User-Agent; one is pinned per endpoint |
| `--force` | Proceed on a single-block `.xz` despite the projected time |
| `--limit N` | Stop after N entries |
| `--max-fetch MiB` | Refuse an extraction that would download more than this (extract only, default 64) |
| `--verify-tls` | Verify TLS certificates — off by default, see [TLS](#tls) |
| `--ca-bundle PATH` | Verify against a private CA instead of the system store (implies `--verify-tls`) |
| `--archive-type T` | Force the parser instead of detecting it — see [Knowing what it is](#knowing-what-it-is) |
| `-v`, `-vv` | Say what is happening; `-vv` adds a line per range request — see [Watching a run](#watching-a-run) |
| `--debug` | `-vv` plus timings, thread names and a real traceback |
| `--progress WHEN` | `auto` (default), `always` or `never` |
| `--log-file PATH` | Write the full debug log here regardless of `-v` |

`rvtree --help` prints the whole reference: every mode, every option with its syntax,
examples, and the exit codes. `rvtree help list` narrows it to one mode.

## Watching a run

Listing a large archive over Tor takes minutes, and most of that time is spent inside one
of five steps. A run in a terminal draws a bar naming the step it is on:

```
walk  block 27/57  614.2 MiB / 1.3 GiB  [███████████░░░░░░░░░░░]  47%
  7.3 MiB fetched · 15 req · 486 KiB/s · 3,412 entries · 0:42 · ETA 0:47
```

The percentage is the walker's position in the stream, not bytes downloaded — those are
different numbers, and the first is the one that predicts when the run ends. `fetched`
counts bytes arriving, not requests completing, so a single multi-megabyte block still
moves it. The bar lives on stderr, appears only when stderr is a terminal, and stands
down when payload is going to that same terminal — `-f ndjson`, or `extract` without
`-o`. `--progress always` forces it on (a redirected stderr then gets a plain line every
couple of seconds); `--progress never` and `-q` turn it off.

`-v` names the steps instead, which is what you want when a run is redirected or when
something went wrong:

```console
$ rvtree list https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.6.tar.xz -v
probe           https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.6.tar.xz
transport.tor   https://cdn.kernel.org/...: 133.6 MiB, ranges honoured, server nginx
detect          133.6 MiB
formats.detect  identified as xz via magic bytes
index           reading the xz block index
formats.xz      xz index: 57 block(s) in 1 stream(s), 1.3 GiB uncompressed, random access yes
sniff           is there a tar inside?
archive         block 0 decodes to a tar header: this is a tar.xz
walk            tar inside an XZ stream
```

`-vv` adds one line per range request, which is the level at which a stalled or
misbehaving server becomes legible — every byte rvtree asks for, which circuit carried
it, how long it took, and every retry and circuit rotation:

```console
$ rvtree list https://example.onion/backup.tar.xz -vv
transport.tor   GET bytes=0-263 (264 B) on circuit 3
transport.tor   got  bytes=0-263: 264 B in 0.94s (281 B/s)
formats.detect  first bytes: fd 37 7a 58 5a 00 00 04 e6 d6 b4 46 03 c0 de ff
transport.tor   GET bytes=12-3512833 (3.4 MiB) on circuit 2
formats.xz      block 1/57: 3.4 MiB fetched in 6.91s, decoded to 23.7 MiB in 0.31s
formats.xz      prefetching block(s) 2, 3, 4
transport.httpfile  readahead 128.0 KiB -> 32.0 KiB (11% of the last buffer was used)
```

Retries are reported at the default verbosity, without any flag: a run that has gone
quiet for two minutes because a Tor exit died should say so on its own.

`--debug` is `-vv` plus timestamps, logger and thread names, and a real traceback instead
of the one-line error. `--log-file PATH` writes that full detail to a file whatever the
console was told, which is the thing to attach to a bug report:

```bash
rvtree list https://example.onion/backup.tar.xz --log-file rv.log
```

### Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | the archive or the transfer failed — unreachable, unreadable, unparseable |
| 2 | bad arguments, or a proxy setting that would leak the target hostname |
| 3 | refused pending your decision — a single-block `.xz`, or an extraction over `--max-fetch`. Re-run with `--force`, or raise `--max-fetch` |
| 4 | encrypted headers: the file list itself needs the password |
| 130 | interrupted (Ctrl-C) |

## Knowing what it is

The URL is the last thing rvtree trusts about the format. A download endpoint often has
no extension at all — `https://example.onion/dl?id=42` — and one that does have an
extension may be lying. So identification runs down a ladder, stopping at the first
answer, and `probe` prints which rung it stopped on:

1. **Magic bytes.** One 264-byte range request, which is enough for every format here.
   A ZIP with an executable stub in front is caught by its trailing central directory.
2. **`Content-Disposition: filename=`.** The name the server itself gives the file. This
   is the usual answer for an extensionless download endpoint, and it costs nothing:
   the header already arrived with the size probe.
3. **`Content-Type`.** Only the specific ones; `application/octet-stream` is ignored,
   since servers hand it out for everything.
4. **The URL path.**

Rungs 2–4 only ever run when the bytes say nothing, which in practice means a format
with no magic at all — a v7 tar — or a response that is not the archive you asked for.
That last case is worth its own error rather than a shrug:

```console
$ rvtree list https://example.onion/dl?id=42
rvtree: could not identify this as an archive: it starts with
3c 68 74 6d 6c 3e 34 30 34 20 6e 6f 74 20 66 6f, Content-Type 'text/html'.
A server error page or a login redirect looks exactly like this.
```

A name can of course be wrong, so when a guessed parser then rejects the bytes the error
says which piece of evidence led there rather than surfacing the parser's own complaint:

```console
rvtree: identified as tar from Content-Disposition filename 'nightly-backup.tar', but it
does not parse as one: truncated header
The bytes start with 3c 68 74 6d 6c 3e 3c 74 69 74 6c 65 3e 34 30 33, which is not a
POSIX tar archive. Pass --archive-type to name the right parser.
```

`--archive-type zip|7z|tar|xz|tar.xz|rar` overrides the lot. (Not to be confused with
`-f/--format`, which is the shape of the *output*.)

`.tar.gz`, `.tar.bz2` and `.tar.zst` are recognised and then refused by name, because
none of the three has a block index: nothing inside can be reached without decompressing
everything before it, so listing one means downloading all of it. That is the same wall
as a single-block `.xz`, minus the escape hatch.

A self-extracting RAR hides its signature behind an executable stub, so the magic bytes
at offset 0 say nothing. Pass `--archive-type rar` and rvtree will scan the first
megabyte for it. That scan is not in the detection path on purpose: paying a megabyte to
identify every unrecognisable URL would cost more than it saves.

## TLS

**Certificates are not verified by default.** An onion address *is* the service's public
key, so the address you typed has already authenticated the peer end to end; a CA's
opinion adds nothing, and onion services accordingly almost never carry a certificate
that would satisfy one. Verifying by default would just make HTTPS onions unreachable.

The trade-off is real for clearnet hosts, where the exit node is untrusted by design and
an unverified connection is one it can sit in the middle of. `--verify-tls` restores the
system CA check for a run; `--ca-bundle PATH` verifies against a CA you chose instead.

```console
$ rvtree probe https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.6.tar.xz --verify-tls
...
tls            verified
```

`REQUESTS_CA_BUNDLE` and `CURL_CA_BUNDLE` cannot quietly switch verification back on:
`verify` is set per request, not just on the session, precisely so the default holds.

## Why this works

Each format keeps its metadata somewhere reachable with a few HTTP Range requests.

| Format | Where the metadata lives | Cost to list a 25 MB fixture |
|---|---|---|
| `.zip` | Central Directory at the end | 1.5 KiB, 4 requests |
| `.7z` | LZMA-compressed header at the end | 615 B, 4 requests |
| `.tar` | nowhere — the header chain must be walked | 264 KiB, 10 requests |
| `.rar` | nowhere — a chain of self-describing headers, walked like tar | 41.5 KiB, 18 requests |
| `.tar.xz` | XZ block index at the end, then the blocks holding tar headers | 7.3 MiB, 15 requests |

The XZ index is the interesting one. It lists every block's compressed and uncompressed
extent, so a multi-block `.xz` becomes randomly accessible. For the real
`linux-6.6.tar.xz` (140 MB), a 12-byte footer plus a 444-byte index reveals all 57
blocks of 24 MiB each; one 3.4 MB range request then yields 5,885 entries.

## What it costs

**Cost tracks tar header density, not archive size.** Only the blocks that contain tar
headers need fetching:

- an archive of large files puts headers in a few blocks — listing is nearly free;
- `linux-6.6.tar.xz` carries 245 headers per uncompressed MiB, so every block holds
  some and a full listing costs roughly the whole file.

`rvtree probe` reports the block map before you commit to anything.

### The case with no shortcut

A `.xz` written by single-threaded `xz` is one block, so nothing in it can be reached
without decompressing everything before it. rvtree detects this from the index, prints
the projected transfer and wall-clock time, and exits rather than silently starting a
multi-hour download. Pass `--force` to go ahead.

## RAR

RAR has no central directory at all: every file header sits immediately after the
previous member's packed data. Listing one means walking that chain, the same shape as
`.tar` — yet it costs six times less on the same payload, because rvtree drives these
reads itself and pulls a 4 KiB window per header rather than paying the file object's
16 KiB readahead floor. Cost tracks the number of members, not the size of the archive.

RAR5 can carry a quick-open record caching every header near the end of the file, which
would make listing a footer read. WinRAR compresses it, so it sits behind the same wall
as member data; rvtree skips it and walks. The table above is the normal path, not a
fallback.

Extraction has three tiers:

- **stored members** (`rar -m0`, which is how already-compressed payloads are usually
  added) are a single range request and a CRC check — no external tools;
- **compressed members** need a decompressor, and RAR's is proprietary with no stdlib
  decoder and no credible pure-Python one. rvtree fetches only that member's packed
  bytes, splices them into a small synthetic archive built from *this* archive's own
  verbatim headers, and hands that to an installed `unrar` (or `7z`). Because the headers
  are copied byte for byte, nothing is re-CRCed and no mistake in rvtree can corrupt a
  name, a size or a method. The output is checked against the size and CRC the real
  header promised, so a helper that misbehaves fails loudly instead of returning wrong
  bytes. Without `unrar` or `7z` installed, this tier — and only this tier — refuses;
- **solid members** can only be reached through every member before them, so the whole
  solid run is spliced in. `rvtree extract` reports that cost up front and `--max-fetch`
  guards it, exactly as it does for a solid `.7z` folder.

Encrypted headers (`rar -hp`) are refused by name: without the password not even the file
list can be read.

A multi-volume set is listed one volume at a time. The members held in the volume you
point at are real and complete; a member continuing into the next volume is flagged, and
extracting it refuses. rvtree will not guess the sibling URLs — the naming schemes are
ambiguous (`.part2.rar` against `.r00`), and probing invented URLs over Tor is both slow
and a distinctive thing to be seen doing.

## Endpoints

`--proxy` puts every circuit on one Tor daemon, and that is the ceiling. One daemon is one
guard and one single-threaded crypto path, so extra circuits queue behind each other —
measured on this host, 555 KiB/s on one circuit and only 1341 KiB/s across six. Worse, the
circuit pool behind `--proxy` is a LIFO stack, so a *sequential* caller gets the same
circuit back every time: a recorded two-hour run issued 7,540 range requests and sent
every one of them over circuit 3 while three prepaid circuits sat idle.

`--endpoints` fixes both. Each entry is a separate SOCKS5 daemon with its own guard and
its own bandwidth, and lanes are handed out round-robin rather than off a stack:

```bash
# A farm of eight independent Tor instances (see OnionAccelerator's --farm).
rvtree list https://example.onion/big.rar \
       --endpoints 127.0.0.1:5000,127.0.0.1:5001,127.0.0.1:5002,127.0.0.1:5003 \
       --endpoints 127.0.0.1:5004,127.0.0.1:5005,127.0.0.1:5006,127.0.0.1:5007
```

What the pool then does, keyed purely on how many bytes were asked for:

- **under 64 KiB — hedge.** The same range goes to two or three *different* daemons and
  the first answer wins. A header chain cannot be parallelised, so the only way to make it
  finish sooner is to make each round trip finish sooner. Copies on the same daemon would
  race nothing, so hedging needs more than one endpoint and switches itself off without one.
- **under 1 MiB — plain.** One lane, with failover to a different endpoint on retry.
- **1 MiB and over — split.** Divided across lanes, `min(lanes, size // 1 MiB)` ways, and
  reassembled. This is what an xz block or a solid 7z folder rides on.

A failing endpoint is parked and the retry lands elsewhere; one that succeeds again is
un-parked. If every endpoint is parked the pool hands one back anyway, because a stale
retry beats giving up.

### Walkers

RAR and plain tar have no index: listing them means following a chain where entry *n+1*'s
offset is only known once entry *n* has been read. That is one round trip per member, and
it is where the two hours went.

`--walkers` enters the chain in several places at once. Each walker starts at a header
found by probing and *verified where it was found* — a RAR5 block by its CRC32, a RAR4
block by its CRC plus its successor, a tar header by its magic and checksum. None of that
has to be right: walker 0 starts where the format says the chain starts, and every later
walker's entries are used only once the walker before it has arrived at exactly the offset
it began from. A boundary that was really the middle of a member is detected there and the
stretch is re-walked serially, so a wrong guess costs time and never entries.

The division is by bytes, so the gain tracks how evenly members are spread. Archives whose
members cluster will see less of it than the count of walkers suggests.

## Tor notes

Each circuit gets its own SOCKS credential, which is what makes Tor give it a separate
circuit. With `--endpoints` that stacks on top of daemon count:
`--circuits-per-endpoint` circuits on each of them.

Two server behaviours are worth knowing, both observed as HTTP 501 from kernel.org's
cache tier and both handled:

- **Suffix ranges** (`Range: bytes=-12`) can be rejected. rvtree always computes an
  absolute range from `Content-Length`, including for `seek(-n, SEEK_END)`.
- **Multi-range requests** can be rejected. Nothing depends on them.

If a server answers a Range request with `200`, rvtree aborts before reading the body —
on a 100 GB archive, quietly accepting the full entity would be ruinous.

## Development

From the project root:

```bash
bash tests/make_fixtures.sh      # once — builds the local archive corpus
python3 -m pytest                # 244 offline tests, no network, ~85s
python3 -m pytest -m network     # 7 live tests over Tor against kernel.org, ~3.5 MB, ~90s
```

Network tests are excluded by default, so plain `pytest` never touches the internet and
needs no Tor. Offline tests run against `tests/rangeserver.py`, which implements real
Range support (`http.server` does not), can simulate both 501 behaviours above, and can
serve over TLS with the self-signed certificate `make_fixtures.sh` generates — which is
what the TLS tests point at. If `openssl` is missing those tests skip.

The RAR fixtures are the odd ones out: no `rar` binary is freely installable, and `unrar`
and `7z` only ever read the format, so `tests/rarwriter.py` writes them in pure Python.
They are real archives rather than stand-ins, and the tests prove it rather than assuming
it — `unrar` is the ground truth for what they contain, and a fixture written by the same
understanding that reads it would otherwise only confirm its own mistakes. Tests needing
`unrar` skip without it; the parser and the stored-extraction tests do not need it.

## Layout

```
rvtree/
  archive.py            format dispatch, listing, extraction, single-block guard
  cli.py                list | extract | probe | help
  progress.py           stage display and progress bar, sampled rather than pushed
  transport/
    tor.py              circuit pool, SOCKS5h isolation, response validation
    httpfile.py         seekable file over Range, adaptive readahead
    probe.py            capability probe
  formats/
    detect.py           magic sniffing, then filename/header fallbacks
    xz.py               block index, FORMAT_RAW block decode, seekable stream
    tarwalk.py          stdlib tarfile driver + speculative header scan
    sevenzip.py         7z header parser
    rar.py              RAR4 + RAR5 header walk, unrar-assisted extraction
    zipfmt.py           stdlib zipfile wrapper
```

`sevenzip.py` and `rar.py` are the two hand-rolled parsers; nothing in the stdlib reads
either format. Elsewhere, `zipfile` and `tarfile` do the format parsing — they already handle
ZIP64, prepended-data self-extracting archives, pax headers, GNU long names and sparse
members, and they work unmodified on a range-backed file object.
