"""Proof-of-work interstitials that stand between a URL and the bytes behind it.

Some onion services front their downloads with a JavaScript challenge: the first GET
of ``/dir/file.7z`` returns an HTML page, not the file, and the real entity only appears
after the client hashes its way to a nonce and posts it back. A browser runs the script
and never notices. Everything else — curl, requests, rvtree — sees a 200 carrying
``text/html`` and a chunked body, and reports something misleading about Content-Length
or ranges when the truth is that it never reached the file at all.

The challenge here is a cost function, not a secret: the page hands out the input, the
difficulty, and the endpoint to post to, and any client willing to spend the CPU may
pass. So this module spends it. It does not pretend to be a browser and it does not look
for a way around the work — it does the work, which is what the gate is asking for.

Two properties of these gates shape everything downstream:

* the URL it redirects to carries a **single-use** token, so a resolved URL cannot be
  cached and reused for a second request; and
* that URL is usually served by the application rather than the web server, which means
  ``Accept-Ranges: none``. Passing the gate gets you a Content-Length and one stream
  from byte zero, and no random access at all.

``rvtree probe`` reports both, and ``archive.open_archive`` chooses its source with them.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urljoin

from .errors import TransportError

log = logging.getLogger(__name__)

# Only ever parsed out of a body we already know to be small and HTML, so a handful of
# regexes is proportionate — a dependency on an HTML parser would not buy accuracy here.
_FORM_RE = re.compile(r"<form[^>]*\baction\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</form>", re.I | re.S)
_INPUT_RE = re.compile(r"<input[^>]*\bname\s*=\s*[\"']([^\"']+)[\"'][^>]*>", re.I)
_VALUE_RE = re.compile(r"\bvalue\s*=\s*[\"']([^\"']*)[\"']", re.I)
_DIFFICULTY_RE = re.compile(r"\bdifficulty\s*=\s*(\d+)", re.I)
_SHA256_RE = re.compile(r"[\"']SHA-?256[\"']", re.I)

# A gate worth solving states its difficulty as a count of leading hex zeros. Four is
# what this family ships with (~65k hashes, tens of milliseconds); the ceiling is a
# guard against burning a core on a page that means something else entirely. Each step
# costs 16x the last, so 7 is already minutes.
MAX_DIFFICULTY = 7

# The body of a challenge page is a few KiB. Anything larger is the payload itself and
# must never be pulled into memory to be pattern-matched.
MAX_BODY = 256 * 1024

# How many gates one request may pass through. A gate that redirects to another gate is
# a loop, not a protocol.
MAX_GATES = 2


class GateError(TransportError):
    """A challenge was recognised but could not be passed."""


@dataclass(frozen=True)
class Challenge:
    """A parsed proof-of-work page: hash ``seed + nonce`` until the digest starts with zeros."""

    seed: str
    difficulty: int
    action: str
    seed_field: str = "challenge"
    nonce_field: str = "nonce"

    @property
    def target(self) -> str:
        return "0" * self.difficulty


def looks_like_payload(headers) -> bool:
    """True when the response is plainly the entity, so the body need not be read.

    The check that keeps a gate probe from costing a download: a challenge page is
    ``text/html``, and anything else is the file we asked for.
    """
    ctype = (headers.get("Content-Type") or headers.get("content-type") or "").lower()
    return "html" not in ctype


def detect(body: str, url: str) -> Optional[Challenge]:
    """Parse a proof-of-work challenge out of an HTML body, or return ``None``.

    Deliberately narrow: it wants a hidden form, a seed in it, a stated difficulty, and
    a SHA-256 in the script. A page missing any of those is some other kind of HTML —
    a login form, an error page — and this must not claim it can pass those.
    """
    if not body or "<form" not in body.lower():
        return None
    difficulty_m = _DIFFICULTY_RE.search(body)
    if not difficulty_m or not _SHA256_RE.search(body):
        return None

    for action, inner in _FORM_RE.findall(body):
        fields = {}
        for match in _INPUT_RE.finditer(inner):
            tag = match.group(0)
            value_m = _VALUE_RE.search(tag)
            fields[match.group(1)] = value_m.group(1) if value_m else ""
        seed_field = next((k for k in fields if k.lower() in ("challenge", "seed", "token")), None)
        nonce_field = next((k for k in fields if k.lower() in ("nonce", "answer", "solution")), None)
        if not seed_field or not nonce_field or not fields[seed_field]:
            continue
        difficulty = int(difficulty_m.group(1))
        if not 1 <= difficulty <= MAX_DIFFICULTY:
            raise GateError(
                f"proof-of-work difficulty {difficulty} is outside what rvtree will spend "
                f"(1-{MAX_DIFFICULTY}); each step costs sixteen times the one before"
            )
        return Challenge(
            seed=fields[seed_field],
            difficulty=difficulty,
            action=urljoin(url, action),
            seed_field=seed_field,
            nonce_field=nonce_field,
        )
    return None


def solve(challenge: Challenge) -> str:
    """Find a nonce whose ``sha256(seed + nonce)`` starts with the required zeros.

    Straight-line and single-threaded on purpose. At the difficulties these gates use it
    finishes in well under a second, which is nothing beside one Tor round trip, and a
    thread pool here would only add a way to get the accounting wrong.
    """
    seed = challenge.seed.encode()
    target = challenge.target
    started = time.monotonic()
    nonce = 0
    while True:
        if hashlib.sha256(seed + str(nonce).encode()).hexdigest().startswith(target):
            log.debug(
                "solved difficulty %d in %d hashes (%.2fs)",
                challenge.difficulty,
                nonce,
                time.monotonic() - started,
            )
            return str(nonce)
        nonce += 1


def read_challenge_body(response) -> str:
    """Read a bounded prefix of a streamed response, for gate detection only.

    ``response.text`` on a body the server declared ``text/html`` would still be
    unbounded, and a gate is the one place where the thing on the other end has already
    proven it will hand you something other than what you asked for.
    """
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_content(16 * 1024):
        chunks.append(chunk)
        size += len(chunk)
        if size >= MAX_BODY:
            break
    encoding = response.encoding or "utf-8"
    return b"".join(chunks).decode(encoding, "replace")


def pass_gate(session, response, url: str, timeout, verify) -> Optional[str]:
    """Solve the challenge in ``response`` and return the URL it grants, if any.

    ``None`` means the response was not a gate and the caller should use it as is. The
    returned URL is single-use: it is the answer to *this* request, and the next one has
    to pay again.
    """
    if response.status_code != 200 or looks_like_payload(response.headers):
        return None
    challenge = detect(read_challenge_body(response), url)
    if challenge is None:
        return None

    log.info(
        "%s is behind a proof-of-work gate (difficulty %d); solving it",
        url,
        challenge.difficulty,
    )
    nonce = solve(challenge)
    posted = session.post(
        challenge.action,
        data={challenge.seed_field: challenge.seed, challenge.nonce_field: nonce},
        timeout=timeout,
        verify=verify,
        allow_redirects=False,
        stream=True,
    )
    try:
        location = posted.headers.get("Location")
        if posted.status_code not in (301, 302, 303, 307, 308) or not location:
            raise GateError(
                f"proof-of-work answer was rejected by {challenge.action} "
                f"(HTTP {posted.status_code}); the challenge may have expired"
            )
    finally:
        posted.close()
    granted = urljoin(challenge.action, location)
    log.info("gate passed; the entity is at %s", granted)
    return granted


def get(session, url: str, *, headers=None, timeout=None, verify=None, stream=True):
    """GET ``url``, passing any proof-of-work gate in the way.

    Returns the response for the *entity*, with its body untouched, and the URL it
    finally came from — which is not the URL asked for when a gate redirected.
    """
    current = url
    for _ in range(MAX_GATES + 1):
        response = session.get(
            current, headers=headers, stream=stream, timeout=timeout, verify=verify
        )
        try:
            granted = pass_gate(session, response, current, timeout, verify)
        except Exception:
            response.close()
            raise
        if granted is None:
            return response, current
        # The challenge page has been read to the end of what mattered; the connection
        # is only useful for the request that follows.
        response.close()
        current = granted
    raise GateError(f"{url} is behind more than {MAX_GATES} chained gates; giving up")
