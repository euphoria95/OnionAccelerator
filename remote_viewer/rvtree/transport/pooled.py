"""Range engine over a pool of independent SOCKS5 endpoints.

``tor.Transport`` puts every circuit on one Tor daemon and hands them out from a
``LifoQueue``. Both halves of that are load-bearing problems. A LIFO stack under a
sequential caller returns the *same* circuit forever — a two-hour production listing in
``rv.log`` issued 7,540 range requests and every one of them went out ``on circuit 3``,
while three prepaid circuits sat idle. And even had they been used, one daemon means one
guard and one single-threaded crypto path.

This module takes the shape OnionAccelerator already uses for downloads: many
*independent* daemons (a ``--farm`` of ``tor@oaNN`` instances, or an external proxy list),
handed out round-robin from a shared pool that parks an endpoint on failure and retries
elsewhere. Three regimes fall out of one axis, the number of bytes asked for:

* under 64 KiB — **hedge**. Send the same range to several endpoints and keep the first
  answer. A header chain is serially dependent, so the only way to make it finish sooner
  is to make each round trip finish sooner; over Tor the latency distribution has a long
  tail, and at 4 KiB a duplicate costs nothing worth counting.
* under 1 MiB — **plain**. One lane, with failover across endpoints on retry.
* 1 MiB and over — **split**, using OnionAccelerator's own size-aware formula. An xz
  block or a solid 7z folder arrives on every lane at once.

All three are invisible to callers: ``get_range`` has the same signature and the same
guarantees it always had.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import secrets
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Optional, Sequence
from urllib.parse import urlsplit, urlunsplit

import requests

from ..util import human_bytes
from . import gate
from .tor import (
    CHUNK,
    DEFAULT_UA,
    DEFAULT_VERIFY,
    Circuit,
    RangeNotHonoured,
    Stream,
    TransportError,
    Verify,
    _check_proxy,
    _no_ranges,
    _read_head,
    _request_error,
    _silence_insecure_warnings,
    _stream_from,
    _validate_content_range,
)

log = logging.getLogger(__name__)

# OnionAccelerator's MIN_PARTIAL_CHUNK_SIZE, kept identical on purpose: the two projects
# split a byte range the same way, and a reader comparing them should find the same number.
MIN_SPLIT = 1024 * 1024

# Above this a duplicate request costs real bandwidth rather than a rounding error, so
# hedging stops. A hedged 4 KiB header is 8 KiB wasted; a hedged 4 MiB xz block is 4 MiB.
HEDGE_MAX_BYTES = 64 * 1024

# Measured against the latency distribution in rv.log (n=7538, p50 0.66 s, p90 1.54 s,
# p99 4.66 s): k=2 gives 1.58x, k=3 gives 1.81x, k=4 only 1.93x for a third more requests.
DEFAULT_HEDGE = 3
DEFAULT_HEAD_RACE = 3

# A hedge copy that has not answered in this long has already lost; capping its read
# timeout returns the lane to the pool instead of leaving it parked behind a stalled exit.
HEDGE_READ_TIMEOUT = 30

# How long a caller will wait for any lane to come free before giving up. Every holder is
# bounded by its own request timeout, so this only fires if something is genuinely wedged.
LANE_WAIT_TIMEOUT = 600

_DIRECT = "direct"

# Set while a thread is executing a fetch on this transport's behalf. ``get_range`` checks
# it so that a caller reached from inside the fetch pool takes the plain path instead of
# submitting more work to the pool it is running on. Nothing does that today; the guard is
# what keeps it from becoming a deadlock if something ever does.
_local = threading.local()


def _in_worker() -> bool:
    return getattr(_local, "in_worker", False)


def parse_endpoint(spec: Optional[str]) -> Optional[str]:
    """Turn an endpoint spec into a proxy URL, or ``None`` for a direct connection.

    ``host:port`` is the form OnionAccelerator uses throughout, and it means SOCKS5 with
    remote DNS — ``make_proxies`` there expands it to exactly this. A fully qualified URL
    is passed through, and validated by the same check that refuses ``socks5://``.
    """
    if spec is None:
        return None
    spec = spec.strip()
    if spec.lower() in ("", "none", _DIRECT):
        return None
    if "://" in spec:
        return _check_proxy(spec)
    # rpartition keeps an IPv6 literal such as [::1]:9050 intact, as OnionAccelerator does.
    host, sep, port = spec.rpartition(":")
    if not sep or not host or not port.isdigit():
        raise TransportError(
            f"unparseable endpoint {spec!r}: expected host:port, or a socks5h:// URL"
        )
    return _check_proxy(f"socks5h://{host}:{port}")


def load_user_agents(path: str) -> list[str]:
    """First column of a tab-separated file; the rest are usage weights and are ignored.

    Mirrors OnionAccelerator's loader so a single ``UserAgents.tsv`` serves both tools.
    """
    agents: list[str] = []
    if not os.path.isfile(path):
        raise TransportError(f"user-agent file not found: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            ua = line.strip().split("\t")[0].strip()
            if ua:
                agents.append(ua)
    if not agents:
        raise TransportError(f"no user-agent strings in {path}")
    return agents


def split_spans(
    start: int, end: int, lanes: int, min_split: int = MIN_SPLIT
) -> list[tuple[int, int]]:
    """Partition an inclusive range the way OnionAccelerator partitions a download.

    ``n = min(lanes, max(1, want // min_split))`` is ``partial_download_file``'s rule
    verbatim: at most one span per lane, and never split finer than ``min_split``, so a
    small read stays one request and a tiny file is not shattered into degenerate ranges.
    The last span absorbs the remainder of the integer division.
    """
    want = end - start + 1
    if want <= 0:
        return []
    n = min(max(1, lanes), max(1, want // max(1, min_split)))
    step = want // n
    return [
        (start + i * step, end if i == n - 1 else start + (i + 1) * step - 1)
        for i in range(n)
    ]


class _IsolatedCircuit(Circuit):
    """``Circuit`` whose SOCKS credential survives an IPv6 endpoint.

    ``urlsplit`` strips the brackets off ``[::1]``, so rebuilding the netloc from
    ``hostname`` alone produces ``rv0:abcd@::1:9050`` — an address no SOCKS client can
    parse. External proxy lists do carry IPv6 entries, so the brackets go back on.
    """

    def _isolated_proxy(self) -> Optional[str]:
        if self._proxy is None:
            return None
        parts = urlsplit(self._proxy)
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        cred = f"rv{self.index}:{secrets.token_hex(8)}"
        return urlunsplit((parts.scheme, f"{cred}@{host}:{parts.port}", parts.path, "", ""))


@dataclasses.dataclass
class Endpoint:
    """One SOCKS5 daemon. Liveness is tracked per endpoint, not per lane."""

    address: str
    proxy_url: Optional[str]
    user_agent: str


@dataclasses.dataclass
class Lane:
    """One circuit on one endpoint — the unit that is checked out of the pool.

    A lane is a ``requests.Session``, which is not thread-safe, so unlike
    OnionAccelerator's ``ProxyPool`` (whose callers open a fresh connection every time)
    this pool has a real checkout and a matching release.
    """

    index: int
    endpoint: Endpoint
    circuit: Circuit


class LanePool:
    """Round-robin pool of lanes with OnionAccelerator's failover contract.

    ``acquire`` prefers a live, non-excluded lane; failing that a live one; failing that
    any lane at all. That last relaxation is deliberate and comes straight from
    ``ProxyPool``: when every endpoint is parked, a stale retry still beats giving up.
    The method therefore never returns ``None``.

    Lanes are interleaved across endpoints (``lane i`` serves ``endpoints[i % E]``) and
    handed out from a shared cursor. That combination is what makes a *sequential* caller
    rotate across daemons, which is precisely what the LIFO stack in ``tor.Transport``
    fails to do.
    """

    # acquire() relaxes its constraints in this order.
    _STRICT, _NO_EXCLUDE, _ANY = 0, 1, 2

    def __init__(self, lanes: Sequence[Lane]):
        self._lanes = list(lanes)
        self._free = set(range(len(self._lanes)))
        self._dead: set[str] = set()
        self._idx = 0
        self._cv = threading.Condition()
        self._taken: dict[int, int] = {i: 0 for i in range(len(self._lanes))}

    def __len__(self) -> int:
        return len(self._lanes)

    def _pick(self, constraint: int, exclude: Optional[Endpoint]) -> Optional[Lane]:
        """One full rotation of the cursor looking for a lane matching ``constraint``."""
        n = len(self._lanes)
        for _ in range(n):
            lane = self._lanes[self._idx % n]
            self._idx += 1
            if lane.index not in self._free:
                continue
            if constraint <= self._NO_EXCLUDE and lane.endpoint.address in self._dead:
                continue
            if constraint == self._STRICT and exclude is not None:
                if lane.endpoint.address == exclude.address:
                    continue
            self._free.discard(lane.index)
            self._taken[lane.index] += 1
            return lane
        return None

    def acquire(
        self, exclude: Optional[Endpoint] = None, timeout: float = LANE_WAIT_TIMEOUT
    ) -> Lane:
        deadline = time.monotonic() + timeout
        with self._cv:
            while True:
                for constraint in (self._STRICT, self._NO_EXCLUDE, self._ANY):
                    lane = self._pick(constraint, exclude)
                    if lane is not None:
                        return lane
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self._cv.wait(remaining):
                    raise TransportError(
                        f"no lane became free within {timeout:.0f}s "
                        f"({len(self._lanes)} lane(s), all busy)"
                    )

    def try_acquire(
        self, exclude: Optional[Endpoint] = None, strict: bool = False
    ) -> Optional[Lane]:
        """Take a lane only if one is sitting idle.

        This is what makes spare capacity opportunistic. Splitting and hedging both use
        it, so when the pool is saturated — by parallel walkers, say — they quietly
        collapse to a single request instead of queueing work behind themselves.

        ``strict`` refuses to fall back to the excluded endpoint. Hedging needs that: a
        second copy of a request down the same daemon races nothing, because the thing it
        is trying to get away from is that daemon's own latency.
        """
        with self._cv:
            constraints = (self._STRICT,) if strict else (self._STRICT, self._NO_EXCLUDE)
            for constraint in constraints:
                lane = self._pick(constraint, exclude)
                if lane is not None:
                    return lane
            return None

    def release(self, lane: Lane) -> None:
        with self._cv:
            self._free.add(lane.index)
            self._cv.notify()

    def mark_dead(self, endpoint: Endpoint) -> None:
        with self._cv:
            self._dead.add(endpoint.address)

    def mark_alive(self, endpoint: Endpoint) -> None:
        with self._cv:
            self._dead.discard(endpoint.address)

    def counts(self) -> dict[int, int]:
        """How many times each lane has been handed out, for tests and diagnostics."""
        with self._cv:
            return dict(self._taken)


class PooledTransport:
    """Range engine over many endpoints. Drop-in for ``tor.Transport``.

    ``endpoints=None`` means one direct lane, which issues exactly the request sequence
    ``Transport`` would: with a single lane there is nothing to split across and nothing
    to race, so both regimes disable themselves. That equivalence is what lets the whole
    offline suite run against this class unchanged.
    """

    def __init__(
        self,
        endpoints: Optional[Sequence[str]] = None,
        circuits_per_endpoint: int = 1,
        user_agents: Optional[Sequence[str]] = None,
        timeout: tuple[int, int] = (60, 120),
        retries: int = 3,
        verify: Verify = DEFAULT_VERIFY,
        hedge: int = DEFAULT_HEDGE,
        head_race: int = DEFAULT_HEAD_RACE,
        min_split: int = MIN_SPLIT,
    ):
        specs = list(endpoints) if endpoints else [None]
        uas = list(user_agents) if user_agents else [DEFAULT_UA]

        self.retries = retries
        self.verify = verify
        self.timeout = timeout
        self.hedge = max(1, hedge)
        self.head_race = max(1, head_race)
        self.min_split = max(1, min_split)
        if not verify:
            _silence_insecure_warnings()

        self.endpoints: list[Endpoint] = []
        for i, spec in enumerate(specs):
            proxy_url = parse_endpoint(spec)
            self.endpoints.append(
                Endpoint(
                    # Direct endpoints are labelled apart because they genuinely are
                    # independent: there is no daemon between them to queue behind, and
                    # one of them failing says nothing about the others.
                    address=proxy_url or f"{_DIRECT}#{i}",
                    proxy_url=proxy_url,
                    # Pinned per endpoint for the whole run, not rotated per request: one
                    # daemon is one identity as far as the far end is concerned, and
                    # changing the header mid-circuit is a fingerprint, not a defence.
                    user_agent=uas[i % len(uas)],
                )
            )

        lanes: list[Lane] = []
        per = max(1, circuits_per_endpoint)
        # Interleaved, so consecutive lane indices land on different daemons and a
        # sequential caller rotates across all of them.
        for c in range(per):
            for endpoint in self.endpoints:
                index = len(lanes)
                lanes.append(
                    Lane(
                        index=index,
                        endpoint=endpoint,
                        circuit=_IsolatedCircuit(
                            index, endpoint.proxy_url, endpoint.user_agent, timeout, verify
                        ),
                    )
                )
        self._pool = LanePool(lanes)
        self._exec = ThreadPoolExecutor(
            max_workers=max(1, len(lanes)), thread_name_prefix="rv-fetch"
        )

        # Surface compatibility with ``Transport``.
        self.proxy = self.endpoints[0].proxy_url
        self.n_circuits = len(lanes)

        self._lock = threading.Lock()
        self.bytes_fetched = 0
        self.requests_made = 0
        self.seconds_transferring = 0.0
        self.bytes_inflight = 0
        self._active = 0
        self._busy_since = 0.0

    # -- surface ---------------------------------------------------------------
    @property
    def lanes(self) -> int:
        return len(self._pool)

    @property
    def daemons(self) -> int:
        """Distinct endpoints, which is what hedging and racing actually depend on.

        Lanes on one daemon are separate circuits and do buy some aggregate bandwidth, so
        splitting a bulk range across them is worth it. Racing a *small* read across them
        is not: every copy queues behind the same process and the same guard, so the
        duplicate pays for itself only when the copies can fail independently.
        """
        return len({endpoint.address for endpoint in self.endpoints})

    @property
    def throughput(self) -> float:
        """Aggregate goodput in bytes/second, across every lane.

        ``Transport`` sums the wall time of each request, which with n lanes in flight
        understates the real rate by about n. That number is not cosmetic: it is what
        ``SingleBlockWarning`` turns into "about N days" when it asks whether a
        single-block ``.xz`` is worth starting. So the clock here runs whenever *any*
        request is in flight and stops when the last one lands, which makes the ratio a
        rate the caller would actually observe.
        """
        if self.seconds_transferring <= 0:
            return 512 * 1024.0
        return self.bytes_fetched / self.seconds_transferring

    def close(self) -> None:
        self._exec.shutdown(wait=False, cancel_futures=True)
        for lane in self._pool._lanes:
            try:
                lane.circuit.session.close()
            except Exception:  # noqa: BLE001 - closing is best-effort
                pass

    # -- accounting ------------------------------------------------------------
    def _count_request(self) -> None:
        with self._lock:
            self.requests_made += 1

    def _account_bytes(self, nbytes: int) -> None:
        with self._lock:
            self.bytes_fetched += nbytes

    def _inflight(self, delta: int) -> None:
        with self._lock:
            self.bytes_inflight += delta

    def _enter_transfer(self) -> None:
        with self._lock:
            if self._active == 0:
                self._busy_since = time.monotonic()
            self._active += 1

    def _leave_transfer(self) -> None:
        with self._lock:
            self._active -= 1
            if self._active == 0:
                self.seconds_transferring += time.monotonic() - self._busy_since

    # -- one HTTP request ------------------------------------------------------
    def _drain(self, response: requests.Response, abort: threading.Event) -> bytes:
        """Read a body in chunks, publishing what has arrived and watching for an abort.

        The chunking is what keeps a progress bar moving through a multi-megabyte block;
        the abort check is what stops a sibling span from finishing a transfer whose
        result has already been thrown away.
        """
        chunks: list[bytes] = []
        received = 0
        try:
            for chunk in response.iter_content(CHUNK):
                if abort.is_set():
                    raise TransportError("abandoned: a sibling sub-range already failed")
                chunks.append(chunk)
                received += len(chunk)
                self._inflight(len(chunk))
            return b"".join(chunks)
        finally:
            self._inflight(-received)

    def _one_request(
        self,
        url: str,
        start: int,
        end: int,
        lane: Lane,
        abort: threading.Event,
        timeout: Optional[tuple[int, int]] = None,
    ) -> bytes:
        """Fetch one absolute range on one lane. Never retries; the caller decides that."""
        want = end - start + 1
        circuit = lane.circuit
        log.debug(
            "GET bytes=%d-%d (%s) on lane %d (%s)",
            start,
            end,
            human_bytes(want),
            lane.index,
            lane.endpoint.address,
        )
        self._count_request()
        self._enter_transfer()
        t0 = time.monotonic()
        try:
            r, effective = gate.get(
                circuit.session,
                url,
                headers={"Range": f"bytes={start}-{end}"},
                timeout=timeout or circuit.timeout,
                # On every call rather than only on the session: merge_environment_settings
                # lets REQUESTS_CA_BUNDLE re-enable verification behind a session-level False.
                verify=circuit.verify,
            )
            try:
                if r.status_code == 200:
                    # The server ignored Range and the whole entity is coming. On a 2.4 GB
                    # archive that is catastrophic, and with several spans in flight it is
                    # catastrophic several times over, so bail out before touching the body.
                    raise RangeNotHonoured(_no_ranges(url, effective))
                if r.status_code != 206:
                    raise TransportError(f"HTTP {r.status_code} for range {start}-{end}")
                _validate_content_range(r.headers.get("Content-Range"), start, end)
                body = self._drain(r, abort)
            finally:
                r.close()
        finally:
            self._leave_transfer()

        if len(body) != want:
            raise TransportError(f"short range response: asked {want} bytes, got {len(body)}")
        elapsed = max(time.monotonic() - t0, 1e-6)
        self._account_bytes(len(body))
        log.debug(
            "got  bytes=%d-%d: %s in %.2fs (%s/s) on lane %d",
            start,
            end,
            human_bytes(len(body)),
            elapsed,
            human_bytes(len(body) / elapsed),
            lane.index,
        )
        return body

    # -- one span, with failover ----------------------------------------------
    def _fetch_span(
        self,
        url: str,
        span: tuple[int, int],
        lane: Optional[Lane],
        abort: threading.Event,
    ) -> bytes:
        """Fetch one span, retrying on a *different* endpoint each time.

        Retries are inline and never re-queued — the same discipline OnionAccelerator
        arrived at after a re-queue deadlocked its worker pool. Ownership of ``lane``
        passes to this method, which always releases whatever it is holding.
        """
        start, end = span
        last_error: Optional[Exception] = None
        last_endpoint: Optional[Endpoint] = None
        was_worker = _in_worker()
        _local.in_worker = True
        try:
            for attempt in range(self.retries):
                if abort.is_set():
                    raise TransportError("abandoned: a sibling sub-range already failed")
                if lane is None:
                    lane = self._pool.acquire(exclude=last_endpoint)
                try:
                    body = self._one_request(url, start, end, lane, abort)
                except RangeNotHonoured:
                    abort.set()
                    raise
                except requests.exceptions.SSLError as exc:
                    # A rejected certificate is a verdict, not a hiccup: retrying only
                    # adds delay to an answer that will not change.
                    abort.set()
                    raise _request_error(exc, url, lane.circuit.verify) from exc
                except Exception as exc:  # noqa: BLE001 - retry on any transport failure
                    last_error = exc
                    last_endpoint = lane.endpoint
                    log.warning(
                        "range %d-%d failed on lane %d (%s): %s; "
                        "parking that endpoint, attempt %d of %d",
                        start,
                        end,
                        lane.index,
                        lane.endpoint.address,
                        exc,
                        attempt + 1,
                        self.retries,
                    )
                    self._pool.mark_dead(lane.endpoint)
                    lane.circuit.rotate()
                    # Release before re-acquiring. No thread ever holds a lane while
                    # waiting for another, which is what makes the pool deadlock-free.
                    self._pool.release(lane)
                    lane = None
                    if self.lanes == 1 and attempt + 1 < self.retries:
                        # Nowhere else to go, so back off. With more than one endpoint,
                        # switching endpoint already changes the guard and the path, and
                        # sleeping would only waste the thing this workload is short of.
                        time.sleep(1.5 * (attempt + 1))
                    continue
                else:
                    self._pool.mark_alive(lane.endpoint)
                    return body
            raise TransportError(
                f"range {start}-{end} failed after {self.retries} attempts: {last_error}"
            )
        finally:
            _local.in_worker = was_worker
            if lane is not None:
                self._pool.release(lane)

    # -- fetching --------------------------------------------------------------
    def get_range(self, url: str, start: int, end: int) -> bytes:
        """Fetch the inclusive byte range ``[start, end]``.

        Always an absolute range: suffix ranges are never emitted, because some CDN tiers
        answer them with HTTP 501, so an archive footer must be reached by computing its
        offset from Content-Length.
        """
        if end < start:
            return b""
        want = end - start + 1
        if want <= HEDGE_MAX_BYTES and self.hedge > 1 and self.daemons > 1 and not _in_worker():
            return self._hedged(url, start, end)
        if want < self.min_split or self.lanes == 1 or _in_worker():
            return self._fetch_span(url, (start, end), None, threading.Event())
        return self._split(url, start, end)

    def _split(self, url: str, start: int, end: int) -> bytes:
        """Fan a large range out across lanes and reassemble it in order."""
        want = end - start + 1
        held = [self._pool.acquire()]
        limit = min(self.lanes, max(1, want // self.min_split))
        while len(held) < limit:
            extra = self._pool.try_acquire()
            if extra is None:
                break  # pool is busy; degrade rather than queue work behind ourselves
            held.append(extra)

        spans = split_spans(start, end, len(held), self.min_split)
        # split_spans caps at len(held), but a short range can produce fewer spans than
        # lanes held; give the surplus straight back so nothing idles behind us.
        for lane in held[len(spans) :]:
            self._pool.release(lane)
        held = held[: len(spans)]
        if len(spans) == 1:
            return self._fetch_span(url, spans[0], held[0], threading.Event())

        log.debug(
            "splitting bytes=%d-%d (%s) across %d lanes",
            start,
            end,
            human_bytes(want),
            len(spans),
        )
        abort = threading.Event()
        futures = [
            self._exec.submit(self._fetch_span, url, spans[i], held[i], abort)
            for i in range(1, len(spans))
        ]
        parts: list[bytes] = [b""] * len(spans)
        try:
            # The calling thread takes span 0. Progress therefore never depends on a
            # worker being schedulable, and every span already holds its own lane.
            parts[0] = self._fetch_span(url, spans[0], held[0], abort)
            for i, fut in enumerate(futures, start=1):
                parts[i] = fut.result()
        except BaseException:
            abort.set()
            for fut in futures:
                fut.cancel()
            wait(futures)
            raise
        body = b"".join(parts)
        if len(body) != want:
            raise TransportError(f"short split response: asked {want} bytes, got {len(body)}")
        return body

    def _one_shot(
        self,
        url: str,
        span: tuple[int, int],
        lane: Lane,
        abort: threading.Event,
        timeout: Optional[tuple[int, int]] = None,
    ) -> bytes:
        """A single attempt for a hedge racer. Releases its lane; never retries."""
        was_worker = _in_worker()
        _local.in_worker = True
        try:
            body = self._one_request(url, span[0], span[1], lane, abort, timeout=timeout)
        except Exception:
            # A copy abandoned because a peer already answered says nothing about this
            # endpoint. A copy that failed on its own says it is not worth racing again,
            # and without parking it every hedge would keep spending a lane on it.
            if not abort.is_set():
                self._pool.mark_dead(lane.endpoint)
                lane.circuit.rotate()
            raise
        else:
            self._pool.mark_alive(lane.endpoint)
            return body
        finally:
            _local.in_worker = was_worker
            self._pool.release(lane)

    def _hedged(self, url: str, start: int, end: int) -> bytes:
        """Race a small range across endpoints and keep the first answer.

        Only spare lanes are enlisted, so this costs nothing when the pool is busy and
        turns itself off entirely when parallel walkers are using every lane — which is
        the right split of a fixed budget, since a walker multiplies throughput and a
        hedge only trims a tail.
        """
        first = self._pool.acquire()
        held = [first]
        while len(held) < min(self.hedge, self.daemons):
            # strict: a copy on the same daemon would wait behind the same process.
            extra = self._pool.try_acquire(exclude=held[-1].endpoint, strict=True)
            if extra is None:
                break
            held.append(extra)
        if len(held) == 1:
            return self._fetch_span(url, (start, end), held[0], threading.Event())

        abort = threading.Event()
        # Losers get a shortened read timeout: a copy still silent after this long was
        # never going to win, and its lane is worth more back in the pool.
        loser_timeout = (self.timeout[0], min(self.timeout[1], HEDGE_READ_TIMEOUT))
        futures = [self._exec.submit(self._one_shot, url, (start, end), held[0], abort)]
        futures += [
            self._exec.submit(self._one_shot, url, (start, end), lane, abort, loser_timeout)
            for lane in held[1:]
        ]

        pending = set(futures)
        errors: list[Exception] = []
        try:
            while pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for fut in done:
                    exc = fut.exception()
                    if exc is None:
                        return fut.result()
                    if isinstance(exc, RangeNotHonoured):
                        raise exc
                    errors.append(exc)
        finally:
            # Whoever is still running is racing for an answer nobody wants.
            abort.set()
            for fut in futures:
                fut.cancel()

        # Every copy failed. Fall through to the full retry ladder, which parks endpoints
        # and works its way across the pool rather than giving up on a transient outage.
        log.debug(
            "all %d hedge copies of bytes=%d-%d failed (%s); falling back to retries",
            len(futures),
            start,
            end,
            errors[0] if errors else "no error recorded",
        )
        return self._fetch_span(url, (start, end), None, threading.Event())

    def get_ranges(self, url: str, spans: Sequence[tuple[int, int]]) -> list[bytes]:
        """Fetch several disjoint ranges concurrently, returned in the order given.

        A caller that already knows every offset it wants should say so once. Pushing
        them through ``get_range`` one at a time serialises requests that have no
        dependency on each other and leaves most of the pool idle — which is the whole
        shape of the problem this module exists to fix.
        """
        spans = list(spans)
        if not spans:
            return []
        if len(spans) == 1:
            return [self.get_range(url, spans[0][0], spans[0][1])]
        if _in_worker():
            # Reached from inside the fetch pool. Submitting here would make this thread
            # wait on the pool it is occupying, so do the work in place instead.
            return [self._fetch_span(url, s, None, threading.Event()) for s in spans]

        abort = threading.Event()
        futures = [
            self._exec.submit(self._fetch_span, url, span, None, abort) for span in spans[1:]
        ]
        out: list[bytes] = [b""] * len(spans)
        try:
            out[0] = self._fetch_span(url, spans[0], None, abort)
            for i, fut in enumerate(futures, start=1):
                out[i] = fut.result()
        except BaseException:
            abort.set()
            for fut in futures:
                fut.cancel()
            wait(futures)
            raise
        return out

    def head_like(self, url: str) -> tuple[int, dict[str, str]]:
        """Learn the entity size and validators without downloading anything.

        A one-byte Range GET rather than HEAD: it yields the total size via Content-Range
        *and* proves 206 support in one round trip, and it works against servers that
        refuse HEAD. Raced across a few endpoints, because it is the request every run
        blocks on and the one where a dead proxy shows up — and because the losers leave
        a warmed circuit behind on their daemon for the fetches that follow.
        """
        racers = min(self.head_race, self.daemons)
        held = [self._pool.acquire()]
        while len(held) < racers:
            extra = self._pool.try_acquire(exclude=held[-1].endpoint, strict=True)
            if extra is None:
                break
            held.append(extra)

        if len(held) == 1:
            lane = held[0]
            try:
                return self._head_once(url, lane)
            finally:
                self._pool.release(lane)

        abort = threading.Event()
        futures = [self._exec.submit(self._head_race_one, url, lane, abort) for lane in held]
        pending = set(futures)
        errors: list[Exception] = []
        try:
            while pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for fut in done:
                    exc = fut.exception()
                    if exc is None:
                        return fut.result()
                    if isinstance(exc, RangeNotHonoured):
                        raise exc
                    errors.append(exc)
        finally:
            abort.set()
            for fut in futures:
                fut.cancel()
        raise errors[0] if errors else TransportError(f"could not probe {url}")

    def _head_race_one(
        self, url: str, lane: Lane, abort: threading.Event
    ) -> tuple[int, dict[str, str]]:
        was_worker = _in_worker()
        _local.in_worker = True
        try:
            head = self._head_once(url, lane)
        except Exception:
            if not abort.is_set():
                self._pool.mark_dead(lane.endpoint)
            raise
        else:
            self._pool.mark_alive(lane.endpoint)
            return head
        finally:
            _local.in_worker = was_worker
            self._pool.release(lane)

    def _head_once(self, url: str, lane: Lane) -> tuple[int, dict[str, str]]:
        # Neither counted nor accounted, matching ``Transport``: ``requests_made`` means
        # range requests, and it is what the listing summary and the byte budgets in the
        # tests are stated in. A capability probe is not part of that story.
        circuit = lane.circuit
        try:
            r, effective = gate.get(
                circuit.session,
                url,
                headers={"Range": "bytes=0-0"},
                timeout=circuit.timeout,
                verify=circuit.verify,
            )
        except requests.RequestException as exc:
            raise _request_error(exc, url, circuit.verify) from exc
        try:
            # Drained but not accounted, as ``Transport`` does: a one-byte probe is not
            # transfer, and counting one per racer would make the byte budgets the tests
            # assert depend on how many lanes happened to be free.
            return _read_head(r, url, effective)
        finally:
            r.close()

    def open_stream(self, url: str) -> Stream:
        """Open the whole entity as one forward stream. See ``Transport.open_stream``.

        No hedging and no splitting: there is nothing to split, and a duplicate of a
        transfer this size is not a hedge, it is a second download. One lane carries it,
        and stays checked out until the stream is closed.
        """
        lane = self._pool.acquire()
        try:
            r, effective = gate.get(
                lane.circuit.session,
                url,
                timeout=lane.circuit.timeout,
                verify=lane.circuit.verify,
            )
        except requests.RequestException as exc:
            self._pool.release(lane)
            raise _request_error(exc, url, lane.circuit.verify) from exc
        except BaseException:
            self._pool.release(lane)
            raise
        try:
            return _stream_from(r, effective, lambda: self._pool.release(lane), self)
        except BaseException:
            r.close()
            self._pool.release(lane)
            raise

    def _stream_open(self) -> None:
        self._count_request()
        self._enter_transfer()

    def _stream_progress(self, delta: int) -> None:
        self._inflight(delta)

    def _stream_done(self, nbytes: int, seconds: float) -> None:
        # ``seconds`` is ignored here on purpose: this transport times transfers by the
        # wall clock kept between ``_enter_transfer`` and ``_leave_transfer``, so that a
        # rate stays a rate the caller would observe no matter how many lanes are busy.
        self._account_bytes(nbytes)
        self._leave_transfer()

    def supports_multirange(self, url: str, size: int) -> bool:
        """Opportunistic check for multipart/byteranges.

        Probed rather than assumed — some CDN tiers answer a multi-range request with 501
        — and every caller must have a single-range fallback.
        """
        if size < 4096:
            return False
        lane = self._pool.acquire()
        try:
            r = lane.circuit.session.get(
                url,
                headers={"Range": f"bytes=0-15,{size - 16}-{size - 1}"},
                stream=True,
                timeout=lane.circuit.timeout,
                verify=lane.circuit.verify,
            )
            try:
                ctype = r.headers.get("Content-Type", "")
                return r.status_code == 206 and "multipart/byteranges" in ctype.lower()
            finally:
                r.close()
        except Exception:  # noqa: BLE001 - a probe that fails is a "no"
            return False
        finally:
            self._pool.release(lane)

    # -- compatibility shims ---------------------------------------------------
    def _acquire(self) -> Circuit:
        """``Transport``-shaped checkout, for tests that reach past the public surface."""
        return self._pool.acquire().circuit

    def _release(self, circuit: Circuit) -> None:
        for lane in self._pool._lanes:
            if lane.circuit is circuit:
                self._pool.release(lane)
                return
