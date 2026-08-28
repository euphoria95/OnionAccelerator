"""The event bus: one run's facts, in order, fanned out to whoever is listening.

A crawl over Tor is slow enough that its findings are worth reading while it runs, not
only after it stops. This is the seam that makes that possible: every producer publishes
the record it was going to write to disk anyway, and any number of consumers read the
same sequence back.

Two rules shape everything here.

**Publishing never blocks.** The bus is called from the crawl's event loop and from
rvtree's walker threads, on the same code path that is otherwise fetching bytes over
Tor. A consumer that stops reading -- a grep paused with Ctrl-S, an indexer wedged on a
slow disk -- must cost the run nothing. So `publish` takes a lock only long enough to
stamp a sequence number and hand out references, and every queue in the system is
bounded.

**Loss is reported, never silent.** The price of bounded queues is that a consumer which
falls far enough behind loses events. For incident response a keyword index with an
unannounced hole in it is worse than no index, so a consumer that lost records is told
exactly how many, and can go back to the JSONL on disk -- which is complete -- to fill
the gap.
"""

from __future__ import annotations

import collections
import json
import threading
import time
from typing import Any, Callable, Optional

# ---------------------------------------------------------------- the kinds

# The API contract. Namespaced with dots so a consumer can select a whole family with
# `kinds=crawl` and not have to enumerate what a crawl can emit -- which also means a
# kind added here does not break a filter someone already wrote.
STREAM_HELLO = "stream.hello"
STREAM_GAP = "stream.gap"
STREAM_HEARTBEAT = "stream.heartbeat"
RUN_START = "run.start"
RUN_STOP = "run.stop"
CRAWL_DIR = "crawl.dir"
CRAWL_FILE = "crawl.file"
CRAWL_SKIP = "crawl.skip"
CRAWL_FAIL = "crawl.fail"
CRAWL_PROGRESS = "crawl.progress"
CRAWL_PAGE = "crawl.page"
TREE_OPEN = "tree.open"
TREE_ENTRY = "tree.entry"
TREE_DONE = "tree.done"
DETECT_RESULT = "detect.result"

# kind -> what it means, served verbatim by GET /schema. The point of publishing this is
# that a consumer can be written against the running server rather than against the
# README, and can tell a kind it has never seen from a kind it should ignore.
KINDS = {
    STREAM_HELLO: "First line on every connection: run metadata and the current cursor.",
    STREAM_GAP: "This consumer fell behind and lost events. 'lost' says how many.",
    STREAM_HEARTBEAT: "Idle keepalive, so a quiet run does not look like a dead socket.",
    RUN_START: "The run began. Carries the mode, the target(s) and the configuration.",
    RUN_STOP: "The run ended. Carries why it stopped and the final totals.",
    CRAWL_DIR: "One directory listed. The same record as a line of dirs.jsonl.",
    CRAWL_FILE: "One file discovered. The same record as a line of listing.jsonl.",
    CRAWL_SKIP: "A page that was fetched and read but is not a directory listing.",
    CRAWL_FAIL: "One failed attempt. The same record as a line of failed.jsonl.",
    CRAWL_PROGRESS: "Periodic counters: directories, files, queued, in flight, seen.",
    CRAWL_PAGE: "Fetched page text, truncated. Only with --stream-bodies.",
    TREE_OPEN: "A remote archive was opened: format, size, range support, gate.",
    TREE_ENTRY: "One member of a remote archive, as it is walked out of the headers.",
    TREE_DONE: "The archive walk finished: entries, bytes fetched, requests spent.",
    DETECT_RESULT: "Which listing templates read a target, and how well.",
}

# The replay ring, in events. Ten thousand is a few minutes of a fast crawl and costs a
# few megabytes of metadata; it exists so a consumer that attaches late, or reconnects
# after a dropped socket, can ask for what it missed instead of starting blind.
DEFAULT_BUFFER = 10_000
# Per consumer. Smaller on purpose: this is the depth at which a slow reader starts
# losing events, and a reader that is two thousand events behind is not going to catch
# up -- telling it so early is more useful than burying the fact under more memory.
DEFAULT_QUEUE = 2_000

# Fields that identify *the thing an event is about*, best first. Used by `shape=line`,
# which exists so a hunt can be piped straight into wget or a fetch queue.
LOCATOR_FIELDS = ("url", "path", "final_url", "target", "seed")


class Event:
    """One published fact, and its NDJSON line.

    The line is built once and shared by every consumer: with a dozen greps attached and
    `--stream-bodies` on, serialising per consumer would be the most expensive thing the
    bus does. `__slots__` for the same reason -- a long crawl holds ten thousand of these
    in the ring at a time.
    """

    __slots__ = ("seq", "ts", "run", "mode", "kind", "data", "_line")

    def __init__(self, seq: int, ts: str, run: str, mode: str, kind: str,
                 data: dict[str, Any]) -> None:
        self.seq = seq
        self.ts = ts
        self.run = run
        self.mode = mode
        self.kind = kind
        self.data = data
        self._line: Optional[str] = None

    def line(self) -> str:
        """The wire form: one JSON object, no newlines inside it.

        `default=str` and `ensure_ascii=False` match what crawler/report.py writes to
        disk, so a record that survived being written to listing.jsonl cannot fail here
        -- and a consumer diffing the stream against the file sees the same bytes.
        """
        if self._line is None:
            self._line = json.dumps(
                {"seq": self.seq, "ts": self.ts, "run": self.run, "mode": self.mode,
                 "kind": self.kind, "data": self.data},
                ensure_ascii=False, default=str, separators=(",", ":"),
            )
        return self._line

    def locator(self) -> Optional[str]:
        """What this event is about, as a bare string, or None if it is about nothing.

        Strings only. `run.start` carries a *list* of targets under 'target', and a
        stringified list in the middle of a `shape=line` body is a row that nothing
        downstream of the pipe can fetch -- naming nothing is the honest answer.
        """
        for field in LOCATOR_FIELDS:
            value = self.data.get(field)
            if isinstance(value, str) and value:
                return value
        return None

    def __repr__(self) -> str:                                   # pragma: no cover
        return f"<Event {self.seq} {self.kind} {self.locator() or ''}>"


class Subscription:
    """One consumer's view of the bus: a bounded queue and a loss counter.

    Every method that touches the queue does so under the *bus's* lock rather than one
    of its own. The producer appends and the consumer drains, and both need the length
    check and the counter bump to be atomic with respect to each other; sharing the one
    lock the producer already takes is cheaper than a second uncontended one.
    """

    __slots__ = ("_bus", "_lock", "_queue", "_wake", "lost", "delivered", "_closed",
                 "since", "attached")

    def __init__(self, bus: "EventBus", backlog: list[Event], maxlen: int,
                 lost: int, since: Optional[int]) -> None:
        self._bus = bus
        self._lock = bus._lock
        self._queue: collections.deque[Event] = collections.deque(backlog, maxlen)
        # A backlog longer than the queue is the same loss as falling behind live, and
        # is counted the same way: deque(iterable, maxlen) keeps the newest.
        self.lost = lost + max(0, len(backlog) - maxlen)
        self._wake = threading.Event()
        self.delivered = 0
        self._closed = False
        self.since = since
        self.attached = time.time()
        if backlog:
            self._wake.set()

    # -- producer side (called with the bus lock held) -------------------------
    def _offer(self, event: Event) -> None:
        queue = self._queue
        if len(queue) == queue.maxlen:
            # The append below evicts the oldest. Counting it here, rather than
            # discovering a hole later, is what makes stream.gap exact.
            self.lost += 1
        queue.append(event)
        self._wake.set()

    def _shutdown(self) -> None:
        self._closed = True
        self._wake.set()

    # -- consumer side ---------------------------------------------------------
    def take(self, timeout: Optional[float] = None) -> tuple[list[Event], int, bool]:
        """Wait for events. Returns (events, lost_before_them, closed).

        `lost` is the number of events dropped since the last call, and is what the
        server turns into a stream.gap line. It is reset by the act of reporting it.
        """
        if not self._queue and not self._closed:
            self._wake.wait(timeout)
        with self._lock:
            self._wake.clear()
            events = list(self._queue)
            self._queue.clear()
            lost, self.lost = self.lost, 0
            self.delivered += len(events)
            return events, lost, self._closed

    def close(self) -> None:
        """Detach. Safe to call twice, and safe to call from the consumer's thread."""
        self._bus.unsubscribe(self)
        self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed

    def stats(self) -> dict[str, Any]:
        return {"since": self.since, "delivered": self.delivered, "lost": self.lost,
                "queued": len(self._queue), "attached_s": round(time.time() - self.attached, 1)}


class EventBus:
    """The run's event sequence, and the consumers reading it.

    The bus is itself the sink the producers are handed: `bus(kind, data)` publishes.
    That is deliberate -- it means crawler/ and rvtree/ need nothing from this package
    beyond "something callable", the same way they take a connector factory and a
    transport, and neither grows an import of it.
    """

    def __init__(self, run: str, mode: str, *, buffer: int = DEFAULT_BUFFER,
                 queue: int = DEFAULT_QUEUE) -> None:
        self.run = run
        self.mode = mode
        self.started = time.time()
        self._lock = threading.Lock()
        self._ring: collections.deque[Event] = collections.deque(maxlen=max(1, buffer))
        self._queue_max = max(1, queue)
        self._subscribers: list[Subscription] = []
        self._seq = 0
        self.published = 0
        self.counts: dict[str, int] = collections.Counter()
        self._on_subscribe: Optional[Callable[[], None]] = None

    # -- producing -------------------------------------------------------------
    def publish(self, kind: str, data: Optional[dict[str, Any]] = None) -> Event:
        """Record one fact. Never blocks, never raises, never does I/O."""
        with self._lock:
            self._seq += 1
            event = Event(self._seq, _iso(time.time()), self.run, self.mode,
                          kind, data if data is not None else {})
            self._ring.append(event)
            self.published += 1
            self.counts[kind] += 1
            for subscriber in self._subscribers:
                subscriber._offer(event)
            return event

    # A bus *is* a sink: `sink=bus` at the call site, `sink(kind, data)` inside.
    __call__ = publish

    # -- consuming -------------------------------------------------------------
    def subscribe(self, since: Optional[int] = None) -> Subscription:
        """Attach a consumer. `since=N` replays everything after seq N that is still held.

        Registration and the backlog snapshot happen under one lock, which is what stops
        an event published mid-subscribe from being delivered twice or not at all.
        """
        with self._lock:
            backlog: list[Event] = []
            lost = 0
            if since is not None:
                backlog = [event for event in self._ring if event.seq > since]
                if self._ring and since < self._ring[0].seq - 1:
                    # Asked for events the ring has already evicted. Say so rather than
                    # quietly starting from the oldest one still held.
                    lost = self._ring[0].seq - 1 - since
            subscriber = Subscription(self, backlog, self._queue_max, lost, since)
            self._subscribers.append(subscriber)
            callback = self._on_subscribe
        if callback is not None:
            callback()
        return subscriber

    def unsubscribe(self, subscriber: Subscription) -> None:
        with self._lock:
            if subscriber in self._subscribers:
                self._subscribers.remove(subscriber)

    def on_subscribe(self, callback: Callable[[], None]) -> None:
        """Called once per new consumer. `--stream-wait` is the only user."""
        with self._lock:
            self._on_subscribe = callback

    def close(self) -> None:
        """End the stream: every consumer's next take() reports closed."""
        with self._lock:
            subscribers, self._subscribers = list(self._subscribers), []
        for subscriber in subscribers:
            subscriber._shutdown()

    # -- introspection ---------------------------------------------------------
    @property
    def seq(self) -> int:
        return self._seq

    @property
    def subscribers(self) -> int:
        return len(self._subscribers)

    def drained(self) -> bool:
        """Has every attached consumer been handed everything published?

        Asked at shutdown, so that a run.stop published a millisecond before the process
        exits still reaches the grep that was waiting for it.
        """
        with self._lock:
            return all(not s._queue for s in self._subscribers)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            oldest = self._ring[0].seq if self._ring else None
            return {
                "run": self.run,
                "mode": self.mode,
                "started_at": _iso(self.started),
                "uptime_s": round(time.time() - self.started, 1),
                "seq": self._seq,
                "published": self.published,
                "buffered": len(self._ring),
                "oldest_seq": oldest,
                "kinds": dict(self.counts),
                "consumers": [s.stats() for s in self._subscribers],
            }


def _iso(epoch: float) -> str:
    """UTC, to the second -- the same stamp crawler/report.py writes into its records."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))
