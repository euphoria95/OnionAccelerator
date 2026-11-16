"""The benchmark suites. Each returns Tables; none of them prints.

Every number comes from a run against the lab (bench/lab.py) and is a median over `reps`.
A run that does not produce the right bytes is not timed: it is counted as a failure.

The parameters are in PROFILES, and the "full" profile is what docs/benchmarks.md is built
from. "quick" is a smoke run for checking the harness works; its numbers are not published.
"""

from __future__ import annotations

import os
import shutil
import statistics
import sys
import tempfile
import time

from . import lab, runners
from .report import NA, Table

MIB = lab.MIB

PROFILES = {
    "full": dict(file_mib=128, rate=8.0, lanes=(1, 2, 4, 8, 16), reps=3, slow_lanes=8,
                 flaky_lanes=8, flaky_mean_mib=24, flaky_reps=5, resume_lanes=8, resume_mib=128,
                 tar_members=50_000, list_reps=12, list_entries=25),
    "quick": dict(file_mib=16, rate=4.0, lanes=(1, 4), reps=1, slow_lanes=4, flaky_lanes=4,
                  flaky_mean_mib=8, flaky_reps=2, resume_lanes=4, resume_mib=48, tar_members=2_000,
                  list_reps=3, list_entries=10),
}

# Latency a Tor circuit has and a loopback socket does not. Modest, so the transfers are
# bandwidth-bound as they are in practice, but not zero, so a request is not free.
LATENCY = dict(rtt=0.02, jitter=1.0, connect_delay=0.05)


def _med(values):
    return statistics.median(values) if values else float("nan")


def _s(x: float) -> str:
    return NA if x != x else f"{x:.1f}"


def _stubs(n, rate, **extra):
    return [dict(rate_mib=rate, seed=i, **LATENCY, **extra) for i in range(n)]


def _timed(fn, reps, lb):
    """(median seconds, runs that were right, runs, median bytes served / file size)."""
    size = lb.info["sizes"]["/file.bin"]
    runs, served = [], []
    for _ in range(reps):
        lb.reset()
        r = fn()
        if r is None:
            continue
        runs.append(r)
        if r.ok:
            served.append(lb.stats()["bytes_sent"] / size)
    good = [r.seconds for r in runs if r.ok]
    return _med(good), len(good), len(runs), _med(served)


class Context:
    """What a suite run shares: the profile, the baseline checkout, whether aria2c exists."""

    def __init__(self, profile: str = "full", baseline: str = runners.DEFAULT_BASELINE):
        self.p = PROFILES[profile]
        self.profile = profile
        self.legacy = runners.prepare_baseline(baseline)
        self.baseline = baseline
        self.aria2c = runners.aria2c_path()

    def close(self):
        if self.legacy:
            shutil.rmtree(self.legacy, ignore_errors=True)


# ----------------------------------------------------------------------------- scaling


def _transfer_times(ctx, lb, mirrors: bool):
    """(engine, legacy, aria2c) median seconds for one lab, '-' where a system cannot run."""
    p = ctx.p
    nothing = (float("nan"), 0, 0, float("nan"))
    out = {"engine": _timed(lambda: runners.run_engine(lb), p["reps"], lb)}
    out["legacy"] = (_timed(lambda: runners.run_legacy(lb, ctx.legacy), p["reps"], lb)
                     if ctx.legacy else nothing)
    out["aria2c"] = (_timed(lambda: runners.run_aria2c(lb, lb.mirror_urls), p["reps"], lb)
                     if ctx.aria2c and mirrors else nothing)
    return out


def scaling(ctx: Context) -> list:
    p = ctx.p
    rows, waste = [], []
    for n in p["lanes"]:
        with lab.Lab(file_mib=p["file_mib"], stubs=_stubs(n, p["rate"]),
                     mirrors=bool(ctx.aria2c)) as lb:
            res = _transfer_times(ctx, lb, True)
        ideal = p["file_mib"] / (n * p["rate"])
        row, wrow = [str(n)], [str(n)]
        for system in ("engine", "legacy", "aria2c"):
            secs, good, total, served = res[system]
            wrow.append(NA if good == 0 else f"{served:.2f}x")
            if good < total or good == 0:
                row += [NA, NA] if good == 0 else [f"{_s(secs)} ({good}/{total} ok)", NA]
            else:
                row += [_s(secs), f"{100 * ideal / secs:.0f}%"]
        rows.append(row)
        waste.append(wrow)
    cols = ["lanes", "engine s", "engine % of ideal", "legacy s", "legacy % of ideal",
            "aria2c s", "aria2c % of ideal"]
    notes = [f"A {p['file_mib']} MiB file, each lane capped at {p['rate']:g} MiB/s, so the ideal "
             f"time is {p['file_mib']} / (lanes × {p['rate']:g}) seconds. Median of {p['reps']}."]
    notes.append("At 16 lanes both systems fall away from ideal together, so the limit is more likely the lab or the single Python interpreter driving the lanes than either design; aria2c, where installed, is the control.")
    if not ctx.legacy:
        notes.append(f"legacy: n/a, commit {ctx.baseline} is not in this clone.")
    if not ctx.aria2c:
        notes.append("aria2c: n/a, it is not installed on the machine that produced this page.")
    wnotes = ["What the content server sent, divided by the file size, on a clean network where "
              "every lane is identical. 1.00x is no duplicate bytes; anything above it is "
              "bandwidth spent twice. On Tor that is the number to watch."]
    return [Table("Throughput against lane count", cols, rows, notes),
            Table("Bytes served on a clean network", ["lanes", "engine", "legacy", "aria2c"],
                  waste, wnotes)]


# ----------------------------------------------------------------------------- slow lane


def slow_lane(ctx: Context) -> list:
    p, n = ctx.p, ctx.p["slow_lanes"]
    healthy = _stubs(n, p["rate"])
    one_slow = _stubs(n, p["rate"])
    one_slow[0]["rate_mib"] = p["rate"] / 10
    rows = []
    systems = ["engine", "legacy"] + (["aria2c"] if ctx.aria2c else [])
    times = {}
    for label, specs in (("healthy", healthy), ("slow", one_slow)):
        with lab.Lab(file_mib=p["file_mib"], stubs=specs, mirrors=bool(ctx.aria2c)) as lb:
            times[label] = _transfer_times(ctx, lb, True)
    for system in systems:
        h, hg, ht, _ = times["healthy"][system]
        s, sg, st, _ = times["slow"][system]
        if hg == 0 or sg == 0:
            rows.append([system, NA, NA, NA])
        else:
            rows.append([system, _s(h), _s(s), f"{s / h:.1f}x"])
    if not ctx.legacy:
        rows.append(["legacy", NA, NA, NA])
    notes = [f"{n} lanes at {p['rate']:g} MiB/s, then the same with lane 0 at one tenth of that. "
             f"The slowdown is the second time over the first; lower is better. Median of {p['reps']}."]
    return [Table("One lane at a tenth of the speed", ["system", "all lanes healthy s",
                                                          "one slow lane s", "slowdown"], rows, notes)]


# ----------------------------------------------------------------------------- flaky


def flaky(ctx: Context) -> list:
    p, n = ctx.p, ctx.p["flaky_lanes"]
    rows = []
    size = p["file_mib"] * MIB
    for system in ("engine", "legacy"):
        if system == "legacy" and not ctx.legacy:
            rows.append([system, NA, NA, NA, NA])
            continue
        specs = _stubs(n, p["rate"], fail_mean_mib=p["flaky_mean_mib"])
        times, served, resets, finished = [], [], [], 0
        with lab.Lab(file_mib=p["file_mib"], stubs=specs) as lb:
            for _ in range(p["flaky_reps"]):
                lb.reset()
                before = sum(s["resets"] for s in lb.stub_stats())
                run = (runners.run_engine(lb) if system == "engine"
                       else runners.run_legacy(lb, ctx.legacy))
                served.append(lb.stats()["bytes_sent"] / size)
                resets.append(sum(s["resets"] for s in lb.stub_stats()) - before)
                if run.ok:
                    finished += 1
                    times.append(run.seconds)
        rows.append([system, f"{finished}/{p['flaky_reps']}", _s(_med(times)),
                     f"{_med(served):.2f}x", f"{_med(resets):.0f}"])
    notes = [f"{n} lanes at {p['rate']:g} MiB/s; every connection is reset after an average of "
             f"{p['flaky_mean_mib']} MiB, so a piece or a chunk is regularly cut off mid-transfer. "
             f"'Bytes served' is what the content server sent divided by the file size: 1.00x is "
             f"no waste. Median of {p['flaky_reps']}."]
    return [Table("Connections that die mid-transfer",
                  ["system", "finished", "median s", "bytes served", "resets per run"], rows, notes)]


# ----------------------------------------------------------------------------- resume


def resume(ctx: Context) -> list:
    p, n = ctx.p, ctx.p["resume_lanes"]
    size = p["resume_mib"] * MIB
    rows = []
    for system in ("engine", "legacy"):
        if system == "legacy" and not ctx.legacy:
            rows.append([system, NA, NA, NA, NA, NA])
            continue
        killed_at, totals, second, base = [], [], [], []
        with lab.Lab(file_mib=p["resume_mib"], stubs=_stubs(n, p["rate"])) as lb:
            def once():
                return (runners.run_engine(lb) if system == "engine"
                        else runners.run_legacy(lb, ctx.legacy))

            for _ in range(p["reps"]):
                lb.reset()
                clean = once()
                if not clean.ok:
                    continue
                base.append(clean.seconds)
                lb.reset()
                workdir = tempfile.mkdtemp(prefix="bench-resume-")
                try:
                    def start():
                        return (runners.start_engine(lb, workdir) if system == "engine"
                                else runners.start_legacy(lb, workdir, ctx.legacy))

                    spawned = time.perf_counter()
                    first = start()
                    killed_at.append(runners.kill_after(lb, first, 0.5 * clean.wall, spawned))
                    time.sleep(0.3)                                  # let in-flight bytes settle
                    verdict = runners.verify(lb, workdir, system, runners._finish(start(), 600))
                    if verdict.ok:
                        second.append(verdict.seconds)
                        totals.append(lb.stats()["bytes_sent"] / size)
                finally:
                    shutil.rmtree(workdir, ignore_errors=True)
        extra = _med(second) / _med(base) if second and base else float("nan")
        rows.append([system, f"{100 * _med(killed_at):.0f}%", f"{_med(totals):.2f}x",
                     _s(_med(second)), _s(_med(base)), NA if extra != extra else f"{extra:.2f}x"])
    notes = [f"{n} lanes at {p['rate']:g} MiB/s, {p['resume_mib']} MiB. The first run is killed with "
             f"SIGKILL halfway through the time an uninterrupted run takes; the same command is "
             f"then run again. 'Killed at' is how much of the file the server had sent by then. "
             f"'Bytes served' counts both runs over the file size: 1.00x would be a resume that "
             f"re-fetches nothing, and 1.00x plus the killed-at share is a restart from scratch. "
             f"A resume cannot be perfect: whatever was in flight when the process died (up to "
             f"lanes x piece size) was never written down. Median of {p['reps']}."]
    return [Table("Killed halfway, then run again",
                  ["system", "killed at", "bytes served, both runs", "second run s",
                   "uninterrupted s", "second run vs uninterrupted"], rows, notes)]


# ----------------------------------------------------------------------------- listing


def _rvtree():
    try:
        import rvtree  # noqa: F401
    except ModuleNotFoundError:
        sys.path.insert(0, os.path.join(runners.ROOT, "remote_viewer"))
    from rvtree import archive
    from rvtree.transport import PooledTransport
    return archive, PooledTransport


def listing(ctx: Context) -> list:
    p = ctx.p
    archive, PooledTransport = _rvtree()
    stubs = [dict(rtt=0.15, jitter=1.5, connect_delay=0.1, seed=i) for i in range(4)]
    results = {}
    with lab.Lab(tar_members=p["tar_members"], stubs=stubs, hash=False) as lb:
        url = lb.url("archive.tar")
        for label, hedge in (("off", 1), ("on", 3)):
            first, many, nbytes, nreq = [], [], [], []
            for _ in range(p["list_reps"]):
                lb.reset()
                tr = PooledTransport(endpoints=lb.endpoints, circuits_per_endpoint=1, hedge=hedge,
                                     head_race=hedge, timeout=(30, 60), retries=3)
                try:
                    t0 = time.perf_counter()
                    arc = archive.open_archive(tr, url)
                    entries = archive.list_archive(arc)
                    next(entries)
                    first.append(time.perf_counter() - t0)
                    for _i, _e in zip(range(p["list_entries"] - 1), entries):
                        pass
                    many.append(time.perf_counter() - t0)
                finally:
                    tr.close()
                st = lb.stats()
                nbytes.append(st["bytes_sent"])
                nreq.append(st["requests"])
            results[label] = (first, many, nbytes, nreq)

    def q(values, frac):
        v = sorted(values)
        return v[min(len(v) - 1, int(frac * len(v)))]

    rows = []
    for label in ("off", "on"):
        first, many, nbytes, nreq = results[label]
        rows.append([f"hedging {label}", f"{_med(first):.2f}", f"{q(first, 0.9):.2f}",
                     f"{_med(many):.2f}", f"{_med(nreq):.0f}", f"{_med(nbytes) / 1024:.0f}"])
    off_b, on_b = _med(results["off"][2]), _med(results["on"][2])
    off_r, on_r = _med(results["off"][3]), _med(results["on"][3])
    notes = [f"A tar of {p['tar_members']:,} members, 4 stubs with a mean round trip of 0.15 s and a "
             f"long tail (mean extra 0.225 s). Hedging sends a small read to several daemons and "
             f"keeps the first answer. Cost of it here: {100 * (on_b / off_b - 1):.0f}% more bytes "
             f"and {100 * (on_r / off_r - 1):.0f}% more requests for the first "
             f"{p['list_entries']} entries. Median of {p['list_reps']} fresh runs."]
    return [Table("Time to the first listing entry, and what hedging costs",
                  ["", "first entry s (median)", "first entry s (p90)",
                   f"first {p['list_entries']} entries s", "requests", "KiB served"], rows, notes)]


SUITES = {"scaling": scaling, "slow": slow_lane, "flaky": flaky, "resume": resume,
          "listing": listing}


def environment(ctx: Context) -> dict:
    import platform
    import subprocess
    import requests
    commit = subprocess.run(["git", "-C", runners.ROOT, "rev-parse", "--short", "HEAD"],
                            capture_output=True, text=True).stdout.strip() or "unknown"
    try:
        import socks
        pysocks = getattr(socks, "__version__", "installed")
    except ImportError:
        pysocks = "missing"
    return {"date": time.strftime("%Y-%m-%d", time.gmtime()), "commit": commit,
            "python": platform.python_version(), "platform": f"{platform.system()} {platform.machine()}",
            "cpus": os.cpu_count(), "requests": requests.__version__, "pysocks": pysocks,
            "baseline": ctx.baseline if ctx.legacy else f"{ctx.baseline} (not in this clone)",
            "aria2c": ctx.aria2c or "not installed"}
