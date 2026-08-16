"""One HTTP request over one Tor circuit, and what to conclude from how it went.

Tor turns error handling from a formality into the main event. A request can fail
because the onion is down, because its descriptor could not be fetched, because the
circuit died halfway through the body, because an exit is overloaded, or because the
service itself is rate-limiting -- and the right response differs in each case. Retrying
down a circuit that has gone away is pure waste; rotating onto a new circuit because of
a 404 is worse.

So every outcome is reduced to one of five verdicts, and the worker in crawl.py does
nothing but act on the verdict. The classification lives here, in one place, where it
can be read and argued with.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import enum
import logging
import random
import time
from email.utils import parsedate_to_datetime
from typing import Mapping, Optional
from urllib.parse import urlsplit

import aiohttp
from yarl import URL

from .config import (
    BACKOFF_BASE,
    BACKOFF_CAP,
    ENDPOINT_DEATH_STREAK,
    MAX_RETRY_AFTER,
    READ_CHUNK,
)
from .frontier import Job
from .proxypool import AsyncLanePool, Endpoint, Lane
from .urlnorm import normalize_url

logger = logging.getLogger("OnionAccelerator.crawl.fetch")

# python_socks is only present when the real SOCKS connector is in use; the test suite
# swaps that out. Missing exceptions simply never match.
try:  # pragma: no cover - import guard
    from python_socks import ProxyError, ProxyConnectionError, ProxyTimeoutError
    _SOCKS_ERRORS: tuple[type[BaseException], ...] = (
        ProxyError, ProxyConnectionError, ProxyTimeoutError,
    )
except ImportError:  # pragma: no cover
    _SOCKS_ERRORS = ()

MAX_REDIRECTS = 5

# Bodies worth handing to the parser. Anything else is a file that happened to be
# linked from a listing, and reading it whole over Tor would be a waste of a circuit.
_PARSEABLE = ("text/html", "application/xhtml", "text/plain", "application/json", "text/json")

# Statuses that mean "the server is there, but not now".
_TRANSIENT_STATUS = frozenset({408, 425, 429, 502, 503, 504, 522, 523, 524})
# Statuses that mean "this URL, definitively, no".
_PERMANENT_STATUS = frozenset({400, 401, 402, 403, 404, 405, 410, 414, 451})
_REDIRECT_STATUS = frozenset({301, 302, 303, 307, 308})


class Verdict(enum.Enum):
    """What the worker should do next."""

    OK = "ok"          # a body worth parsing
    LEAF = "leaf"      # reached it, but it is a file, not a listing
    RETRY = "retry"    # transient: back off and try again, preferably elsewhere
    ROTATE = "rotate"  # the circuit is gone: rebuild the lane, then retry
    DROP = "drop"      # permanent: record it and move on


@dataclasses.dataclass
class FetchResult:
    """Everything one attempt produced, successful or not."""

    verdict: Verdict
    url: str
    final_url: Optional[str] = None
    status: Optional[int] = None
    content_type: str = ""
    body: Optional[str] = None
    nbytes: int = 0
    elapsed: float = 0.0
    error: Optional[str] = None
    retry_after: Optional[float] = None
    endpoint: Optional[Endpoint] = None
    lane_index: Optional[int] = None
    retry_budget: Optional[int] = None

    @property
    def ok(self) -> bool:
        return self.verdict in (Verdict.OK, Verdict.LEAF)

    def as_record(self) -> dict[str, object]:
        return {
            "url": self.url,
            "final_url": self.final_url,
            "status": self.status,
            "content_type": self.content_type,
            "bytes": self.nbytes,
            "elapsed_ms": round(self.elapsed * 1000),
            "verdict": self.verdict.value,
            "error": self.error,
            "endpoint": self.endpoint.address if self.endpoint else None,
        }


class AsyncFetcher:
    """Fetches one directory listing per call, over a leased circuit."""

    def __init__(self, pool: AsyncLanePool, *, max_page_bytes: int) -> None:
        self._pool = pool
        self._max_page_bytes = max_page_bytes

    async def fetch(self, job: Job, *, exclude: Optional[Endpoint] = None) -> FetchResult:
        """Fetch `job.url`, following same-host redirects, and classify the outcome.

        `exclude` is the endpoint that failed this job last time; the pool will avoid
        it if it can, which is the difference between retrying a dead daemon three times
        and actually getting the page.
        """
        started = time.monotonic()
        async with self._pool.lease(exclude=exclude) as lane:
            try:
                result = await self._request(lane, job.url)
            except Exception as exc:                      # noqa: BLE001 - classified below
                result = self._classify_exception(exc, job.url)
            result.elapsed = time.monotonic() - started
            result.endpoint = lane.endpoint
            result.lane_index = lane.index

            # A DROP means the *server* answered -- 404, 403, an off-host redirect. That
            # proves the circuit works, so like a success it counts in the endpoint's
            # favour. Only RETRY and ROTATE are the transport's fault.
            if result.verdict in (Verdict.OK, Verdict.LEAF, Verdict.DROP):
                await self._pool.record_success(lane, result.nbytes, result.elapsed)
            else:
                await self._pool.record_failure(lane, ENDPOINT_DEATH_STREAK)

            if result.verdict is Verdict.ROTATE:
                await self._pool.rotate(lane)

        logger.debug(
            "job=%s lane=%s endpoint=%s depth=%d attempt=%d status=%s bytes=%d ms=%d "
            "verdict=%s url=%s%s",
            id(job) % 100000, result.lane_index,
            result.endpoint.address if result.endpoint else "-",
            job.depth, job.attempt, result.status, result.nbytes,
            round(result.elapsed * 1000), result.verdict.value, job.url,
            f" error={result.error}" if result.error else "",
        )
        return result

    # ------------------------------------------------------------ the request

    async def _request(self, lane: Lane, url: str) -> FetchResult:
        """GET `url`, following redirects by hand so every hop is visible and checked."""
        current = url
        for hop in range(MAX_REDIRECTS + 1):
            async with lane.session.get(current, allow_redirects=False) as response:
                status = response.status
                content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip()

                if status in _REDIRECT_STATUS:
                    location = response.headers.get("Location")
                    if not location:
                        return FetchResult(Verdict.DROP, url, status=status,
                                           error="redirect without Location")
                    target = normalize_url(str(response.url.join(URL(location))))
                    if urlsplit(target).netloc != urlsplit(normalize_url(current)).netloc:
                        # Following an off-host redirect would walk the crawl off the
                        # tree, and on Tor it can walk it onto the clearnet entirely.
                        return FetchResult(Verdict.DROP, url, final_url=target, status=status,
                                           error="off-host redirect")
                    logger.debug("redirect %d %s -> %s (hop %d)", status, current, target, hop + 1)
                    current = target
                    continue

                if status in _TRANSIENT_STATUS:
                    return FetchResult(
                        Verdict.RETRY, url, final_url=current, status=status,
                        content_type=content_type,
                        error=f"HTTP {status}",
                        retry_after=_retry_after(response.headers.get("Retry-After")),
                    )
                if status in _PERMANENT_STATUS:
                    return FetchResult(Verdict.DROP, url, final_url=current, status=status,
                                       content_type=content_type, error=f"HTTP {status}")
                if status >= 500:
                    # An unrecognised 5xx gets exactly one more chance: it is more often
                    # a broken application than an overloaded one.
                    return FetchResult(Verdict.RETRY, url, final_url=current, status=status,
                                       content_type=content_type, error=f"HTTP {status}",
                                       retry_budget=1)
                if status >= 400:
                    return FetchResult(Verdict.DROP, url, final_url=current, status=status,
                                       content_type=content_type, error=f"HTTP {status}")

                if not any(content_type.startswith(p) for p in _PARSEABLE):
                    # Don't pull the body: this is a file, and the listing already told
                    # us everything we wanted to know about it.
                    return FetchResult(Verdict.LEAF, url, final_url=current, status=status,
                                       content_type=content_type,
                                       nbytes=_declared_length(response.headers))

                body, nbytes, truncated = await self._read_capped(response)
                if truncated:
                    return FetchResult(Verdict.LEAF, url, final_url=current, status=status,
                                       content_type=content_type, nbytes=nbytes,
                                       error=f"body exceeded {self._max_page_bytes} bytes")
                return FetchResult(Verdict.OK, url, final_url=current, status=status,
                                   content_type=content_type, body=body, nbytes=nbytes)

        return FetchResult(Verdict.DROP, url, final_url=current,
                           error=f"more than {MAX_REDIRECTS} redirects")

    async def _read_capped(self, response: aiohttp.ClientResponse) -> tuple[str, int, bool]:
        """Read at most `max_page_bytes`, decoding with whatever charset was declared."""
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.content.iter_chunked(READ_CHUNK):
            chunks.append(chunk)
            total += len(chunk)
            if total > self._max_page_bytes:
                return "", total, True
        raw = b"".join(chunks)
        charset = response.charset or "utf-8"
        try:
            return raw.decode(charset, errors="replace"), total, False
        except LookupError:
            return raw.decode("utf-8", errors="replace"), total, False

    # ------------------------------------------------------------ classification

    def _classify_exception(self, exc: BaseException, url: str) -> FetchResult:
        """Map a transport failure onto a verdict.

        The distinction that matters is *retry here* versus *retry somewhere else*. A
        timeout may just be a slow onion, so it is worth another go on a fresh lane. A
        dead circuit, a SOCKS-level refusal or a mid-body disconnect will keep failing
        the same way until the circuit is rebuilt, so those rotate first.
        """
        if isinstance(exc, asyncio.CancelledError):
            raise exc

        name = type(exc).__name__
        detail = f"{name}: {exc}".strip()

        if isinstance(exc, _SOCKS_ERRORS):
            # Tor answers a SOCKS request for an unreachable onion with 'TTL expired'
            # or 'host unreachable' -- a failed descriptor fetch or a failed rendezvous,
            # both of which a new circuit can fix and a retry down this one cannot.
            return FetchResult(Verdict.ROTATE, url, error=detail)
        if isinstance(exc, (aiohttp.ServerDisconnectedError, aiohttp.ClientPayloadError,
                            aiohttp.ClientConnectorError, aiohttp.ClientOSError,
                            ConnectionResetError)):
            # The connection either never came up or died under us. Both mean this
            # circuit is finished; ClientConnectorError and the proxy-connection error
            # that subclasses it are how a refused SOCKS dial surfaces.
            return FetchResult(Verdict.ROTATE, url, error=detail)
        if isinstance(exc, (asyncio.TimeoutError, aiohttp.ServerTimeoutError)):
            return FetchResult(Verdict.RETRY, url, error=detail or "timeout")
        if isinstance(exc, (UnicodeDecodeError, ValueError)):
            return FetchResult(Verdict.DROP, url, error=detail)
        if isinstance(exc, aiohttp.ClientError):
            return FetchResult(Verdict.RETRY, url, error=detail)

        logger.debug("unclassified transport error on %s: %s", url, detail)
        return FetchResult(Verdict.RETRY, url, error=detail)


# ---------------------------------------------------------------- backoff


def backoff_delay(attempt: int, retry_after: Optional[float] = None) -> float:
    """Exponential backoff with full jitter, or the server's own answer if it gave one.

    Full jitter rather than a fixed schedule because the failures being backed off are
    correlated: a dozen workers hitting one overloaded onion will otherwise retry in
    lockstep and overload it again at exactly the same moment.
    """
    if retry_after is not None:
        return max(0.0, min(retry_after, MAX_RETRY_AFTER))
    ceiling = min(BACKOFF_CAP, BACKOFF_BASE * (2 ** max(0, attempt)))
    return random.uniform(0.0, ceiling)


def _retry_after(value: Optional[str]) -> Optional[float]:
    """Parse a Retry-After header: delta-seconds or an HTTP-date."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    now = datetime.datetime.now(datetime.timezone.utc)
    return max(0.0, (when - now).total_seconds())


def _declared_length(headers: Mapping[str, str]) -> int:
    """Content-Length if it is present and sane, else 0. Never fatal."""
    try:
        return max(0, int(headers.get("Content-Length", "0")))
    except (TypeError, ValueError):
        return 0
