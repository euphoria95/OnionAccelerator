"""Replaying a previous crawl's frontier journal.

A crawl of a large tree over Tor is not one sitting. The onion services that serve
these trees go offline within the hour, and a crawl that restarts from the seed spends
that hour re-walking the shallow layers it already has -- so the second window covers
the same ground as the first, and the tree is never finished no matter how many windows
you get.

What a restart needs is the two facts no other output file records together: which
directories were already *settled* (so they are not fetched again), and which were
*discovered but never reached* (so they are queued first). `frontier.jsonl` is the
write-ahead journal of exactly that, appended and flushed per line, so it survives a
kill -9 as readily as a clean exit.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, NamedTuple, Optional

from .config import FRONTIER_FILE
from .listing.model import PageRequest

logger = logging.getLogger("OnionAccelerator.crawl.resume")


class ResumeState(NamedTuple):
    """What a previous run had queued and what it had finished."""

    admitted: list[dict[str, Any]]   # every directory the old frontier accepted, in order
    done: set[str]                   # those it settled: listed, skipped, or found to be a file

    @property
    def pending(self) -> list[dict[str, Any]]:
        """Admitted but never settled -- what the next run has to do.

        Give-ups are in here on purpose: a directory abandoned after exhausting its
        retries was never listed, and the next window is a fresh chance at it.
        """
        return [rec for rec in self.admitted if rec.get("url") not in self.done]


def journal_path(job_dir: str) -> str:
    return os.path.join(job_dir, FRONTIER_FILE)


def load(job_dir: str) -> ResumeState:
    """Read a previous run's frontier journal.

    A truncated last line is tolerated rather than fatal: the journal is flushed per
    line, but a process killed mid-write can still leave a partial one, and losing the
    whole resume over the final record would defeat the point.
    """
    path = journal_path(job_dir)
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"no frontier journal at {path} -- a crawl can only be resumed from a run "
            f"that wrote one (runs before --resume existed did not)"
        )

    admitted: list[dict[str, Any]] = []
    done: set[str] = set()
    seen_urls: set[str] = set()
    damaged = 0
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                damaged += 1
                continue
            url = record.get("url")
            if not url:
                damaged += 1
                continue
            if record.get("k") == "done":
                done.add(url)
            elif record.get("k") == "add":
                # The journal can legitimately hold a URL twice if a previous resume
                # replayed it. The frontier deduplicates anyway, but keeping the replay
                # single-copy keeps the new journal from doubling on every window.
                if url not in seen_urls:
                    seen_urls.add(url)
                    admitted.append(record)
            else:
                damaged += 1
    if damaged:
        logger.warning("frontier journal %s: skipped %d unreadable record(s)", path, damaged)
    return ResumeState(admitted=admitted, done=done)


def describe(state: ResumeState, job_dir: str) -> str:
    return (f"resuming from {job_dir}: {len(state.admitted)} directory(ies) known, "
            f"{len(state.done)} already done, {len(state.pending)} to crawl")


# ---------------------------------------------------------------- request round-trip


def request_to_dict(request: Optional[PageRequest]) -> Optional[dict[str, Any]]:
    """A PageRequest as JSON, or None for the plain-GET case that needs no record."""
    if request is None:
        return None
    return {
        "url": request.url,
        "method": request.method,
        "headers": [list(pair) for pair in request.headers],
        "body": request.body,
        "profile": request.profile,
        "path": request.path,
        "endpoint": request.endpoint,
    }


def request_from_dict(record: Optional[dict[str, Any]]) -> Optional[PageRequest]:
    """The inverse. Headers come back as the tuple-of-pairs that keeps it hashable."""
    if not record:
        return None
    return PageRequest(
        url=record["url"],
        method=record.get("method") or "GET",
        headers=tuple((str(k), str(v)) for k, v in record.get("headers") or ()),
        body=record.get("body"),
        profile=record.get("profile"),
        path=record.get("path"),
        endpoint=bool(record.get("endpoint")),
    )
