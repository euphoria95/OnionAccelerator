# Remote archive tree (`--mode tree`)

- Lists or extracts members of a **huge remote archive** (`.rar`, `.tar`, `.zip`, `.7z`,
  `.tar.xz`) using HTTP Range requests, **without downloading it**. A 100 GB archive costs
  kilobytes to read, because every supported format keeps its metadata somewhere a handful
  of range requests can reach.
- Backed by [`remote_viewer/rvtree`](../../remote_viewer/), driven over the same `host:port`
  SOCKS5 pool the download modes use — so a `--farm` of independent Tor daemons, or an
  `--external` proxy list, gives it real parallelism.
- Solves the **proof-of-work gates** some services put in front of a download, and says
  so — a challenge page answered with 200 otherwise looks like the archive itself, and
  every size and range it reports is a fact about the wrong entity.
- Falls back to `--spool` when a server refuses ranges outright, which is the one case
  the whole premise does not survive: it costs a full transfer, so it is offered with a
  time estimate rather than taken (see **Gates, and servers that refuse ranges** below).
- Everything after `--` is passed straight to rvtree, so the full `list` / `extract` /
  `probe` surface is available with no flags to keep in sync.

## Details

Reading a 2.4 GB `.rar` on an onion service to find out what is in it means transferring
2.4 GB — unless you only fetch the headers. That is what `rvtree` does, and `--mode tree`
runs it over OnionAccelerator's proxy fleet.

```bash
# What is in this archive, and what did it cost to find out?
python3 OnionAccelerator.py --mode tree --url https://example.onion/backup.rar

# The same thing spelled the long way. --url is inserted exactly here.
python3 OnionAccelerator.py --mode tree -- list https://example.onion/backup.rar

# Server capabilities and archive shape first — about 65 KiB.
python3 OnionAccelerator.py --mode tree -- probe https://example.onion/backup.tar.xz

# Pull one member out of it. --url lands before the member path, where rvtree wants it.
python3 OnionAccelerator.py --mode tree --url https://example.onion/backup.rar -- extract etc/hosts -o hosts

# Stream a huge tree as it is discovered, so an interrupted run keeps what it had.
python3 OnionAccelerator.py --mode tree -- list https://example.onion/big.tar.xz -f ndjson > tree.ndjson

# Through the external proxy list instead of a local farm.
python3 OnionAccelerator.py --mode tree --external --test-url http://your-own/ping -- list https://example.onion/a.7z

# A target behind a proof-of-work gate that then refuses ranges: pay for one full pass
# and keep the file (--spool-temp throws it away once the listing is printed).
python3 OnionAccelerator.py --mode tree --url https://example.onion/DEADBEEF/addresses.7z -- list --spool addresses.7z
```

**Why the farm matters here.** rvtree's own default is a single Tor daemon with several
credential-isolated circuits, and that is what makes it slow: one daemon is one guard and
one single-threaded crypto path, so the circuits queue behind each other. A recorded
production run listed a 2.4 GB RAR in **2 h 3 m**, spending 97.7% of that wall clock
waiting on 7,540 sequential 4 KiB round trips — every one of them on the *same* circuit.

`--mode tree` replaces that with the concept the download modes already use:

- **Endpoints, not circuits.** Each `host:port` is an independent Tor daemon with its own
  guard and its own bandwidth. Discovered from the farm automatically.
- **Round-robin lanes with failover.** A failed request parks its endpoint and retries on
  a different one, exactly as `ProxyPool` does for downloads.
- **Size-aware range splitting.** A bulk read — an xz block, a solid 7z folder — is split
  across lanes using the same `min(lanes, size // 1 MB)` rule as partial-download mode.
- **Hedged small reads.** A header read under 64 KiB is raced across two or three
  *different* daemons and the first answer wins. This is the only lever that helps a
  header chain, because the chain cannot be parallelised — entry *n+1*'s offset is not
  known until entry *n* has been read.
- **Parallel segmented walking.** For RAR and plain tar, the archive is divided into
  segments, a verified header is located in each (RAR5 by its CRC32, RAR4 by its CRC plus
  its successor, tar by its magic and checksum), and several walkers run at once. Walker 0
  starts where the format says the chain starts, and every later walker is believed only
  once the walker before it *arrives* at the offset it began from — so a boundary that
  turns out to be wrong costs time, never entries.

**Without a farm** the mode falls back to a local Tor client on `127.0.0.1:9150`, warns
that it is a single daemon, and suggests bringing a farm up. It works, but one daemon is
the thing that made it slow in the first place:

```bash
sudo python3 OnionAccelerator.py --farm up --count 8
python3 OnionAccelerator.py --mode tree -- list https://example.onion/backup.rar
```

rvtree writes its own full debug log to `logs/rvtree_<job_id>.log` for every run. Add `-v`
to watch the pipeline, or `-vv` for one line per range request.

## Gates, and servers that refuse ranges

Some services do not serve a download directly. The first GET of `file.7z` answers **200
with an HTML challenge page** — a proof-of-work interstitial that hands out an input and a
difficulty and only produces the file once a client hashes its way to a nonce and posts it
back. Nothing about that response says "this is not your archive": it is a 200, it has a
Content-Length, and every capability read off it describes the challenge page. rvtree
solves the challenge — it is a cost function, not a secret, and spending the CPU is
exactly what the gate asks of any client — and then reports that it did:

```console
$ python3 OnionAccelerator.py --mode tree -- probe http://example.onion/DEADBEEF/addresses.7z
size           1,877,268,564 bytes (1.7 GiB)
gate           proof-of-work — solved; the size above is the real entity
accept-ranges  NO — only --spool can read this archive, at a full download
```

Passing the gate is the easy half. The URL it grants is served by the application rather
than the web server, so it is single-use, non-resumable, and answers a `Range` request
with 200 and the whole body. That removes the premise this mode is built on, because a 7z
end header, a zip central directory and an xz index all live at the *far end* of the
stream — there is no way to reach them but to receive everything before them.

So `list` refuses, exits **3**, and prints what the alternative costs: 1.7 GiB at the
throughput it just measured, with no resume if the circuit dies. `--spool FILE` accepts
that — one stream to disk, then the archive is read locally at local speed. The file is
kept and a complete one is reused rather than fetched twice, which matters precisely
because a broken transfer restarts at zero. `--spool-temp` deletes it afterwards, for when
the listing was all you wanted. More endpoints do not help: every stream starts at byte
zero, so a second lane would only fetch the same bytes again.
