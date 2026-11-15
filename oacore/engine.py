"""The download engine: pieces, a journal beside the file, and lanes that steal work.

One engine behind `--mode multi`, `--mode partial` and `crawl --download`. It replaces a
design with four properties that each lost work:

  * one chunk per proxy, fixed up front, so the slowest proxy set the finish time;
  * one failed chunk deleted every part, so a 40 GB transfer died with its last retry;
  * each chunk was read whole into memory and merged by `cf.read()`, so memory was the
    file size;
  * nothing identified the version of the file being fetched, so a source that changed
    mid-transfer produced a file spliced from two versions without a word.

What replaces it:

  Pieces      Every sized file is cut into fixed pieces (default 4 MiB). Lanes pull the
              next piece from a shared queue, so a fast lane simply does more of them.
  One file    `<name>.oa-part` is preallocated and each piece is written at its offset.
              There is no merge pass, and a lane holds at most one piece in memory.
  Journal     `<name>.oa-state` is append-only JSON lines: a job header, then one record
              per finished piece (SHA-256, lane, attempts). A line is one write(), so a
              SIGKILL can tear at most the last line, which the reader drops.
  Resume      A rerun finds the journal, re-hashes the pieces it claims are finished, and
              fetches only the rest. `fresh` discards it.
  Validators  The size probe's ETag / Last-Modified go out as `If-Range` on every piece.
              A 200 to a ranged request, a changed validator on a 206, or a changed total
              in Content-Range marks the transfer stale: it restarts once, then fails.
  Slow lanes  A piece running far below the median lane for a sustained window is
              cancelled, requeued, and its lane rotated onto a new circuit.
  Endgame     The last few pieces are raced on two lanes and the first to finish wins.
  Partial     A piece that exhausts its retries stops the job but deletes nothing: the
              file stays resumable.

The lanes are rvtree's: `LanePool` already has credential-isolated circuits per lane,
interleaving across daemons, a failover contract (`acquire(exclude=...)` relaxes to a
stale lane rather than giving up) and proof-of-work gate handling, and it stays on
`requests`. The async pool in crawler/ would have let `--download` run inside a crawl,
but rewrites more and shares none of that. rvtree is imported lazily so that importing
this module, like the rest of oacore, cannot fail on a host that lacks it.

A file with no discoverable size, or one that fits in a single piece, is fetched as one
sequential GET with nothing to resume: there is no journal, and a failure leaves nothing
behind, as before.
"""

from __future__ import annotations

import collections
import dataclasses
import hashlib
import json
import os
import random
import statistics
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any, Callable, Optional, Sequence

from .common import _check_response, explain_error, logger
from .config import DEFAULT_RETRIES, TRANSFER_TIMEOUT
from .sizeprobe import Validators, probe_remote_size_ex

# The stream contract: kinds this module publishes, each with `data` equal to a record it
# wrote to disk -- the journal line for start/piece/fail (while the transfer runs), the
# custody file for done. A sequential transfer has no journal, so its start and fail
# events are the exceptions: they describe a transfer that left nothing resumable.
EVENT_DOWNLOAD_START = "download.start"
EVENT_DOWNLOAD_PIECE = "download.piece"
EVENT_DOWNLOAD_DONE = "download.done"
EVENT_DOWNLOAD_FAIL = "download.fail"

DEFAULT_PIECE_SIZE = 4 * 1024 * 1024
# Read granularity. Small enough that a cancelled attempt notices within a fraction of a
# second even on a lane doing 20 KB/s, and large enough that a fast lane is not paying
# per-call overhead for it.
READ_SIZE = 16 * 1024
HASH_BLOCK = 1024 * 1024


def plan_pieces(size: int, piece_size: int) -> list[tuple[int, int]]:
    """Inclusive (start, end) byte ranges covering [0, size) once; the last may be short."""
    return [(start, min(start + piece_size, size) - 1) for start in range(0, size, piece_size)]


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclasses.dataclass
class EngineConfig:
    piece_size: int = DEFAULT_PIECE_SIZE
    retries: int = DEFAULT_RETRIES          # attempts per piece before the job gives up
    timeout: tuple = TRANSFER_TIMEOUT       # (connect, read)
    fresh: bool = False                     # discard any journal and start over
    endgame_pieces: int = 3                 # race the last N pieces on two lanes; 0 = never
    slow_window: float = 20.0               # seconds a piece must be slow before it is cancelled
    slow_ratio: float = 0.1                 # "far below": under this fraction of the median; 0 = off
    slow_min_lanes: int = 4                 # lanes that must have a rate before there is a median
    slow_max_cancels: int = 2               # per piece, so a uniformly bad path cannot thrash
    drain_grace: float = 10.0               # after a piece gives up, seconds in-flight ones may finish
    verify: Any = None                      # None: rvtree's default (off; the onion authenticates)
    file_mode: int = 0o644


@dataclasses.dataclass
class DownloadResult:
    url: str
    path: str
    ok: bool = False
    reason: Optional[str] = None
    size: Optional[int] = None
    sha256: Optional[str] = None
    pieces_total: int = 0
    pieces_done: int = 0
    pieces_resumed: int = 0
    pieces_by_lane: dict = dataclasses.field(default_factory=dict)
    restarts: int = 0
    rotations: int = 0
    slow_cancelled: int = 0
    raced: int = 0
    custody: Optional[dict] = None


class _Stale(Exception):
    """The source is not the one this transfer started with."""

    def __init__(self, kind: str, detail: str):
        super().__init__(detail)
        self.kind, self.detail = kind, detail


class _Cancelled(Exception):
    pass


_RV = None
_RV_LOCK = threading.Lock()


def _rv() -> SimpleNamespace:
    """rvtree's lane machinery, imported on first use and not before.

    An installed rvtree wins; an uninstalled checkout falls back to remote_viewer/ beside
    this package, the same order OnionAccelerator._import_rvtree uses.
    """
    global _RV
    with _RV_LOCK:
        if _RV is None:
            try:
                import rvtree  # noqa: F401
            except ModuleNotFoundError as exc:
                here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                pkg_dir = os.path.join(here, "remote_viewer")
                if exc.name != "rvtree" or not os.path.isdir(os.path.join(pkg_dir, "rvtree")):
                    raise
                if pkg_dir not in sys.path:
                    sys.path.insert(0, pkg_dir)
            from rvtree.transport import gate, pooled, tor
            _RV = SimpleNamespace(
                gate=gate, Endpoint=pooled.Endpoint, Lane=pooled.Lane, LanePool=pooled.LanePool,
                Circuit=pooled._IsolatedCircuit, parse_endpoint=pooled.parse_endpoint,
                TransportError=tor.TransportError, DEFAULT_VERIFY=tor.DEFAULT_VERIFY,
                validate_content_range=tor._validate_content_range)
        return _RV


# ----------------------------------------------------------------------- journal


def read_journal(path: str) -> list[dict]:
    """Every intact record in `path`; an unreadable line (a torn write) is dropped."""
    records = []
    try:
        with open(path, "rb") as fh:
            for raw in fh.read().splitlines():
                try:
                    record = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    records.append(record)
    except OSError:
        pass
    return records


class _Journal:
    def __init__(self, path: str, mode: int):
        self.path, self._mode, self._fh = path, mode, None

    def rewrite(self, records: Sequence[dict]) -> None:
        """Replace the journal atomically, then keep it open for appending."""
        self.close()
        tmp = self.path + ".tmp"
        with open(tmp, "wb") as fh:
            for record in records:
                fh.write(self._line(record))
        os.chmod(tmp, self._mode)
        os.replace(tmp, self.path)
        self._fh = open(self.path, "ab", buffering=0)

    @staticmethod
    def _line(record: dict) -> bytes:
        return (json.dumps(record, separators=(",", ":")) + "\n").encode()

    def append(self, record: dict) -> None:
        # Unbuffered and one write(): the record is in the kernel's hands before anyone
        # is told about it, and a SIGKILL cannot leave half of it behind a buffer.
        self._fh.write(self._line(record))

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


def _hash_range(fh, start: int, end: int) -> str:
    digest = hashlib.sha256()
    fh.seek(start)
    left = end - start + 1
    while left > 0:
        block = fh.read(min(HASH_BLOCK, left))
        if not block:
            break
        digest.update(block)
        left -= len(block)
    return digest.hexdigest() if left == 0 else ""


# ----------------------------------------------------------------------- pieces


class _Piece:
    __slots__ = ("i", "start", "end", "state", "failures", "fetches", "cancels", "owners",
                 "reserved", "last_endpoint", "sha", "lane")

    def __init__(self, i: int, start: int, end: int):
        self.i, self.start, self.end = i, start, end
        self.state = "pending"              # pending -> active -> done
        self.failures = self.fetches = self.cancels = 0
        self.owners: list[_Attempt] = []    # attempts running right now
        self.reserved = 0                   # workers that picked it and are waiting for a lane
        self.last_endpoint = None
        self.sha = None
        self.lane = None


class _Attempt:
    __slots__ = ("piece", "lane", "raced", "started", "abort", "bytes", "samples", "cancel",
                 "result", "cv")

    def __init__(self, piece: _Piece, lane, raced: bool, result, cv):
        self.piece, self.lane, self.raced = piece, lane, raced
        self.result, self.cv = result, cv
        self.started = time.monotonic()
        self.abort = threading.Event()
        self.bytes = 0
        self.samples = collections.deque([(self.started, 0)])
        self.cancel: Optional[str] = None   # "slow" | "lost" | "halt"


# ----------------------------------------------------------------------- the downloader


class Downloader:
    """A set of lanes and the machinery to move files over them.

    `endpoints` are 'host:port' SOCKS5 specs, as everywhere else in this program; `None`
    is a direct connection, which only the engine's own tests use. Each endpoint gets
    `circuits_per_endpoint` lanes, each with its own SOCKS credential (so its own Tor
    circuit) and its own User-Agent, pinned for the lane's whole life: one lane is one
    identity as far as the far end can tell, and changing the header between attempts
    is a fingerprint, not a defence.
    """

    def __init__(self, endpoints: Sequence[Optional[str]], *, circuits_per_endpoint: int = 1,
                 user_agents: Optional[Sequence[str]] = None, config: Optional[EngineConfig] = None,
                 sink: Optional[Callable[[str, dict], Any]] = None, job_id: Optional[str] = None,
                 progress: Optional[Callable[[int], Any]] = None):
        rv = _rv()
        self.cfg = config or EngineConfig()
        self.sink = sink
        self.job_id = job_id
        self.progress = progress
        verify = rv.DEFAULT_VERIFY if self.cfg.verify is None else self.cfg.verify

        specs = list(endpoints) or [None]
        per = max(1, circuits_per_endpoint)
        n_lanes = len(specs) * per
        pool = list(user_agents) if user_agents else ["Mozilla/5.0"]
        # Distinct where the list allows it: two lanes that look alike are one fingerprint.
        agents = random.sample(pool, n_lanes) if len(pool) >= n_lanes else [
            pool[i % len(pool)] for i in range(n_lanes)]
        self.lane_user_agents: list[str] = agents

        endpoint_objs = []
        for i, spec in enumerate(specs):
            proxy_url = rv.parse_endpoint(spec)
            endpoint_objs.append(rv.Endpoint(address=proxy_url or f"direct#{i}",
                                             proxy_url=proxy_url, user_agent=agents[i]))
        lanes = []
        for c in range(per):
            for endpoint in endpoint_objs:
                index = len(lanes)
                lanes.append(rv.Lane(index=index, endpoint=endpoint, circuit=rv.Circuit(
                    index, endpoint.proxy_url, agents[index], self.cfg.timeout, verify)))
        self.lanes = lanes
        self.pool = rv.LanePool(lanes)

        self._lock = threading.Lock()
        # Threads are cheap to wait with and dear to multiply: a job asks for as many
        # workers as it has pieces to run, and the lanes arbitrate between them, but the
        # total is capped so forty big files in flight cannot mean a thousand threads.
        self._worker_budget = max(8, 3 * len(lanes))
        self._workers_out = 0
        self._attempts: set[_Attempt] = set()
        self._rates: dict[int, float] = {}
        self._closing = threading.Event()
        self._monitor_thread: Optional[threading.Thread] = None

    # -- public ---------------------------------------------------------------
    def download(self, url: str, out_path: str) -> DownloadResult:
        self._start_monitor()
        return _Job(self, url, out_path).run()

    def download_many(self, items: Sequence[tuple[str, str]], file_workers: int) -> list[DownloadResult]:
        """Files in parallel over the shared lanes; `file_workers` of them in flight at once."""
        self._start_monitor()
        file_workers = max(1, min(file_workers, len(items)))
        with ThreadPoolExecutor(max_workers=file_workers, thread_name_prefix="oa-file") as ex:
            futures = [ex.submit(_Job(self, url, out).run) for url, out in items]
            return [f.result() for f in futures]

    def close(self) -> None:
        self._closing.set()
        for lane in self.lanes:
            try:
                lane.circuit.session.close()
            except Exception:  # noqa: BLE001 - closing is best effort
                pass

    # -- plumbing ---------------------------------------------------------------
    def emit(self, kind: str, data: dict) -> None:
        """Publish to the stream sink. A sink must not break a download."""
        if self.sink is None:
            return
        try:
            self.sink(kind, data)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"stream sink raised on {kind}: {exc!r}")

    def note_progress(self, n: int) -> None:
        if self.progress is not None and n:
            self.progress(n)

    def probe(self, url: str):
        """(SizeProbe, Validators) for `url`, asked of whichever lane is free.

        Sent through that lane's own credentialed proxy, so the probe rides the circuit
        and wears the User-Agent the pieces will: a probe on the daemon's shared circuit
        would be a second, anonymous identity asking about the same file.
        """
        lane = self.pool.acquire()
        try:
            proxy = lane.circuit.session.proxies.get("http")
            spec = proxy.split("://", 1)[1] if proxy else None
            return probe_remote_size_ex(url, spec, self.lane_user_agents[lane.index])
        finally:
            self.pool.release(lane)

    def reserve_workers(self, want: int) -> int:
        """Workers a job may start: what it wants, within the budget, and never fewer than one."""
        with self._lock:
            granted = max(1, min(want, self._worker_budget - self._workers_out))
            self._workers_out += granted
            return granted

    def release_workers(self, n: int) -> None:
        with self._lock:
            self._workers_out -= n

    def register(self, attempt: _Attempt) -> None:
        with self._lock:
            self._attempts.add(attempt)

    def unregister(self, attempt: _Attempt) -> None:
        with self._lock:
            self._attempts.discard(attempt)

    def note_rate(self, lane_index: int, rate: float) -> None:
        with self._lock:
            self._rates[lane_index] = rate

    # -- the slow-lane watchdog ----------------------------------------------------
    def _start_monitor(self) -> None:
        if self.cfg.slow_ratio <= 0 or self._monitor_thread is not None:
            return
        with self._lock:
            if self._monitor_thread is None:
                self._monitor_thread = threading.Thread(target=self._monitor, daemon=True,
                                                        name="oa-slow-lanes")
                self._monitor_thread.start()

    def _monitor(self) -> None:
        """Cancel an attempt that has run far below the median lane for a whole window.

        The median is over each lane's most recent finished piece, so a farm that is slow
        everywhere (a congested network, a throttled target) has a low median too and
        cancels nothing: the rule is "worse than your peers", not "slow".
        """
        cfg = self.cfg
        tick = max(0.05, min(0.5, cfg.slow_window / 4))
        while not self._closing.wait(tick):
            now = time.monotonic()
            with self._lock:
                attempts = list(self._attempts)
                rates = list(self._rates.values())
            for a in attempts:
                a.samples.append((now, a.bytes))
                while len(a.samples) > 2 and a.samples[1][0] <= now - cfg.slow_window:
                    a.samples.popleft()
            if len(rates) < cfg.slow_min_lanes:
                continue
            median = statistics.median(rates)
            if median <= 0:
                continue
            for a in attempts:
                if a.abort.is_set() or a.piece.cancels >= cfg.slow_max_cancels:
                    continue
                t0, b0 = a.samples[0]
                if now - a.started < cfg.slow_window or now - t0 < cfg.slow_window * 0.9:
                    continue
                if (a.bytes - b0) / (now - t0) < median * cfg.slow_ratio:
                    # Counted here, at the decision: the attempt only notices at its next
                    # read, which on a lane this slow can be after the job is over.
                    with a.cv:
                        if a.abort.is_set():
                            continue
                        a.cancel = "slow"
                        a.abort.set()
                        a.piece.cancels += 1
                        a.result.slow_cancelled += 1


# ----------------------------------------------------------------------- one file


class _Job:
    def __init__(self, dl: Downloader, url: str, out_path: str):
        self.dl, self.cfg, self.url, self.out = dl, dl.cfg, url, out_path
        self.part = out_path + ".oa-part"
        self.journal_path = out_path + ".oa-state"
        self.custody_path = out_path + ".oa.json"
        self.result = DownloadResult(url=url, path=out_path)
        self.started_at = _now()

    # -- driver ---------------------------------------------------------------
    def run(self) -> DownloadResult:
        os.makedirs(os.path.dirname(self.out) or ".", exist_ok=True)
        restarts, sequential_only = 0, False
        try:
            while True:
                try:
                    return self._sequential(None, Validators()) if sequential_only \
                        else self._sized()
                except _Stale as stale:
                    self._discard()
                    if restarts >= 1:
                        if stale.kind == "status200":
                            # Same validators, still a 200: this is a server without
                            # ranges, not a file that changed. Fetch it whole.
                            sequential_only = True
                            logger.warning(f"{self.url}: the server does not honour ranges; "
                                           f"fetching it in one piece.")
                            continue
                        return self._fail(f"the source changed again while downloading "
                                          f"({stale.detail})", persist=False)
                    restarts += 1
                    self.result.restarts = restarts
                    logger.warning(f"{self.url}: the source changed under the transfer "
                                   f"({stale.detail}); starting the file over once.")
        except Exception as exc:  # noqa: BLE001 - one bad file must not end the run
            logger.error(f"[ERR] {self.url}: {explain_error(exc)}")
            return self._fail(explain_error(exc), persist=os.path.exists(self.journal_path))

    def _discard(self) -> None:
        for path in (self.journal_path, self.journal_path + ".tmp", self.part):
            try:
                os.remove(path)
            except OSError:
                pass

    def _fail(self, reason: str, *, persist: bool, piece: Optional[int] = None,
              journal: Optional[_Journal] = None) -> DownloadResult:
        r = self.result
        r.ok, r.reason = False, reason
        record = {"kind": "fail", "job_id": self.dl.job_id, "url": self.url, "reason": reason,
                  "piece": piece, "pieces_done": r.pieces_done, "pieces_total": r.pieces_total,
                  "at": _now()}
        if persist:
            try:
                if journal is not None and journal._fh is not None:
                    journal.append(record)
                else:
                    with open(self.journal_path, "ab") as fh:
                        fh.write(_Journal._line(record))
            except OSError:
                pass
        self.dl.emit(EVENT_DOWNLOAD_FAIL, record)
        logger.error(f"[FAIL] {self.url}: {reason}")
        return r

    # -- sized: pieces, journal, lanes -----------------------------------------
    def _sized(self) -> DownloadResult:
        probe, validators = self.dl.probe(self.url)
        size = probe.size
        if size is None:
            logger.warning(f"Could not determine the size of {self.url}; fetching it in one "
                           f"piece.")
            return self._sequential(None, validators)
        plan = plan_pieces(size, self.cfg.piece_size)
        if len(plan) == 1:
            return self._sequential(size, validators)

        done, started_at = self._load(size, len(plan), validators)
        resumed = done is not None
        if done is None:
            self._discard()
            done, started_at = {}, self.started_at
        header = {"kind": "job", "job_id": self.dl.job_id, "url": self.url, "path": self.out,
                  "size": size, "piece_size": self.cfg.piece_size, "pieces": len(plan),
                  "etag": validators.etag, "last_modified": validators.last_modified,
                  "started_at": started_at, "resumed_pieces": len(done)}
        journal = _Journal(self.journal_path, self.cfg.file_mode)
        journal.rewrite([header] + [done[i] for i in sorted(done)])

        if resumed:
            fh = open(self.part, "r+b", buffering=0)
        else:
            fd = os.open(self.part, os.O_RDWR | os.O_CREAT | os.O_TRUNC, self.cfg.file_mode)
            fh = os.fdopen(fd, "r+b", buffering=0)
            fh.truncate(size)
        r = self.result
        r.size, r.pieces_total, r.pieces_resumed = size, len(plan), len(done)
        r.pieces_done = len(done)
        self.dl.emit(EVENT_DOWNLOAD_START, header)
        logger.info(f"[ENGINE] {self.url} => {size} bytes in {len(plan)} piece(s) of "
                    f"{self.cfg.piece_size}"
                    + (f", {len(done)} already finished" if done else ""))
        self.dl.note_progress(sum(plan[i][1] - plan[i][0] + 1 for i in done))

        transfer = _Transfer(self, plan, done, fh, journal, validators, size)
        try:
            outcome = transfer.run()
        finally:
            r.pieces_done = transfer.done_count
            r.pieces_by_lane = dict(transfer.lane_pieces)
        if outcome == "stale":
            fh.close()
            journal.close()
            raise transfer.stale
        if outcome == "failed":
            result = self._fail(transfer.failure[0], persist=True, piece=transfer.failure[1],
                                journal=journal)
            fh.close()
            journal.close()
            return result
        return self._finish(fh, journal, size, validators, started_at, transfer)

    def _load(self, size: int, n_pieces: int, validators: Validators):
        """Pieces a previous run finished and that still hash right, or None to start over."""
        if self.cfg.fresh:
            return None, None
        records = read_journal(self.journal_path)
        header = next((rec for rec in records if rec.get("kind") == "job"), None)
        if (header is None or header.get("url") != self.url or header.get("size") != size
                or header.get("piece_size") != self.cfg.piece_size
                or not os.path.isfile(self.part) or os.path.getsize(self.part) != size):
            return None, None
        if header.get("etag") and validators.etag and header["etag"] != validators.etag:
            logger.info(f"{self.url}: the ETag changed since the last run; starting over.")
            return None, None
        if (not header.get("etag") and header.get("last_modified") and validators.last_modified
                and header["last_modified"] != validators.last_modified):
            logger.info(f"{self.url}: Last-Modified changed since the last run; starting over.")
            return None, None
        plan = plan_pieces(size, self.cfg.piece_size)
        claimed = {}
        for rec in records:
            if rec.get("kind") == "piece" and rec.get("status") == "done":
                i = rec.get("i")
                if isinstance(i, int) and 0 <= i < n_pieces:
                    claimed[i] = rec
        verified = {}
        with open(self.part, "rb") as fh:
            for i in sorted(claimed):
                if _hash_range(fh, *plan[i]) == claimed[i].get("sha256"):
                    verified[i] = claimed[i]
        if len(verified) != len(claimed):
            logger.warning(f"{self.url}: {len(claimed) - len(verified)} finished piece(s) no "
                           f"longer match their hash; fetching them again.")
        return verified, header.get("started_at") or self.started_at

    def _finish(self, fh, journal, size, validators, started_at, transfer) -> DownloadResult:
        # The whole-file hash is a separate pass on purpose: the pieces were hashed as
        # they arrived, but this is the one that reads what is actually on disk.
        digest = hashlib.sha256()
        fh.seek(0)
        for block in iter(lambda: fh.read(HASH_BLOCK), b""):
            digest.update(block)
        os.fsync(fh.fileno())
        fh.close()
        lanes = [{"lane": index, "endpoint": self.dl.lanes[index].endpoint.address,
                  "pieces": count} for index, count in sorted(transfer.lane_pieces.items())]
        record = self._custody(size, digest.hexdigest(), validators, started_at, lanes,
                               len(transfer.pieces), self.cfg.piece_size)
        self._write_custody(record)
        journal.close()
        os.replace(self.part, self.out)
        try:
            os.remove(self.journal_path)
        except OSError:
            pass
        r = self.result
        r.ok, r.sha256, r.custody = True, record["sha256"], record
        logger.info(f"[OK] {self.url} -> {self.out} ({size} bytes, {len(transfer.pieces)} pieces)")
        self.dl.emit(EVENT_DOWNLOAD_DONE, record)
        return r

    def _custody(self, size, sha, validators, started_at, lanes, pieces, piece_size) -> dict:
        r = self.result
        return {"kind": "custody", "job_id": self.dl.job_id, "url": self.url, "path": self.out,
                "size": size, "sha256": sha, "etag": validators.etag,
                "last_modified": validators.last_modified, "piece_size": piece_size,
                "pieces": pieces, "resumed_pieces": r.pieces_resumed, "restarts": r.restarts,
                "lanes": lanes, "started_at": started_at, "finished_at": _now()}

    def _write_custody(self, record: dict) -> None:
        tmp = self.custody_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2)
            fh.write("\n")
        os.chmod(tmp, self.cfg.file_mode)
        os.replace(tmp, self.custody_path)

    # -- sequential: no size, or one piece -----------------------------------------
    def _sequential(self, size: Optional[int], validators: Validators) -> DownloadResult:
        rv, cfg, dl, r = _rv(), self.cfg, self.dl, self.result
        r.size, r.pieces_total = size, 1
        header = {"kind": "job", "job_id": dl.job_id, "url": self.url, "path": self.out,
                  "size": size, "piece_size": None, "pieces": 1, "etag": validators.etag,
                  "last_modified": validators.last_modified, "started_at": self.started_at,
                  "resumed_pieces": 0}
        dl.emit(EVENT_DOWNLOAD_START, header)
        last_endpoint, reason = None, "no attempt was made"
        for attempt in range(cfg.retries):
            lane = dl.pool.acquire(exclude=last_endpoint)
            tmp, got = None, 0
            try:
                response, _ = rv.gate.get(lane.circuit.session, self.url, timeout=cfg.timeout,
                                          verify=lane.circuit.verify)
                try:
                    _check_response(self.url, response)
                    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.out) or ".",
                                               prefix=".oa-", suffix=".part")
                    os.chmod(tmp, cfg.file_mode)
                    digest = hashlib.sha256()
                    with os.fdopen(fd, "wb") as out:
                        for chunk in response.iter_content(READ_SIZE):
                            out.write(chunk)
                            digest.update(chunk)
                            got += len(chunk)
                            dl.note_progress(len(chunk))
                        out.flush()
                        os.fsync(out.fileno())
                finally:
                    response.close()
                lanes = [{"lane": lane.index, "endpoint": lane.endpoint.address, "pieces": 1}]
                record = self._custody(got, digest.hexdigest(), validators, self.started_at,
                                       lanes, 1, None)
                self._write_custody(record)
                os.replace(tmp, self.out)
                tmp = None
                dl.pool.mark_alive(lane.endpoint)
                r.ok, r.size, r.sha256, r.custody = True, got, record["sha256"], record
                r.pieces_done, r.pieces_by_lane = 1, {lane.index: 1}
                logger.info(f"[OK] {self.url} -> {self.out} ({got} bytes, one piece)")
                dl.emit(EVENT_DOWNLOAD_DONE, record)
                return r
            except Exception as exc:  # noqa: BLE001 - any failure moves to another lane
                reason = explain_error(exc)
                logger.error(f"[ERR] Download failed for {self.url} on lane {lane.index}: "
                             f"{reason}")
                dl.note_progress(-got)
                if tmp is not None:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                dl.pool.mark_dead(lane.endpoint)
                lane.circuit.rotate()
                r.rotations += 1
                last_endpoint = lane.endpoint
                if attempt < cfg.retries - 1:
                    logger.warning(f"Retrying {self.url} ({attempt + 2}/{cfg.retries}) on a "
                                   f"different lane")
            finally:
                dl.pool.release(lane)
        return self._fail(reason, persist=False)


# ----------------------------------------------------------------------- one run of a sized file


class _Transfer:
    """The workers, the queue and the commit rule for one sized file.

    Everything that decides what happens to a piece -- who takes it, whether it is done,
    whether the job is over -- happens under `cv`; the network and the hashing do not.
    """

    def __init__(self, job: _Job, plan, done: dict, fh, journal: _Journal,
                 validators: Validators, size: int):
        self.job, self.dl, self.cfg, self.url, self.size = job, job.dl, job.cfg, job.url, size
        self.pieces = [_Piece(i, s, e) for i, (s, e) in enumerate(plan)]
        for i, rec in done.items():
            self.pieces[i].state, self.pieces[i].sha = "done", rec["sha256"]
        self.pending = collections.deque(i for i, p in enumerate(self.pieces) if p.state != "done")
        self.done_count = len(done)
        self.fh, self.journal = fh, journal
        self.etag, self.last_modified = validators.etag, validators.last_modified
        # A weak ETag cannot be an If-Range validator (RFC 9110 13.1.5); Last-Modified can.
        strong = self.etag if self.etag and not self.etag.startswith("W/") else None
        self.if_range = strong or self.last_modified
        if self.if_range is None:
            logger.warning(f"{self.url}: the server gave no ETag or Last-Modified, so a change "
                           f"of the file mid-transfer can only be noticed by its size.")
        self.cv = threading.Condition()
        self.halt: Optional[str] = None          # "done" | "failed" | "stale"
        self.failing = False                     # a piece gave up: take nothing new, let the rest land
        self.drain_deadline = 0.0
        self.failure: Optional[tuple] = None     # (reason, piece index)
        self.stale: Optional[_Stale] = None
        self.lane_pieces: collections.Counter = collections.Counter()
        self.live: list[_Attempt] = []

    # -- control ----------------------------------------------------------------
    def run(self) -> str:
        cfg = self.cfg
        # The extra workers are the endgame's: a worker with no piece left to take is the
        # one that races somebody else's.
        wanted = len(self.pending) + (cfg.endgame_pieces if len(self.pieces) > 1 else 0)
        workers = self.dl.reserve_workers(min(len(self.dl.lanes), wanted))
        try:
            for n in range(workers):
                threading.Thread(target=self._worker, daemon=True, name=f"oa-lane-{n}").start()
            with self.cv:
                if self.done_count == len(self.pieces):
                    self.halt = "done"
                while self.halt is None:
                    if self.failing and (not self.live or time.monotonic() >= self.drain_deadline):
                        self.halt = "failed"
                        break
                    self.cv.wait(0.2)
                for a in self.live:
                    a.cancel = a.cancel or "halt"
                    a.abort.set()
                return self.halt
        finally:
            self.dl.release_workers(workers)

    def _set_halt(self, how: str) -> None:
        if self.halt is None:
            self.halt = how
        self.cv.notify_all()

    def _begin_failure(self, reason: str, piece: Optional[int]) -> None:
        """A piece has run out of attempts. Stop taking work; let what is running land.

        Called with `cv` held. The pieces already in flight are bytes this run has paid
        for, and committing them is what makes the rerun cheap, so they get
        `drain_grace` seconds. A stalled one is abandoned when the grace runs out.
        """
        if self.failure is None:
            self.failure = (reason, piece)
        if not self.failing:
            self.failing = True
            self.drain_deadline = time.monotonic() + self.cfg.drain_grace
        self.cv.notify_all()

    def _bump(self, name: str) -> None:
        with self.cv:
            setattr(self.job.result, name, getattr(self.job.result, name) + 1)

    # -- scheduling ----------------------------------------------------------------
    def _worker(self) -> None:
        try:
            while True:
                picked = self._next()
                if picked is None:
                    return
                self._attempt(*picked)
        except BaseException as exc:  # noqa: BLE001 - a bug here must stop the job, not hang it
            with self.cv:
                if self.failure is None:
                    self.failure = (f"internal error: {exc!r}", None)
                self._set_halt("failed")      # a bug is not worth waiting for

    def _next(self):
        """The next (piece, lane, raced) for a worker, or None when the job is over."""
        pool = self.dl.pool
        while True:
            with self.cv:
                if self.halt or self.failing:
                    return None
                if self.pending:
                    piece = self.pieces[self.pending.popleft()]
                    piece.state = "active"
                    piece.reserved += 1
                    exclude = piece.last_endpoint
                    racing = False
                else:
                    piece = self._race_candidate()
                    lane = None
                    if piece is not None:
                        lane = pool.try_acquire(exclude=piece.owners[0].lane.endpoint)
                    if piece is None or lane is None:
                        self.cv.wait(0.2)
                        continue
                    piece.reserved += 1
                    racing = True
            if racing:
                return piece, lane, True
            # Outside the lock: waiting for a lane must not stall everyone's commits.
            return piece, pool.acquire(exclude=exclude), False

    def _race_candidate(self) -> Optional[_Piece]:
        """A piece worth running twice: one of the last few, running once, on one lane."""
        cfg = self.cfg
        if cfg.endgame_pieces <= 0 or len(self.pieces) < 2:
            return None
        left = [p for p in self.pieces if p.state != "done"]
        if not left or len(left) > cfg.endgame_pieces:
            return None
        runnable = [p for p in left if len(p.owners) == 1 and p.reserved == 0]
        return min(runnable, key=lambda p: p.owners[0].started, default=None)

    # -- one attempt ---------------------------------------------------------------
    def _attempt(self, piece: _Piece, lane, raced: bool) -> None:
        dl, cfg = self.dl, self.cfg
        att = _Attempt(piece, lane, raced, self.job.result, self.cv)
        with self.cv:
            piece.reserved -= 1
            if self.halt or self.failing or piece.state == "done":
                dl.pool.release(lane)
                return
            piece.owners.append(att)
            piece.fetches += 1
            self.live.append(att)
            if raced:
                self.job.result.raced += 1
        dl.register(att)
        rotate = False
        try:
            body, digest = self._fetch(att)
        except _Cancelled:
            dl.note_progress(-att.bytes)
            if att.cancel == "slow":
                rotate = True
                logger.warning(f"piece {piece.i} on lane {lane.index} ran far below the "
                               f"median lane for {cfg.slow_window:g}s; requeued, lane rotated")
        except _Stale as stale:
            dl.note_progress(-att.bytes)
            with self.cv:
                if self.stale is None:
                    self.stale = stale
                self._set_halt("stale")
        except Exception as exc:  # noqa: BLE001 - any transport failure: another lane, new circuit
            dl.note_progress(-att.bytes)
            rotate = True
            dl.pool.mark_dead(lane.endpoint)
            reason = explain_error(exc)
            with self.cv:
                piece.failures += 1
                piece.last_endpoint = lane.endpoint
                logger.warning(f"piece {piece.i} ({piece.start}-{piece.end}) failed on lane "
                               f"{lane.index}: {reason}; attempt {piece.failures} of "
                               f"{cfg.retries}")
                if piece.failures >= cfg.retries and piece.state != "done" and self.halt is None:
                    self._begin_failure(f"piece {piece.i} (bytes {piece.start}-{piece.end}) "
                                        f"failed {piece.failures} times; last error: {reason}",
                                        piece.i)
        else:
            dl.pool.mark_alive(lane.endpoint)
            self._commit(att, body, digest)
        finally:
            dl.unregister(att)
            if rotate:
                lane.circuit.rotate()
            with self.cv:
                if rotate:
                    self.job.result.rotations += 1
                piece.owners.remove(att)
                self.live.remove(att)
                if piece.state == "active" and not piece.owners and not piece.reserved:
                    piece.state = "pending"
                    self.pending.appendleft(piece.i)
                self.cv.notify_all()
            dl.pool.release(lane)

    def _fetch(self, att: _Attempt):
        rv, piece, lane = _rv(), att.piece, att.lane
        want = piece.end - piece.start + 1
        headers = {"Range": f"bytes={piece.start}-{piece.end}"}
        if self.if_range:
            headers["If-Range"] = self.if_range
        response, _ = rv.gate.get(lane.circuit.session, self.url, headers=headers,
                                  timeout=self.cfg.timeout, verify=lane.circuit.verify)
        try:
            # An HTML error page served with 200 is an error page, not "the server ignored
            # my Range": the same check the whole-file path always had, and first, so that
            # a busy front end costs a retry rather than a restart.
            _check_response(self.url, response)
            if response.status_code == 200:
                raise self._stale_from_200(response)
            if response.status_code != 206:
                raise rv.TransportError(f"HTTP {response.status_code} for bytes "
                                        f"{piece.start}-{piece.end}")
            rv.validate_content_range(response.headers.get("Content-Range"), piece.start,
                                      piece.end)
            self._check_identity(response)
            buf = bytearray(want)
            view, got, digest = memoryview(buf), 0, hashlib.sha256()
            for chunk in response.iter_content(READ_SIZE):
                if att.abort.is_set():
                    raise _Cancelled()
                n = len(chunk)
                if got + n > want:
                    raise rv.TransportError("the server sent more bytes than the range asked for")
                view[got:got + n] = chunk
                digest.update(chunk)
                got += n
                att.bytes = got
                self.dl.note_progress(n)
            if att.abort.is_set():
                raise _Cancelled()
            if got != want:
                raise rv.TransportError(f"short range response: asked {want} bytes, got {got}")
            return buf, digest.hexdigest()
        finally:
            response.close()

    def _stale_from_200(self, response) -> _Stale:
        etag, modified = response.headers.get("ETag"), response.headers.get("Last-Modified")
        if self.etag and etag and etag != self.etag:
            return _Stale("changed", f"ETag {self.etag} is now {etag}")
        if not self.etag and self.last_modified and modified and modified != self.last_modified:
            return _Stale("changed", f"Last-Modified {self.last_modified} is now {modified}")
        return _Stale("status200", "a ranged request was answered with 200 and the whole entity")

    def _check_identity(self, response) -> None:
        """A 206 that is from a different version of the file than the one being built."""
        etag, modified = response.headers.get("ETag"), response.headers.get("Last-Modified")
        if self.etag and etag and etag != self.etag:
            raise _Stale("changed", f"ETag {self.etag} is now {etag}")
        if not self.etag and self.last_modified and modified and modified != self.last_modified:
            raise _Stale("changed", f"Last-Modified {self.last_modified} is now {modified}")
        total = (response.headers.get("Content-Range") or "").rpartition("/")[2].strip()
        if total.isdigit() and int(total) != self.size:
            raise _Stale("changed", f"size {self.size} is now {total}")

    def _commit(self, att: _Attempt, body: bytearray, digest: str) -> None:
        piece, lane = att.piece, att.lane
        elapsed = max(time.monotonic() - att.started, 1e-6)
        record = None
        with self.cv:
            if self.halt or piece.state == "done":
                self.dl.note_progress(-len(body))        # lost the race: its bytes were waste
                return
            self._write_at(piece.start, body)
            piece.state, piece.sha, piece.lane = "done", digest, lane.index
            self.done_count += 1
            self.lane_pieces[lane.index] += 1
            record = {"kind": "piece", "url": self.url, "i": piece.i, "status": "done",
                      "sha256": digest, "lane": lane.index, "attempts": piece.fetches}
            self.journal.append(record)
            for other in piece.owners:
                if other is not att:
                    other.cancel = other.cancel or "lost"
                    other.abort.set()
            if self.done_count == len(self.pieces):
                self._set_halt("done")
            self.cv.notify_all()
        self.dl.note_rate(lane.index, len(body) / elapsed)
        self.dl.emit(EVENT_DOWNLOAD_PIECE, record)

    def _write_at(self, offset: int, body: bytearray) -> None:
        self.fh.seek(offset)
        view = memoryview(body)
        while view:
            view = view[self.fh.write(view):]
