"""The remote-size probe ladder: HEAD, then ranged and unranged GETs.

Moved out of OnionAccelerator.py unchanged. tests/test_size_probe.py patches `_probe`
through OnionAccelerator; the forwarding table there makes that patch land here.
"""

from email.message import Message
from typing import NamedTuple, Optional

import requests

from .common import logger, make_proxies
from .config import TRANSFER_TIMEOUT


# =========== REMOTE FILE SIZE PROBE ===========

# The lengths a response advertises only describe the file when the body is sent
# as-is. Under any other Content-Encoding they describe the *compressed* transfer,
# and feeding one of those to the range splitter would cut the file at the wrong
# offsets. See _size_from_response().
IDENTITY_ENCODINGS = {"", "identity"}


class SizeProbe(NamedTuple):
    """What a size probe learned about a URL.

    `size is None` means undetermined, which is not the same as unreachable:
    `reachable` says whether any probe got an answer out of the server at all.
    """
    size: Optional[int] = None
    reachable: bool = False
    source: str = ""
    error: Optional[str] = None


def _positive_int(value) -> Optional[int]:
    """A header value as a positive int, or None if it isn't one.

    Zero counts as undetermined on purpose: a HEAD-hostile server answering
    "Content-Length: 0" is far more common than a genuinely empty file, and
    treating it as unknown keeps the ladder walking instead of stopping there.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text.isdigit():
        return None
    n = int(text)
    return n if n > 0 else None


def _size_from_content_length(headers) -> Optional[int]:
    """Content-Length, which on a non-206 response is the size of the whole entity."""
    return _positive_int(headers.get("content-length"))


def _size_from_content_disposition(headers) -> Optional[int]:
    """The optional `size` parameter of Content-Disposition.

    `Message.get_param` is the stdlib's header parser, so quoting and the RFC 2231
    form come for free and, unlike a "size=(digits)" regex, it will not read the 99
    out of `filename="report-size=99.pdf"`. (`cgi.parse_header` would be the obvious
    alternative and is gone in Python 3.13.)
    """
    raw = headers.get("content-disposition")
    if not raw:
        return None
    msg = Message()
    msg["Content-Disposition"] = raw
    param = msg.get_param("size", header="content-disposition")
    if isinstance(param, tuple):    # RFC 2231 form: (charset, language, value)
        param = param[2]
    return _positive_int(param)


def _size_from_content_range(headers) -> Optional[int]:
    """The total after the slash in `Content-Range: bytes 0-0/1048576`.

    An unsatisfied range answers `bytes */0`, whose "*" is not a digit and is
    rejected by _positive_int.
    """
    return _positive_int(headers.get("content-range", "").rsplit("/", 1)[-1])


def _size_from_response(status, headers):
    """The size a single response advertises, as (size, source); (None, "") if none.

    A non-identity Content-Encoding disqualifies the whole response: every number it
    carries then describes the compressed transfer rather than the file, and a body
    that arrives encoded cannot be reassembled out of parallel ranges anyway.
    Answering "unknown" routes the file to fallback_sequential_download(), which
    handles it correctly, instead of letting the merge fail after every byte has
    already been pulled over Tor.

    On a 206 the Content-Length is the length of the returned range -- one byte for
    our probe -- so the entity size has to come from Content-Range instead. Reading
    Content-Length there would report every file as 1 byte.
    """
    encoding = headers.get("content-encoding", "").strip().lower()
    if encoding not in IDENTITY_ENCODINGS:
        logger.debug(f"Ignoring the sizes on a response sent with Content-Encoding: {encoding}")
        return None, ""
    if status == 206:
        size = _size_from_content_range(headers)
        if size:
            return size, "Content-Range"
    else:
        size = _size_from_content_length(headers)
        if size:
            return size, "Content-Length"
    size = _size_from_content_disposition(headers)
    if size:
        return size, "Content-Disposition size="
    return None, ""


def _probe(session, url, method, extra_headers=None, stream=False):
    """Issue one probe request and return (status, lower-cased headers).

    allow_redirects is passed explicitly because requests.head() defaults it to
    False, and a redirect is not a 4xx -- so an unguarded HEAD happily reports the
    redirect's empty body as the size of a file that downloads perfectly well.

    The body is never touched on the streaming probes: closing an unconsumed
    response tears the socket down instead of returning it to the pool, and that is
    what stops the payload from being downloaded. The close sits in a finally so it
    happens on every path out.
    """
    r = session.request(method, url, headers=extra_headers, stream=stream,
                        timeout=TRANSFER_TIMEOUT, allow_redirects=True)
    try:
        return r.status_code, {k.lower(): v for k, v in r.headers.items()}
    finally:
        r.close()


def probe_remote_size(url, proxy, ua=None) -> SizeProbe:
    """Determine the size of `url` without downloading it.

    Four sources of evidence, tried in this order and stopping at the first that
    answers. They cost at most three requests, because the first two read the same
    response:

      1. Content-Length on a HEAD.
      2. The `size=` parameter of that same response's Content-Disposition -- free,
         since it shares the round trip.
      3. Content-Range on a one-byte `Range: bytes=0-0` GET. A 206 gives the total
         after the slash; a server that ignores the Range answers 200, and then its
         Content-Length is the whole entity.
      4. A plain streamed GET, aborted as soon as the headers are in. It earns its
         place because a server that rejects *any* Range header with 501 never gets
         an answer out of probe 3.

    One Session covers all of them, so the proxy is dialled once rather than three
    times, and its close() releases every socket on every exit path.
    """
    last_error = None
    reachable = False

    with requests.Session() as session:
        session.proxies.update(make_proxies(proxy))
        session.verify = False
        if ua:
            session.headers["User-Agent"] = ua

        probes = (
            ("HEAD", "HEAD", None, False),
            ("range GET", "GET", {"Range": "bytes=0-0"}, True),
            ("streamed GET", "GET", None, True),
        )
        for label, method, extra_headers, stream in probes:
            try:
                status, headers = _probe(session, url, method, extra_headers, stream)
            except requests.exceptions.ConnectionError as e:
                # Covers ProxyError and ConnectTimeout: the proxy or the endpoint is
                # unreachable, so the remaining probes would fail the same way on the
                # same proxy. Stop rather than spend another 2 x REQUEST_TIMEOUT.
                logger.debug(f"{label} for {url} (proxy={proxy}) could not connect: {e}")
                return SizeProbe(reachable=reachable, error=str(e))
            except requests.RequestException as e:
                # Read timeout, too many redirects, malformed response: this probe is
                # out, but another method may still get an answer.
                last_error = str(e)
                logger.debug(f"{label} for {url} (proxy={proxy}) failed: {e}")
                continue

            if status >= 400:
                # A 405 on HEAD or a 501/416 on Range is the very reason the later
                # probes exist, so it isn't an error -- just move on.
                last_error = f"HTTP {status}"
                logger.debug(f"{label} for {url} returned HTTP {status}, trying the next probe")
                continue

            reachable = True
            size, source = _size_from_response(status, headers)
            if size:
                logger.debug(f"{url}: size={size} B via {source} ({label})")
                return SizeProbe(size=size, reachable=True, source=source)

    logger.debug(f"No size for {url} after all probes (last error: {last_error})")
    return SizeProbe(reachable=reachable, error=last_error)


def get_remote_file_size(url, proxy, ua) -> Optional[int]:
    """The size of `url` in bytes, or None if no probe could determine it."""
    return probe_remote_size(url, proxy, ua).size
