"""Tor transport: a pool of independently-isolated SOCKS5h circuits.

Tor gives every distinct SOCKS username/password its own circuit (``IsolateSOCKSAuth``,
on by default). Handing each worker a different credential therefore buys real
parallel bandwidth: measured 555 KiB/s on one circuit versus 1341 KiB/s across six.
The gain flattens quickly, so ``--circuits`` defaults to 4.

TLS certificates are **not** verified by default. An onion address is itself the
service's public key, so the address you typed already authenticates the peer end to
end; a certificate chain adds nothing there, and onion services accordingly almost never
carry one a CA would vouch for. Verifying by default would simply make HTTPS onions
unreachable. The trade is real for clearnet hosts reached through an untrusted exit node
— ``verify=True`` (``--verify-tls``) restores the check for anyone who wants it.
"""

from __future__ import annotations

import logging
import queue
import secrets
import threading
import time
from typing import Iterator, Optional, Union
from urllib.parse import urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter

from ..util import human_bytes

log = logging.getLogger(__name__)

try:
    from urllib3.exceptions import InsecureRequestWarning
except ImportError:  # some distributions ship requests with a vendored urllib3
    from requests.packages.urllib3.exceptions import InsecureRequestWarning  # type: ignore

DEFAULT_PROXY = "socks5h://127.0.0.1:9150"
DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; rv:128.0) Gecko/20100101 Firefox/128.0"
DEFAULT_VERIFY = False

# Body read size. Only visible in how often in-flight progress moves: a single 4 MiB
# block over Tor takes seconds, and a display that only learns of bytes when the whole
# request lands would sit frozen for all of them.
CHUNK = 64 * 1024

# ``True``/``False``, or a path to a CA bundle to verify against.
Verify = Union[bool, str]

_warnings_silenced = False


class TransportError(RuntimeError):
    pass


class RangeNotHonoured(TransportError):
    """The server answered a Range request with the whole entity."""


def _check_proxy(proxy: Optional[str]) -> Optional[str]:
    """Reject proxy schemes that would resolve the target hostname locally.

    ``socks5://`` makes the client do DNS itself, which leaks the target to the
    local resolver and cannot reach ``.onion`` names at all. Only ``socks5h://``
    (and the equivalent ``socks4a``) defer resolution to the proxy.
    """
    if proxy in (None, "", "none", "direct"):
        return None
    scheme = urlsplit(proxy).scheme.lower()
    if scheme in ("socks5", "socks4"):
        raise TransportError(
            f"refusing proxy scheme {scheme!r}: it resolves DNS locally, which leaks the "
            f"target host and cannot reach .onion addresses. Use socks5h:// instead."
        )
    if scheme not in ("socks5h", "socks4a", "http", "https"):
        raise TransportError(f"unsupported proxy scheme {scheme!r}")
    return proxy


def _silence_insecure_warnings() -> None:
    """Stop urllib3 announcing every unverified connection.

    Not verifying is the documented default here, so the warning is noise — and it lands
    on stderr, where the summary line lives. The filter is process-global, which is the
    right tool anyway: ``warnings.catch_warnings()`` is not thread-safe and a Transport
    is driven from a ThreadPoolExecutor.
    """
    global _warnings_silenced
    if not _warnings_silenced:
        import urllib3

        urllib3.disable_warnings(InsecureRequestWarning)
        _warnings_silenced = True


def _request_error(exc: Exception, url: str, verify: Verify) -> TransportError:
    """Turn a requests-level failure into something the CLI can print without a traceback."""
    if isinstance(exc, requests.exceptions.SSLError):
        if verify:
            return TransportError(
                f"TLS verification failed for {url}: {exc}\n"
                f"rvtree does not verify certificates by default — drop --verify-tls, or "
                f"point --ca-bundle at the issuing CA."
            )
        return TransportError(f"TLS handshake failed for {url}: {exc}")
    return TransportError(f"could not reach {url}: {exc}")


class Circuit:
    """One Tor circuit, pinned to a `requests.Session` so the TCP+TLS setup is reused.

    A fresh handshake through Tor costs seconds, so a long-lived session matters far
    more here than it would over a direct connection.
    """

    def __init__(
        self,
        index: int,
        proxy: Optional[str],
        user_agent: str,
        timeout: tuple[int, int],
        verify: Verify = DEFAULT_VERIFY,
    ):
        self.index = index
        self._proxy = proxy
        self._user_agent = user_agent
        self.timeout = timeout
        self.verify = verify
        self.session: requests.Session
        self._build()

    def _isolated_proxy(self) -> Optional[str]:
        if self._proxy is None:
            return None
        parts = urlsplit(self._proxy)
        # A distinct credential is what earns a distinct circuit.
        cred = f"rv{self.index}:{secrets.token_hex(8)}"
        netloc = f"{cred}@{parts.hostname}:{parts.port}"
        return urlunsplit((parts.scheme, netloc, parts.path, "", ""))

    def _build(self) -> None:
        s = requests.Session()
        s.headers.update({"User-Agent": self._user_agent, "Accept": "*/*", "Accept-Encoding": "identity"})
        s.max_redirects = 5
        s.verify = self.verify
        proxy = self._isolated_proxy()
        if proxy:
            s.proxies = {"http": proxy, "https": proxy}
        adapter = HTTPAdapter(pool_connections=2, pool_maxsize=2, max_retries=0)
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        self.session = s

    def rotate(self) -> None:
        """Tear down and rebuild on a brand-new circuit, e.g. after a dead exit node."""
        try:
            self.session.close()
        except Exception:
            pass
        self._build()


class Transport:
    """Range-request engine over a pool of circuits, with byte/request accounting."""

    def __init__(
        self,
        proxy: Optional[str] = DEFAULT_PROXY,
        circuits: int = 4,
        user_agent: str = DEFAULT_UA,
        timeout: tuple[int, int] = (60, 120),
        retries: int = 3,
        verify: Verify = DEFAULT_VERIFY,
    ):
        self.proxy = _check_proxy(proxy)
        self.retries = retries
        self.verify = verify
        if not verify:
            _silence_insecure_warnings()
        self._pool: queue.LifoQueue[Circuit] = queue.LifoQueue()
        for i in range(max(1, circuits)):
            self._pool.put(Circuit(i, self.proxy, user_agent, timeout, verify))
        self.n_circuits = max(1, circuits)
        self._lock = threading.Lock()
        self.bytes_fetched = 0
        self.requests_made = 0
        self.seconds_transferring = 0.0
        # Bytes of transfers still in progress. Kept apart from ``bytes_fetched`` so
        # that what the summary reports stays exactly what completed.
        self.bytes_inflight = 0

    # -- accounting ------------------------------------------------------------
    def _account(self, nbytes: int, seconds: float) -> None:
        with self._lock:
            self.bytes_fetched += nbytes
            self.requests_made += 1
            self.seconds_transferring += seconds

    def _inflight(self, delta: int) -> None:
        with self._lock:
            self.bytes_inflight += delta

    @property
    def throughput(self) -> float:
        """Observed bytes/second, used to decide seek-versus-read-through."""
        if self.seconds_transferring <= 0:
            return 512 * 1024.0
        return self.bytes_fetched / self.seconds_transferring

    # -- circuit checkout ------------------------------------------------------
    def _acquire(self) -> Circuit:
        return self._pool.get()

    def _release(self, c: Circuit) -> None:
        self._pool.put(c)

    # -- fetching --------------------------------------------------------------
    def get_range(self, url: str, start: int, end: int) -> bytes:
        """Fetch the inclusive byte range ``[start, end]``.

        Always an absolute range. Suffix ranges (``bytes=-N``) are deliberately never
        emitted: kernel.org's cache tier answers them with HTTP 501, so anything that
        needs an archive footer must compute the absolute offset from Content-Length.
        """
        if end < start:
            return b""
        want = end - start + 1
        headers = {"Range": f"bytes={start}-{end}"}
        last_error: Optional[Exception] = None

        for attempt in range(self.retries):
            circuit = self._acquire()
            t0 = time.monotonic()
            log.debug(
                "GET bytes=%d-%d (%s) on circuit %d", start, end, human_bytes(want), circuit.index
            )
            try:
                # ``verify`` goes on every call rather than only on the session:
                # merge_environment_settings lets REQUESTS_CA_BUNDLE and CURL_CA_BUNDLE
                # override a session-level False, which would silently re-enable
                # verification for anyone who has either set.
                r = circuit.session.get(
                    url,
                    headers=headers,
                    stream=True,
                    timeout=circuit.timeout,
                    verify=circuit.verify,
                )
                try:
                    if r.status_code == 200:
                        # The server ignored Range and is about to hand us the entire
                        # entity. On a 100 GB archive that is catastrophic, so bail out
                        # before touching the body.
                        raise RangeNotHonoured(
                            f"server returned 200 for a Range request ({url}); "
                            f"it does not support partial content"
                        )
                    if r.status_code != 206:
                        raise TransportError(f"HTTP {r.status_code} for range {start}-{end}")
                    _validate_content_range(r.headers.get("Content-Range"), start, end)
                    body = self._drain(r)
                finally:
                    r.close()
            except RangeNotHonoured:
                self._release(circuit)
                raise
            except requests.exceptions.SSLError as exc:
                # A rejected certificate is a verdict, not a hiccup: retrying it three
                # times only adds backoff to an answer that will not change.
                self._release(circuit)
                raise _request_error(exc, url, circuit.verify) from exc
            except Exception as exc:  # noqa: BLE001 - retry on any transport failure
                last_error = exc
                # WARNING, not DEBUG: a run that has gone quiet for two minutes because
                # it is patiently retrying should say so without being asked.
                log.warning(
                    "range %d-%d failed on circuit %d (%s); rotating circuit, attempt %d of %d",
                    start,
                    end,
                    circuit.index,
                    exc,
                    attempt + 1,
                    self.retries,
                )
                circuit.rotate()  # a stalled or failing exit is worth abandoning
                self._release(circuit)
                if attempt + 1 < self.retries:
                    time.sleep(1.5 * (attempt + 1))
                continue
            else:
                self._release(circuit)
                elapsed = max(time.monotonic() - t0, 1e-6)
                self._account(len(body), elapsed)
                log.debug(
                    "got  bytes=%d-%d: %s in %.2fs (%s/s)",
                    start,
                    end,
                    human_bytes(len(body)),
                    elapsed,
                    human_bytes(len(body) / elapsed),
                )
                if len(body) != want:
                    raise TransportError(
                        f"short range response: asked {want} bytes, got {len(body)}"
                    )
                return body

        raise TransportError(f"range {start}-{end} failed after {self.retries} attempts: {last_error}")

    def _drain(self, response: requests.Response) -> bytes:
        """Read a body in chunks, publishing what has arrived as it arrives.

        ``response.content`` performs the same read in one blocking call. The difference
        is only visible to a progress display, and it is the difference between a bar
        that moves through a multi-megabyte block and one that looks hung until it lands.
        """
        chunks: list[bytes] = []
        received = 0
        try:
            for chunk in response.iter_content(CHUNK):
                chunks.append(chunk)
                received += len(chunk)
                self._inflight(len(chunk))
            return b"".join(chunks)
        finally:
            # Whatever happens next, these bytes stop being "in flight": on success
            # ``_account`` takes them over, and on failure they were never delivered.
            self._inflight(-received)

    def head_like(self, url: str) -> tuple[int, dict[str, str]]:
        """Learn the entity size and validators without downloading anything.

        Prefers a one-byte Range GET over HEAD: it yields the total size via
        Content-Range *and* proves 206 support in a single round trip, and it works
        against servers that refuse HEAD.
        """
        circuit = self._acquire()
        try:
            try:
                r = circuit.session.get(
                    url,
                    headers={"Range": "bytes=0-0"},
                    stream=True,
                    timeout=circuit.timeout,
                    verify=circuit.verify,
                )
            except requests.RequestException as exc:
                # This is the first request of every run, so it is where a dead proxy or
                # a rejected certificate surfaces. Say so plainly instead of letting a
                # requests exception reach the top level as a traceback.
                raise _request_error(exc, url, circuit.verify) from exc
            try:
                headers = {k.lower(): v for k, v in r.headers.items()}
                if r.status_code == 206:
                    cr = headers.get("content-range", "")
                    total = cr.rsplit("/", 1)[-1].strip()
                    if not total.isdigit():
                        raise TransportError(f"unparseable Content-Range: {cr!r}")
                    r.content
                    log.info(
                        "%s: %s, ranges honoured, server %s",
                        url,
                        human_bytes(int(total)),
                        headers.get("server", "-"),
                    )
                    return int(total), headers
                if r.status_code == 200:
                    length = headers.get("content-length")
                    if not length or not length.isdigit():
                        raise TransportError("server ignored Range and gave no Content-Length")
                    return int(length), headers
                raise TransportError(f"HTTP {r.status_code} probing {url}")
            finally:
                r.close()
        finally:
            self._release(circuit)

    def supports_multirange(self, url: str, size: int) -> bool:
        """Opportunistic check for multipart/byteranges.

        kernel.org's tier answers multi-range with 501, so this is probed rather than
        assumed and every caller must have a single-range fallback.
        """
        if size < 4096:
            return False
        circuit = self._acquire()
        try:
            r = circuit.session.get(
                url,
                headers={"Range": f"bytes=0-15,{size - 16}-{size - 1}"},
                stream=True,
                timeout=circuit.timeout,
                verify=circuit.verify,
            )
            try:
                ctype = r.headers.get("Content-Type", "")
                return r.status_code == 206 and "multipart/byteranges" in ctype.lower()
            finally:
                r.close()
        except Exception:
            return False
        finally:
            self._release(circuit)

    def close(self) -> None:
        while True:
            try:
                self._pool.get_nowait().session.close()
            except queue.Empty:
                return
            except Exception:
                pass


def _validate_content_range(value: Optional[str], start: int, end: int) -> None:
    """Confirm the server sent back the range we actually asked for."""
    if not value:
        raise TransportError("206 response without Content-Range")
    spec = value.strip()
    if spec.lower().startswith("bytes"):
        spec = spec[5:].lstrip()
    got_range = spec.split("/", 1)[0].strip()
    try:
        got_start, got_end = (int(x) for x in got_range.split("-", 1))
    except ValueError as exc:
        raise TransportError(f"unparseable Content-Range: {value!r}") from exc
    if got_start != start or got_end != end:
        raise TransportError(
            f"server returned range {got_start}-{got_end}, expected {start}-{end}"
        )
