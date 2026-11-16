"""The one benchmark that uses Tor: your own onion, through your own farm.

    python -m bench.onion --url http://YOURS.onion/test.bin --lanes 1,4,8

Nothing else in bench/ touches a network. This does, so it is separate, needs a target you
operate (a static file server behind an onion service you run, ideally one you can also see
the other end of), and a farm that is up: `--farm up`, or `--socks` for daemons you manage.

What it prints is a table with the date, the Tor version, the lane counts and the file size,
because a number from this run is a statement about *that onion on that day*: guard luck,
the relays between, and the time of day move it by more than any software change here does.
Repeat it, and publish a number only with the conditions beside it.

The onion address is never printed, logged to a file you keep, or written into the table: it
is reported as `<onion>`. The transfer runs in a scratch directory that is removed after.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

from . import runners
from .report import Table, render_table


def tor_version() -> str:
    binary = os.environ.get("ONIONACCELERATOR_TOR") or shutil.which("tor")
    if not binary:
        return "unknown (no tor binary found)"
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=10)
        return (out.stdout.splitlines() or ["unknown"])[0].strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def resolve_endpoints(spec: str, base_port: int, count: int) -> list:
    if spec:
        return [e.strip() for e in spec.split(",") if e.strip()]
    import OnionAccelerator as oa
    return oa.discover_local_socks_ports(base_port=base_port, max_ports=count)


def one_run(url: str, endpoints: list, piece_mib: int, expect_sha: str, timeout: float):
    """(seconds, bytes, ok) for one transfer over `endpoints`, in a scratch directory."""
    workdir = tempfile.mkdtemp(prefix="bench-onion-")
    try:
        script = runners._ENGINE.format(root=runners.ROOT, url=url, endpoints=endpoints,
                                        retries=3, kwargs={"piece_size": piece_mib << 20,
                                                           "circuits_per_endpoint": 1,
                                                           "fresh": True})
        proc = runners._spawn(script, workdir)
        result = runners._finish(proc, timeout)
        if result is None:
            return 0.0, 0, False
        files = [os.path.join(d, f) for d, _, fs in os.walk(os.path.join(workdir, "partials"))
                 for f in fs if not f.endswith((".oa.json", ".oa-state", ".oa-part"))]
        if not files:
            return result["seconds"], 0, False
        size = os.path.getsize(files[0])
        ok = bool(result["ok"] and result["ok"][0])
        if ok and expect_sha:
            ok = runners._sha256(files[0]) == expect_sha
        return result["seconds"], size, ok
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bench.onion", description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", required=True, help="a file on an onion service you operate")
    ap.add_argument("--lanes", default="1,4,8", help="comma-separated lane counts (default 1,4,8)")
    ap.add_argument("--socks", default="", help="host:port,host:port of daemons to use "
                                                  "(default: probe the farm range)")
    ap.add_argument("--base-port", type=int, default=5000)
    ap.add_argument("--count", type=int, default=20)
    ap.add_argument("--piece-size", type=int, default=4, metavar="MiB")
    ap.add_argument("--sha256", default="", help="the file's SHA-256, to count a run only if right")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--timeout", type=float, default=3600)
    args = ap.parse_args(argv)

    lane_counts = [int(x) for x in args.lanes.split(",") if x.strip()]
    endpoints = resolve_endpoints(args.socks, args.base_port, max(args.count, max(lane_counts)))
    if len(endpoints) < max(lane_counts):
        print(f"need {max(lane_counts)} SOCKS endpoints, found {len(endpoints)}; start a farm "
              f"(--farm up --count {max(lane_counts)}) or pass --socks", file=sys.stderr)
        return 1

    rows, size = [], 0
    for n in lane_counts:
        seconds, ok = [], 0
        for _ in range(args.reps):
            s, size_now, good = one_run(args.url, endpoints[:n], args.piece_size, args.sha256,
                                        args.timeout)
            if good:
                seconds.append(s)
                size = size_now
            ok += good
        med = sorted(seconds)[len(seconds) // 2] if seconds else float("nan")
        rows.append([str(n), f"{ok}/{args.reps}", "n/a" if med != med else f"{med:.1f}",
                     "n/a" if med != med or not size else f"{size / med / 2**20:.2f}"])
    table = Table("A transfer through Tor", ["lanes", "finished", "seconds", "MiB/s"], rows, [
        f"{time.strftime('%Y-%m-%d', time.gmtime())} UTC; {tor_version()}; {size / 2**20:.0f} MiB; "
        f"url <onion>; {len(endpoints)} SOCKS endpoints, one lane each. A statement about this "
        f"onion on this day, not about Tor."])
    print(render_table(table))
    print(json.dumps({"date": time.strftime("%Y-%m-%d", time.gmtime()), "tor": tor_version(),
                      "lanes": lane_counts, "bytes": size}), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
