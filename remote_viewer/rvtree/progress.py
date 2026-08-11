"""Live progress display, for runs that would otherwise be silent for minutes.

Two diagnostic channels leave this package and they are deliberately separate. Text goes
through stdlib ``logging`` (``-v``, ``-vv``, ``--debug``), pushed from wherever a fact is
known. The bar here *polls* instead: on a timer it samples the transport's byte and
request counters, the walker's stream position, and an entry tally. That keeps the
parsers almost free of instrumentation, and keeps the cost on the hot path — one call
per archive entry, per xz block, per RAR header — down to an integer increment.

Everything is written to stderr. stdout carries payload — ndjson entries, extracted
member bytes — and must stay byte-identical whether or not a bar is drawn.
"""

from __future__ import annotations

import collections
import contextlib
import logging
import os
import shutil
import sys
import threading
import time
from typing import Callable, Iterator, Optional

from .util import human_bytes

# Stage announcements are logged under their own name so ``-v`` can show the pipeline
# without the per-request detail that ``-vv`` adds.
_stage_log = logging.getLogger("rvtree.stage")

REDRAW_INTERVAL = 0.125  # 8 Hz: fast enough to look alive, cheap enough to ignore
PLAIN_INTERVAL = 2.0  # --progress always without a terminal: one line every so often
RATE_WINDOW = 4.0  # seconds of history behind the reported throughput
MIN_TWO_LINE_WIDTH = 60
BAR_CELLS = 22


def _isatty(stream) -> bool:
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def _dumb() -> bool:
    return os.environ.get("TERM", "") == "dumb"


def _term_width() -> int:
    return min(shutil.get_terminal_size((80, 24)).columns, 100)


def _glyphs(stream) -> tuple[str, str]:
    """Block-drawing characters, or ASCII when the terminal cannot encode them."""
    encoding = getattr(stream, "encoding", None) or "ascii"
    try:
        "█░".encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return "#", "-"
    return "█", "░"


def _clock(seconds: float) -> str:
    seconds = int(max(0.0, seconds))
    if seconds < 3600:
        return f"{seconds // 60}:{seconds % 60:02d}"
    return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


def _clip(text: str, width: int) -> str:
    if width <= 1 or len(text) <= width:
        return text
    return text[: max(1, width - 2)] + "…"


class Reporter:
    """Stage and progress display on stderr; safe to call from any thread.

    Disabled instances answer every method and do nothing, so callers never branch.
    """

    def __init__(
        self,
        stream=None,
        enabled: bool = True,
        ansi: Optional[bool] = None,
        color: bool = True,
        interval: float = REDRAW_INTERVAL,
    ):
        self.stream = stream if stream is not None else sys.stderr
        self.enabled = enabled
        self._ansi = _isatty(self.stream) and not _dumb() if ansi is None else ansi
        self._color = color and self._ansi and not os.environ.get("NO_COLOR")
        self._solid, self._empty = _glyphs(self.stream)
        self._interval = interval if self._ansi else PLAIN_INTERVAL
        # Re-entrant: a log record emitted while composing would re-enter through the
        # bar-aware handler's suspended() call.
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._painted = 0
        self._started = time.monotonic()
        self._last_plain = 0.0
        self._samples: deque = collections.deque()

        # Sampled state. Plain attributes on purpose: the writers are on the hot path
        # and an int store is atomic, so none of them needs the lock.
        self._transport = None
        self._poll: Optional[Callable[[], int]] = None
        self._stage = ""
        self._detail = ""
        self._pos = 0
        self._high = 0
        self._total = 0
        self._unit = "bytes"
        self._block = -1
        self._blocks = 0
        self.entries = 0

    # -- what the rest of the package calls ------------------------------------
    def attach(self, transport) -> None:
        """Read bytes, requests and throughput from the transport's own counters."""
        self._transport = transport

    def stage(self, name: str, detail: str = "") -> None:
        """Announce a pipeline step. Logged at INFO *and* shown on the bar."""
        # Padded to the width the console formatter gives a logger name, so stage lines
        # and module lines share one column under -v.
        _stage_log.info("%-15s %s", name, detail) if detail else _stage_log.info("%s", name)
        with self._lock:
            self._stage = name
            self._detail = detail
            self._poll = None
            self._pos = self._high = self._total = 0
            self._block = -1
            self._blocks = 0

    def track(self, stream, total: int, unit: str = "bytes") -> None:
        """Follow a walker by polling ``stream.tell()`` against a known total."""
        self.track_bytes(stream.tell, total, unit=unit)

    def track_bytes(self, poll: Callable[[], int], total: int, unit: str = "bytes") -> None:
        """Follow anything that can answer "how far" without doing I/O.

        The no-I/O rule is the important one: this runs eight times a second, and a poll
        that touched the network would turn a cosmetic bar into real Tor traffic.
        """
        with self._lock:
            self._poll = poll
            self._total = max(0, total)
            self._unit = unit

    def position(self, pos: int, total: Optional[int] = None) -> None:
        """Push a position, for walkers whose progress is not their stream's ``tell()``."""
        self._pos = pos
        if pos > self._high:
            self._high = pos
        if total:
            self._total = total

    def block(self, index: int, count: int) -> None:
        self._block = index
        self._blocks = count

    def entry(self, n: int = 1) -> None:
        self.entries += n

    # -- lifecycle -------------------------------------------------------------
    def start(self) -> "Reporter":
        if not self.enabled or self._thread is not None:
            return self
        self._started = time.monotonic()
        self._thread = threading.Thread(target=self._run, name="rvtree-progress", daemon=True)
        self._thread.start()
        return self

    def close(self) -> None:
        """Stop and leave the cursor on a clean line, whatever happens next."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=1.0)
        with self._lock:
            self._erase()
            self.enabled = False

    @contextlib.contextmanager
    def suspended(self) -> Iterator[None]:
        """Clear the bar for the duration, so a log line lands on an empty row.

        The lock is held throughout: that is what stops the renderer thread painting
        over a half-written record.
        """
        with self._lock:
            self._erase()
            yield  # the next tick repaints

    # -- rendering -------------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self._draw()

    def _draw(self) -> None:
        with self._lock:
            if not self.enabled or self._stop.is_set():
                return
            self._sample()
            lines = self._compose(_term_width())
            if self._ansi:
                self._paint(lines)
            else:
                now = time.monotonic()
                if now - self._last_plain >= PLAIN_INTERVAL:
                    self._last_plain = now
                    self._write("".join(line + "\n" for line in lines))

    def _sample(self) -> None:
        poll = self._poll
        if poll is None:
            return
        try:
            pos = poll()
        except Exception:  # noqa: BLE001 - a closed stream must not kill the display
            return
        self._pos = pos
        if pos > self._high:
            # tarfile seeks backwards for pax and GNU continuation records; a bar that
            # goes into reverse looks like a bug, so only the high-water mark is shown.
            self._high = pos

    def _compose(self, width: int) -> list[str]:
        """Plain text first, colour last: an escape sequence must never eat the budget."""
        head, tail = self._head(), self._tail()
        if not self._ansi or width < MIN_TWO_LINE_WIDTH:
            return [_clip(f"{head}  {tail.strip()}", width)]
        return [self._emphasise(_clip(head, width)), self._dim(_clip(tail, width))]

    def _emphasise(self, line: str) -> str:
        """Bold the stage name, which is always the first token of the line."""
        if not self._color:
            return line
        name, sep, rest = line.partition("  ")
        return f"\x1b[1m{name}\x1b[0m{sep}{rest}"

    def _head(self) -> str:
        bits = [self._stage or "working"]
        if self._blocks:
            bits.append(f"block {self._block + 1}/{self._blocks}")
        elif self._detail:
            bits.append(self._detail)
        if self._total and self._unit == "bytes":
            bits.append(f"{human_bytes(self._high)} / {human_bytes(self._total)}")
        line = "  ".join(bits)
        frac = self._fraction()
        if frac is None:
            return f"{line}  {self._marquee()}"
        return f"{line}  {self._bar(frac)} {frac * 100:3.0f}%"

    def _tail(self) -> str:
        parts = []
        transport = self._transport
        if transport is not None:
            fetched = self._fetched()
            parts.append(f"{human_bytes(fetched)} fetched")
            parts.append(f"{transport.requests_made} req")
            rate = self._rate(fetched)
            if rate is not None:
                parts.append(f"{human_bytes(rate)}/s")
        if self.entries:
            parts.append(f"{self.entries:,} entries")
        elapsed = time.monotonic() - self._started
        parts.append(_clock(elapsed))
        frac = self._fraction()
        if frac is not None and frac > 0.02 and elapsed > 2:
            parts.append(f"ETA {_clock(elapsed * (1 - frac) / frac)}")
        return "  " + " · ".join(parts)

    def _fetched(self) -> int:
        """Bytes down the wire, counting the transfer currently in flight.

        ``bytes_fetched`` only moves when a request completes, and a single 4 MiB block
        over Tor takes seconds — long enough for a bar built on it alone to look hung,
        which is the exact complaint this display exists to answer.
        """
        transport = self._transport
        return transport.bytes_fetched + getattr(transport, "bytes_inflight", 0)

    def _rate(self, fetched: int) -> Optional[float]:
        """Bytes per second over the last few seconds, not over the whole run.

        A run-long average keeps reporting the speed of a burst that finished a minute
        ago; what a reader wants to know is whether anything is moving *now*.
        """
        now = time.monotonic()
        self._samples.append((now, fetched))
        while len(self._samples) > 1 and now - self._samples[0][0] > RATE_WINDOW:
            self._samples.popleft()
        first_t, first_bytes = self._samples[0]
        span = now - first_t
        if span < 0.5:
            transport = self._transport
            return transport.throughput if transport.bytes_fetched else None
        return (fetched - first_bytes) / span

    def _fraction(self) -> Optional[float]:
        if self._total > 0:
            return min(1.0, self._high / self._total)
        if self._blocks:
            return min(1.0, (self._block + 1) / self._blocks)
        return None

    def _bar(self, frac: float) -> str:
        filled = int(round(frac * BAR_CELLS))
        return f"[{self._solid * filled}{self._empty * (BAR_CELLS - filled)}]"

    def _marquee(self) -> str:
        """Indeterminate work still has to look alive: a 7z header read, or unrar."""
        span = BAR_CELLS - 3
        at = int((time.monotonic() - self._started) * 8) % (2 * span)
        at = at if at < span else 2 * span - at
        return f"[{self._empty * at}{self._solid * 3}{self._empty * (span - at)}]"

    def _dim(self, text: str) -> str:
        return f"\x1b[2m{text}\x1b[0m" if self._color else text

    def _paint(self, lines: list[str]) -> None:
        buf = [f"\x1b[{self._painted}A"] if self._painted else []
        for line in lines:
            buf.append("\r\x1b[2K" + line + "\n")
        self._painted = len(lines)
        self._write("".join(buf))

    def _erase(self) -> None:
        if not self._painted:
            return
        n, self._painted = self._painted, 0
        self._write(f"\x1b[{n}A" + "\r\x1b[2K\n" * n + f"\x1b[{n}A")

    def _write(self, text: str) -> None:
        try:
            self.stream.write(text)
            self.stream.flush()
        except Exception:  # noqa: BLE001 - a broken stderr must not end the run
            self.enabled = False


# The reporter is module-level rather than threaded through every parser, for the same
# reason ``logging`` is: reaching ``xz._plaintext`` or ``rar.walk`` from the CLI would
# otherwise mean a new keyword on a dozen signatures and every call site in tests/, to
# carry something a single-job process only ever has one of. Format modules pay one
# attribute lookup on a disabled instance when nothing is switched on.
_NULL = Reporter(enabled=False, ansi=False)
_active: Reporter = _NULL


def get() -> Reporter:
    """The reporter for this run, or a disabled one when nothing is active."""
    return _active


def make(when: str = "auto", quiet: bool = False, stream=None) -> Reporter:
    """Build the reporter the CLI flags ask for.

    ``auto`` draws only into a real terminal: stderr is where the summary and the error
    messages go, and filling a redirected log with cursor movements would ruin both.
    """
    stream = stream if stream is not None else sys.stderr
    interactive = _isatty(stream) and not _dumb()
    if quiet or when == "never":
        enabled = False
    elif when == "always":
        enabled = True
    else:
        enabled = interactive
    return Reporter(stream=stream, enabled=enabled, ansi=interactive)


@contextlib.contextmanager
def activate(reporter: Reporter) -> Iterator[Reporter]:
    global _active
    previous, _active = _active, reporter
    reporter.start()
    try:
        yield reporter
    finally:
        reporter.close()
        _active = previous
