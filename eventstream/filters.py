"""What a consumer asked for, parsed out of the query string.

The whole point of putting a filter on the server side is that a hunt for one keyword
across a run that emits a hundred thousand events should not have to move a hundred
thousand events. `curl -sN '.../events?kinds=crawl.file&match=(?i)passw'` is the shape
this exists to support -- and a consumer that would rather grep the raw stream itself
simply passes none of it.

Every parse failure names the parameter and what was wrong with it, because the reader
of that message is writing a client against a live run and cannot afford to guess.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Mapping, Optional, Pattern, Sequence

from .bus import Event

# Shapes. `ndjson` is the API; `line` is the concession to the pipe -- just the URL or
# path, one per line, so a hunt can be handed straight to wget, a fetch queue or sort -u.
NDJSON = "ndjson"
LINE = "line"
SHAPES = (NDJSON, LINE)

# Seconds between keepalives on an idle stream. Long crawls go quiet -- a wide directory
# on a slow onion can take minutes -- and a consumer behind a proxy or a NAT that reaps
# idle connections would otherwise lose the stream and never know the run was still up.
DEFAULT_HEARTBEAT = 15.0
MIN_HEARTBEAT = 1.0

# Kind families that describe the stream itself or the run's lifecycle rather than
# anything the run found. Never filtered out: see Selector.wants().
CONTROL_FAMILIES = ("stream.", "run.")


class SelectorError(ValueError):
    """A malformed query. The server turns this into a 400 with the message intact."""


@dataclasses.dataclass(frozen=True)
class Selector:
    """One consumer's filter. Pure: no I/O, no state, trivially testable."""

    since: Optional[int] = None
    kinds: tuple[str, ...] = ()
    match: Optional[Pattern[str]] = None
    shape: str = NDJSON
    heartbeat: float = DEFAULT_HEARTBEAT

    @classmethod
    def from_query(cls, query: Mapping[str, Sequence[str]]) -> "Selector":
        """Build from urllib.parse.parse_qs output. Unknown parameters are an error.

        Rejecting them rather than ignoring them is deliberate: a typo in `kinds=` that
        silently returns everything looks exactly like a filter that matched everything,
        and an analyst would believe the second explanation.
        """
        unknown = set(query) - {"since", "kinds", "match", "shape", "heartbeat", "token"}
        if unknown:
            raise SelectorError(
                f"unknown parameter(s): {', '.join(sorted(unknown))}. "
                f"Accepted: since, kinds, match, shape, heartbeat, token.")

        return cls(
            since=_since(_one(query, "since")),
            kinds=_kinds(_one(query, "kinds")),
            match=_regex(_one(query, "match")),
            shape=_shape(_one(query, "shape")),
            heartbeat=_heartbeat(_one(query, "heartbeat")),
        )

    def wants(self, event: Event) -> bool:
        """Does this event pass the filter?

        Control events (stream.*) and the run's own lifecycle (run.*) are never filtered
        out. A consumer that asked for crawl.file still has to be told it lost seventeen
        of them, and still has to see the run start and end -- a filter is about the
        payload, not about the protocol. Without this a consumer written to the
        documented `kinds=crawl.file` never receives run.stop and cannot tell a finished
        run from a socket that went quiet.
        """
        if event.kind.startswith(CONTROL_FAMILIES):
            return True
        if self.kinds and not _in_family(event.kind, self.kinds):
            return False
        if self.match is not None and not self.match.search(event.line()):
            return False
        return True

    def render(self, event: Event) -> Optional[str]:
        """The event as this consumer wants to read it, or None to send nothing.

        `shape=line` drops events that name nothing -- heartbeats, progress counters --
        because a locator list with blank rows in it is a locator list nobody can pipe.
        """
        if self.shape == LINE:
            return event.locator()
        return event.line()

    def describe(self) -> dict[str, object]:
        """What the server understood, echoed back in stream.hello.

        A consumer that mistyped a regex and got zero hits can see, in its first line,
        exactly which filter the server is applying.
        """
        return {"since": self.since, "kinds": list(self.kinds),
                "match": self.match.pattern if self.match else None,
                "shape": self.shape, "heartbeat": self.heartbeat}


def _in_family(kind: str, wanted: Sequence[str]) -> bool:
    """`crawl` selects every crawl.* kind; `crawl.file` selects exactly one.

    Prefix matching is what lets a filter written today keep working when a kind is
    added tomorrow, which for a long-lived hunting script matters more than precision.
    """
    return any(kind == name or kind.startswith(name + ".") for name in wanted)


def _one(query: Mapping[str, Sequence[str]], name: str) -> Optional[str]:
    values = query.get(name)
    if not values:
        return None
    if len(values) > 1:
        raise SelectorError(f"{name} given {len(values)} times; give it once")
    value = values[0].strip()
    if not value:
        # '?kinds=' with nothing after it is a shell variable that did not expand, and
        # quietly reading it as "no filter" is how a hunt comes back empty-handed and
        # looks like a hunt that found nothing.
        raise SelectorError(f"{name} was given with no value; omit it entirely to "
                            f"leave it unset")
    return value


def _since(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    try:
        seq = int(value)
    except ValueError:
        raise SelectorError(f"since must be a sequence number, not {value!r}") from None
    if seq < 0:
        raise SelectorError("since cannot be negative; use since=0 for everything held")
    return seq


def _kinds(value: Optional[str]) -> tuple[str, ...]:
    if value is None:
        return ()
    names = tuple(name.strip() for name in value.split(",") if name.strip())
    if not names:
        raise SelectorError("kinds was empty; omit it to receive every kind")
    return names


def _regex(value: Optional[str]) -> Optional[Pattern[str]]:
    if value is None:
        return None
    try:
        return re.compile(value)
    except re.error as exc:
        raise SelectorError(f"match is not a valid regex: {exc}") from None


def _shape(value: Optional[str]) -> str:
    if value is None:
        return NDJSON
    if value not in SHAPES:
        raise SelectorError(f"shape must be one of {', '.join(SHAPES)}, not {value!r}")
    return value


def _heartbeat(value: Optional[str]) -> float:
    if value is None:
        return DEFAULT_HEARTBEAT
    try:
        seconds = float(value)
    except ValueError:
        raise SelectorError(f"heartbeat must be seconds, not {value!r}") from None
    if seconds < 0:
        raise SelectorError("heartbeat cannot be negative; use heartbeat=0 to disable it")
    if 0 < seconds < MIN_HEARTBEAT:
        return MIN_HEARTBEAT
    return seconds
