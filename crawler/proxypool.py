"""An async pool of Tor circuits, one aiohttp session per circuit.

This is the async counterpart of `remote_viewer/rvtree/transport/pooled.py`'s
`LanePool`, and it keeps that class's two load-bearing ideas:

  * **Lanes are interleaved across endpoints** (`lane i` serves `endpoints[i % E]`) and
    handed out from a shared cursor, so even a sequential caller rotates across daemons.
  * **acquire relaxes STRICT -> NO_EXCLUDE -> ANY**, so a retry prefers a different live
    daemon, settles for any live one, and -- when every endpoint is parked -- still
    hands back a stale lane, because a stale retry beats giving up.

The third idea is what makes "multi-Tor concurrency" mean anything. Twenty sessions
pointed at one SOCKS port share **one Tor circuit**, so twenty concurrent requests
queue behind each other through the same three relays and the same exit -- no
throughput gained, and the same rate limit hit. Tor's `IsolateSOCKSAuth` (on by default)
keys circuits on the SOCKS username/password, so giving each lane its own random
credential gets each lane its own circuit, even on a single daemon. That trick is
lifted from `_IsolatedCircuit._isolated_proxy()`; here it also gives `rotate()` its
meaning -- a new credential is a new circuit, which is the only way to walk away from a
dead one.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import secrets
import time
from typing import AsyncIterator, Callable, Optional, Sequence

import aiohttp

from .config import (
    CONNECT_TIMEOUT,
    LANE_WAIT_TIMEOUT,
    READ_TIMEOUT,
    TOTAL_TIMEOUT,
)

logger = logging.getLogger("OnionAccelerator.crawl.pool")

ConnectorFactory = Callable[["Endpoint", str, str], aiohttp.BaseConnector]


class PoolError(RuntimeError):
    """No lane could be produced."""


@dataclasses.dataclass
class Endpoint:
    """One SOCKS5 daemon. Liveness is tracked here, not per lane.

    A dead daemon is dead for every circuit on it, and a circuit that works proves the
    daemon works -- so the state belongs at this level.
    """

    address: str
    host: str
    port: int
    requests: int = 0
    failures: int = 0
    streak: int = 0
    bytes_in: int = 0
    latency_total: float = 0.0

    @classmethod
    def parse(cls, address: str) -> "Endpoint":
        host, _, port = address.rpartition(":")
        if not host or not port.isdigit():
            raise ValueError(f"not a host:port SOCKS endpoint: {address!r}")
        return cls(address=address, host=host.strip("[]"), port=int(port))

    @property
    def mean_latency(self) -> float:
        return self.latency_total / self.requests if self.requests else 0.0

    def as_record(self) -> dict[str, object]:
        return {
            "endpoint": self.address,
            "requests": self.requests,
            "failures": self.failures,
            "bytes_in": self.bytes_in,
            "mean_latency_s": round(self.mean_latency, 3),
        }


@dataclasses.dataclass
class Lane:
    """One circuit on one endpoint: the unit that is leased out of the pool.

    An `aiohttp.ClientSession` is not safe to drive from two places at once when its
    connector is capped at a single connection, which is exactly how a lane is built --
    one lane, one TCP stream, one circuit. Hence a real lease with a matching release.
    """

    index: int
    endpoint: Endpoint
    user_agent: str
    session: aiohttp.ClientSession
    credential: str
    rotations: int = 0


class AsyncLanePool:
    """Round-robin pool of circuit-isolated aiohttp sessions."""

    _STRICT, _NO_EXCLUDE, _ANY = 0, 1, 2

    def __init__(
        self,
        lanes: Sequence[Lane],
        endpoints: Sequence[Endpoint],
        *,
        connector_factory: ConnectorFactory,
        timeout: aiohttp.ClientTimeout,
    ) -> None:
        self._lanes = list(lanes)
        self._endpoints = list(endpoints)
        self._free = set(range(len(self._lanes)))
        self._dead: set[str] = set()
        self._idx = 0
        self._cv = asyncio.Condition()
        self._connector_factory = connector_factory
        self._timeout = timeout
        self._closed = False

    # ------------------------------------------------------------ construction

    @classmethod
    async def create(
        cls,
        addresses: Sequence[str],
        *,
        circuits_per_endpoint: int,
        user_agents: Sequence[str],
        connector_factory: Optional[ConnectorFactory] = None,
        timeout: Optional[aiohttp.ClientTimeout] = None,
    ) -> "AsyncLanePool":
        """Build `len(addresses) * circuits_per_endpoint` lanes.

        `connector_factory` is the single seam Tor sits behind: the test suite passes
        one that returns a plain TCPConnector, and nothing else in the crawler has to
        know whether it is talking through SOCKS.
        """
        if not addresses:
            raise PoolError("no SOCKS endpoints to build a pool from")
        if circuits_per_endpoint < 1:
            raise ValueError("circuits_per_endpoint must be >= 1")

        endpoints = [Endpoint.parse(a) for a in addresses]
        factory = connector_factory or socks_connector
        client_timeout = timeout or aiohttp.ClientTimeout(
            total=TOTAL_TIMEOUT, connect=CONNECT_TIMEOUT, sock_read=READ_TIMEOUT
        )

        lanes: list[Lane] = []
        for i in range(len(endpoints) * circuits_per_endpoint):
            endpoint = endpoints[i % len(endpoints)]     # interleave, don't block-fill
            credential = _new_credential(i)
            # One UA per lane for the lane's lifetime: a User-Agent that changes on a
            # fixed circuit is itself a distinguishing pattern.
            user_agent = user_agents[i % len(user_agents)] if user_agents else "Mozilla/5.0"
            lanes.append(Lane(
                index=i,
                endpoint=endpoint,
                user_agent=user_agent,
                credential=credential,
                session=_new_session(endpoint, credential, user_agent, factory, client_timeout),
            ))

        logger.info(
            "circuit pool: %d lane(s) over %d endpoint(s) (%d circuit(s) each)",
            len(lanes), len(endpoints), circuits_per_endpoint,
        )
        return cls(lanes, endpoints, connector_factory=factory, timeout=client_timeout)

    # ------------------------------------------------------------ leasing

    def __len__(self) -> int:
        return len(self._lanes)

    @property
    def endpoints(self) -> list[Endpoint]:
        return list(self._endpoints)

    def _pick(self, constraint: int, exclude: Optional[Endpoint]) -> Optional[Lane]:
        """One full rotation of the cursor, looking for a lane matching `constraint`."""
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
            return lane
        return None

    async def acquire(
        self,
        exclude: Optional[Endpoint] = None,
        timeout: float = LANE_WAIT_TIMEOUT,
    ) -> Lane:
        deadline = time.monotonic() + timeout
        async with self._cv:
            while True:
                if self._closed:
                    raise PoolError("pool is closed")
                for constraint in (self._STRICT, self._NO_EXCLUDE, self._ANY):
                    lane = self._pick(constraint, exclude)
                    if lane is not None:
                        return lane
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PoolError(
                        f"no lane became free within {timeout:.0f}s "
                        f"({len(self._lanes)} lane(s), all busy)"
                    )
                try:
                    await asyncio.wait_for(self._cv.wait(), remaining)
                except asyncio.TimeoutError:
                    continue

    async def release(self, lane: Lane) -> None:
        async with self._cv:
            self._free.add(lane.index)
            self._cv.notify()

    @contextlib.asynccontextmanager
    async def lease(
        self,
        exclude: Optional[Endpoint] = None,
        timeout: float = LANE_WAIT_TIMEOUT,
    ) -> AsyncIterator[Lane]:
        """Hold a lane for the duration of one request, releasing it even on error."""
        lane = await self.acquire(exclude, timeout)
        try:
            yield lane
        finally:
            await self.release(lane)

    # ------------------------------------------------------------ liveness

    async def mark_dead(self, endpoint: Endpoint) -> None:
        """Park an endpoint after a run of failures, and say so once, not every time."""
        async with self._cv:
            if endpoint.address not in self._dead:
                self._dead.add(endpoint.address)
                logger.warning(
                    "endpoint %s parked as dead after %d consecutive failure(s)",
                    endpoint.address, endpoint.streak,
                )

    async def mark_alive(self, endpoint: Endpoint) -> None:
        async with self._cv:
            endpoint.streak = 0
            if endpoint.address in self._dead:
                self._dead.discard(endpoint.address)
                logger.info("endpoint %s recovered", endpoint.address)

    async def record_success(self, lane: Lane, nbytes: int, elapsed: float) -> None:
        lane.endpoint.requests += 1
        lane.endpoint.bytes_in += nbytes
        lane.endpoint.latency_total += elapsed
        await self.mark_alive(lane.endpoint)

    async def record_failure(self, lane: Lane, death_streak: int) -> None:
        """Count a failure and park the endpoint once it has failed `death_streak` times."""
        lane.endpoint.requests += 1
        lane.endpoint.failures += 1
        lane.endpoint.streak += 1
        if lane.endpoint.streak >= death_streak:
            await self.mark_dead(lane.endpoint)

    async def rotate(self, lane: Lane) -> None:
        """Give this lane a new Tor circuit by rebuilding it on a fresh credential.

        The only remedy for a circuit that has gone away mid-transfer: the old one is
        not coming back, and retrying down it just spends the timeout again.
        """
        old = lane.session
        lane.credential = _new_credential(lane.index)
        lane.rotations += 1
        lane.session = _new_session(
            lane.endpoint, lane.credential, lane.user_agent,
            self._connector_factory, self._timeout,
        )
        logger.debug(
            "lane %d rotated onto a new circuit (endpoint=%s, rotation #%d)",
            lane.index, lane.endpoint.address, lane.rotations,
        )
        with contextlib.suppress(Exception):
            await old.close()

    # ------------------------------------------------------------ teardown & stats

    async def close(self) -> None:
        async with self._cv:
            self._closed = True
            self._cv.notify_all()
        for lane in self._lanes:
            with contextlib.suppress(Exception):
                await lane.session.close()

    def stats(self) -> list[dict[str, object]]:
        return [e.as_record() for e in self._endpoints]

    def balance_line(self) -> str:
        """A one-line per-endpoint request count, for the periodic progress log."""
        return " ".join(
            f"{e.address}={e.requests}"
            + (f"/{e.failures}f" if e.failures else "")
            + ("*" if e.address in self._dead else "")
            for e in self._endpoints
        )


# ---------------------------------------------------------------- session building


def _new_credential(index: int) -> str:
    """A SOCKS username/password pair that no other lane will collide with.

    The username carries the lane index so a `tor` log at `notice` can be read against
    this crawler's own log; the random half is what actually forces circuit isolation.
    """
    return f"oa{index}:{secrets.token_hex(8)}"


def socks_connector(endpoint: Endpoint, credential: str, _user_agent: str) -> aiohttp.BaseConnector:
    """A SOCKS5 connector bound to one isolated Tor circuit.

    `rdns=True` is not optional: it is what `socks5h` means, and without it the client
    resolves the hostname locally -- which for a `.onion` fails outright, and for a
    clearnet target leaks the lookup to the local resolver.

    `ssl=False` mirrors the rest of the project: an onion address is itself the
    service's authentication, so the self-signed certificates common on onion HTTPS are
    accepted rather than fatal.
    """
    from aiohttp_socks import ProxyConnector, ProxyType

    username, _, password = credential.partition(":")
    return ProxyConnector(
        host=endpoint.host,
        port=endpoint.port,
        proxy_type=ProxyType.SOCKS5,
        username=username,
        password=password,
        rdns=True,
        ssl=False,
        limit=1,              # one lane, one connection, one circuit
        ttl_dns_cache=0,
        enable_cleanup_closed=True,
    )


def _new_session(
    endpoint: Endpoint,
    credential: str,
    user_agent: str,
    factory: ConnectorFactory,
    timeout: aiohttp.ClientTimeout,
) -> aiohttp.ClientSession:
    return aiohttp.ClientSession(
        connector=factory(endpoint, credential, user_agent),
        timeout=timeout,
        headers={
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
        },
        # Redirects are followed by the fetcher, not here, so that each hop is logged
        # and scope-checked rather than silently walking off the tree.
        auto_decompress=True,
    )
