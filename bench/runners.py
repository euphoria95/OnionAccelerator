"""The systems under test, each run as a subprocess against the lab, and checked.

A run counts only if the file it wrote has the right SHA-256. A run that does not finish, or
finishes with the wrong bytes, is reported as a failure and its time is not used: a fast wrong
answer is not a result.

  engine   this checkout's `partial_download_mode`, the piece-based engine
  legacy   the same function at an earlier commit (default 6d8a1d8: one chunk per proxy,
           merged afterwards), extracted with `git archive` into a scratch directory so the
           two never share a module
  aria2c   if it is on PATH. aria2c cannot speak SOCKS5, so each lane is *also* exposed as a
           shaped HTTP mirror (same rate, latency, failures) and aria2c is given every mirror
           as a source for one file: the same capacity, reached the way aria2c can reach it

Subprocesses, not threads or imports: the legacy code and this checkout both call themselves
OnionAccelerator, and a benchmark should not have the timer in the same interpreter as the
thing it is timing.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_BASELINE = "6d8a1d8"

_ENGINE = r"""
import json, sys, time
sys.path.insert(0, {root!r})
import OnionAccelerator as oa
t0 = time.perf_counter()
kwargs = {kwargs!r}
res = oa.partial_download_mode([{url!r}], {endpoints!r}, retries={retries}, **kwargs)
print(json.dumps({{"seconds": time.perf_counter() - t0,
                   "ok": [bool(getattr(r, "ok", r)) for r in (res or [])]}}))
"""

_LEGACY = r"""
import json, sys, time
sys.path.insert(0, {legacy!r})
import OnionAccelerator as oa
t0 = time.perf_counter()
oa.partial_download_mode([{url!r}], {endpoints!r}, retries={retries})
print(json.dumps({{"seconds": time.perf_counter() - t0, "ok": []}}))
"""


@dataclasses.dataclass
class Run:
    system: str
    ok: bool
    seconds: float
    note: str = ""
    wall: float = 0.0          # spawn to exit, interpreter start-up included; `seconds` excludes it


def aria2c_path() -> Optional[str]:
    return shutil.which("aria2c")


def prepare_baseline(ref: str = DEFAULT_BASELINE) -> Optional[str]:
    """The pre-engine code, in a directory of its own; None if this clone lacks the commit."""
    exists = subprocess.run(["git", "-C", ROOT, "cat-file", "-e", f"{ref}^{{commit}}"],
                            capture_output=True)
    if exists.returncode != 0:
        return None
    dest = tempfile.mkdtemp(prefix="bench-legacy-")
    archive = subprocess.run(
        ["git", "-C", ROOT, "archive", ref, "OnionAccelerator.py", "fullhelp.py", "UserAgents.tsv"],
        capture_output=True, check=True)
    subprocess.run(["tar", "-x", "-C", dest], input=archive.stdout, check=True)
    return dest


def _output_path(workdir: str, lab, subdir: str = "partials") -> str:
    host = f"bench.lab:{lab.info['content_port']}".replace(":", "_")
    return os.path.join(workdir, subdir, host, "file.bin")


def _sha256(path: str) -> Optional[str]:
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                digest.update(block)
        return digest.hexdigest()
    except OSError:
        return None


def _child_env() -> dict:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["ONIONACCELERATOR_INDEX"] = os.path.join(tempfile.gettempdir(), "bench-unused-index.db")
    return env


def _spawn(script: str, workdir: str) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", script], cwd=workdir, env=_child_env(),
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)


def _finish(proc: subprocess.Popen, timeout: float):
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return None
    for line in reversed(out.splitlines()):
        try:
            return json.loads(line)
        except ValueError:
            continue
    return None


def start_engine(lab, workdir: str, *, piece_size: int = 4 << 20, circuits: int = 1,
                 retries: int = 3, fresh: bool = False) -> subprocess.Popen:
    kwargs = {"piece_size": piece_size, "circuits_per_endpoint": circuits, "fresh": fresh}
    return _spawn(_ENGINE.format(root=ROOT, url=lab.url(), endpoints=lab.endpoints,
                                 retries=retries, kwargs=kwargs), workdir)


def start_legacy(lab, workdir: str, legacy_dir: str, *, retries: int = 3) -> subprocess.Popen:
    return _spawn(_LEGACY.format(legacy=legacy_dir, url=lab.url(), endpoints=lab.endpoints,
                                 retries=retries), workdir)


def verify(lab, workdir: str, system: str, result: Optional[dict], expect_ok: bool = True,
           subdir: str = "partials") -> Run:
    if result is None:
        return Run(system, False, 0.0, "did not finish")
    got = _sha256(_output_path(workdir, lab, subdir))
    if got != lab.info["sha256"]:
        return Run(system, False, result["seconds"], "wrong or missing file" if got is None
                   else "wrong bytes")
    return Run(system, True, result["seconds"])


def _timed_run(lab, system, workdir, start, timeout) -> Run:
    t0 = time.perf_counter()
    proc = start()
    run = verify(lab, workdir, system, _finish(proc, timeout))
    run.wall = time.perf_counter() - t0
    return run


def run_engine(lab, *, timeout: float = 600, **kwargs) -> Run:
    workdir = tempfile.mkdtemp(prefix="bench-engine-")
    try:
        return _timed_run(lab, "engine", workdir,
                          lambda: start_engine(lab, workdir, **kwargs), timeout)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def run_legacy(lab, legacy_dir: str, *, timeout: float = 600, **kwargs) -> Run:
    workdir = tempfile.mkdtemp(prefix="bench-legacy-run-")
    try:
        return _timed_run(lab, "legacy", workdir,
                          lambda: start_legacy(lab, workdir, legacy_dir, **kwargs), timeout)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def aria2c_argv(binary: str, urls: list, directory: str, piece_mib: int = 4) -> list:
    """aria2c over mirrors: one connection per mirror, split into 4 MiB pieces."""
    return [binary, "-q", "-d", directory, "-o", "file.bin", f"-s{len(urls)}", "-x1",
            f"-k{piece_mib}M", "--file-allocation=none", "--allow-overwrite=true",
            "--auto-file-renaming=false", "--max-tries=3", "--retry-wait=0",
            "--check-certificate=false", *urls]


def run_aria2c(lab, mirror_urls: list, *, timeout: float = 600) -> Optional[Run]:
    binary = aria2c_path()
    if binary is None:
        return None
    workdir = tempfile.mkdtemp(prefix="bench-aria2c-")
    try:
        t0 = time.perf_counter()
        proc = subprocess.run(aria2c_argv(binary, mirror_urls, workdir), capture_output=True,
                              timeout=timeout)
        seconds = time.perf_counter() - t0
        got = _sha256(os.path.join(workdir, "file.bin"))
        if proc.returncode != 0 or got != lab.info["sha256"]:
            return Run("aria2c", False, seconds, f"exit {proc.returncode}" if proc.returncode
                       else "wrong bytes")
        return Run("aria2c", True, seconds)
    except subprocess.TimeoutExpired:
        return Run("aria2c", False, timeout, "did not finish")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def kill_after(lab, proc: subprocess.Popen, seconds: float, spawned: float) -> float:
    """SIGKILL `proc` `seconds` after it was spawned. Returns the fraction of the file the
    server had sent by then.

    By time, not by bytes served: pieces commit in waves, so "half the bytes have been sent"
    can be the instant before the first wave lands, when nothing is yet written down and a
    kill loses everything in flight. A fraction of the *uninterrupted* time is the same
    question for every system, and lands wherever each one's progress actually is.
    """
    size = lab.info["sizes"]["/file.bin"]
    remaining = spawned + seconds - time.perf_counter()
    if remaining > 0:
        time.sleep(remaining)
    served = lab.stats()["bytes_sent"] / size
    if proc.poll() is None:
        proc.send_signal(signal.SIGKILL)
    proc.wait()
    return served
