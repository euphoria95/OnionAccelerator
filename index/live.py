"""The index, fed from a running job's event stream.

The same Ingester as the offline path, attached to `--stream` the way contrib/streamhunt.py
attaches a keyword matcher -- and it takes over the two things that script does that an
indexer must not get wrong:

* **Reconnecting from where it stopped.** A dropped socket resumes with ?since=<last seq>,
  so a laptop that slept through twenty minutes of a crawl still gets them, provided the
  run's replay buffer still holds them. A cursor belongs to one run: if a different run
  answers on the same port, it is started from its first event, because carrying seq 4812
  across would skip its first 4812 findings and report nothing missing.
* **Gaps.** A consumer that falls behind loses events, and the run says how many
  (`stream.gap`). An index with a silent hole in it is worse than none, so a gap is counted
  and, when the run's own output directory can be read -- named with `reconcile`, or
  announced by the run's /status -- that directory is ingested after the run stops. It is
  complete (the stream's contract is that `data` is the record on disk), and ingest is
  idempotent, so it fills exactly the holes. Without it the loss is reported loudly with
  the command that mends it; it is never swallowed.

Standard library only (urllib), so it runs on the analysis host without installing anything.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from .ingest import Ingester, ingest_dir
from .rules import RuleSet

logger = logging.getLogger("OnionAccelerator.index.live")

# Everything the index records; stream.* control lines are delivered whatever the filter.
KINDS = "crawl.dir,crawl.file,tree,run,download.done"
READ_TIMEOUT = 150.0           # the run heartbeats every 60 s by default; silence past 2.5x is a dead socket
DEFAULT_RETRIES = 10


class StreamError(RuntimeError):
    """The stream cannot be used: refused, unauthorised or unreachable."""


@dataclasses.dataclass
class LiveSummary:
    runs: list = dataclasses.field(default_factory=list)
    events: int = 0
    gaps: int = 0
    lost: int = 0
    reconnects: int = 0
    reconciled: list = dataclasses.field(default_factory=list)
    skipped: int = 0


def _request(url: str, token: Optional[str]) -> urllib.request.Request:
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    return req


def _events(base: str, selector: dict, token: Optional[str], since: Optional[int]):
    """Decoded events from ONE connection. Ends when it does; raises when it breaks.

    Reconnection is the caller's: it alone knows the cursor and whether the run changed.
    """
    query = dict(selector)
    if since is not None:
        query["since"] = str(since)
    url = f"{base.rstrip('/')}/events?{urllib.parse.urlencode(query)}"
    try:
        with urllib.request.urlopen(_request(url, token), timeout=READ_TIMEOUT) as response:
            for raw in response:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace").strip()[:200]
        raise StreamError(f"{url}: HTTP {exc.code}"
                          f"{' (a token is required: --token)' if exc.code in (401, 403) else ''}"
                          f": {detail}") from exc


def _out_dir(base: str, token: Optional[str]) -> Optional[str]:
    """Where the run says it writes its artefacts, from its /status."""
    try:
        with urllib.request.urlopen(_request(f"{base.rstrip('/')}/status", token),
                                    timeout=10) as response:
            run = json.load(response).get("run")
    except (OSError, ValueError, urllib.error.URLError):
        return None
    out = run.get("out_dir") if isinstance(run, dict) else None
    return out if isinstance(out, str) else None


def follow(ix, base_url: str, *, token: Optional[str] = None, rules: Optional[RuleSet] = None,
           reconcile: Optional[str] = None, keep_following: bool = False,
           since: Optional[int] = 0, poll: float = 3.0,
           retries: Optional[int] = None) -> LiveSummary:
    """Index a run as it happens, until it stops (or forever with `keep_following`)."""
    summary = LiveSummary()
    ing = Ingester(ix, rules)
    selector = {"kinds": KINDS}
    cursor, run_id, failures = since, None, 0
    run_gaps: dict = {}
    limit = DEFAULT_RETRIES if retries is None else retries
    last_flush = time.monotonic()
    try:
        while True:
            try:
                restart = False
                for event in _events(base_url, selector, token, cursor):
                    failures = 0
                    payload = event.get("data") if isinstance(event.get("data"), dict) else {}
                    # Findings carry the run on the envelope; the control lines do not, and
                    # stream.hello names it in its data -- which is the first line, and so
                    # the one that tells a gap or a stop which run it belongs to.
                    named = event.get("run") or payload.get("run")
                    named = named if isinstance(named, str) else None
                    if named and named != run_id:
                        if run_id is not None:
                            logger.warning("a different run (%s) is now on this stream; "
                                           "starting it from its first event", named)
                            cursor, restart = 0, True
                        run_id = named
                        if restart:
                            break
                    if isinstance(event.get("seq"), int):
                        cursor = event["seq"]
                    kind = event.get("kind", "")
                    data = payload
                    if kind == "stream.gap":
                        summary.gaps += 1
                        summary.lost += int(data.get("lost") or 0)
                        run_gaps[run_id] = run_gaps.get(run_id, 0) + int(data.get("lost") or 0)
                        logger.warning("lost %s event(s): this index fell behind the run; the "
                                       "run's own directory is complete", data.get("lost"))
                        continue
                    if kind.startswith("stream."):
                        continue
                    summary.events += 1
                    if not ing.apply(run_id or "", kind, data):
                        summary.skipped += 1
                    if run_id and run_id not in summary.runs and kind != "download.done":
                        summary.runs.append(run_id)
                    if time.monotonic() - last_flush > 2.0:
                        ing.flush()
                        last_flush = time.monotonic()
                    if kind == "run.stop":
                        ing.flush()
                        _mend(ix, base_url, token, reconcile, run_id, run_gaps, summary, rules)
                        if not keep_following:
                            return summary
                if not restart:
                    # The stream closed cleanly without a run.stop: the run is over (or the
                    # server went down). Either way there is nothing more to wait for.
                    if not keep_following:
                        ing.flush()
                        _mend(ix, base_url, token, reconcile, run_id, run_gaps, summary, rules)
                        return summary
                    time.sleep(poll)
            except (OSError, urllib.error.URLError) as exc:
                failures += 1
                summary.reconnects += 1
                ing.flush()
                if failures > limit:
                    raise StreamError(f"cannot connect to {base_url}: {exc}") from exc
                logger.warning("disconnected (%s); retrying in %.0fs from seq %s", exc, poll, cursor)
                time.sleep(poll)
    finally:
        ing.flush()


def _mend(ix, base: str, token, reconcile, run_id, run_gaps, summary, rules) -> None:
    """After a run that lost events, read its directory so the index has everything."""
    lost = run_gaps.get(run_id, 0)
    if not lost:
        return
    where = reconcile or _out_dir(base, token)
    if where and os.path.isdir(where):
        ingest_dir(ix, where, rules=rules)
        summary.reconciled.append(where)
        logger.info("mended %d lost event(s) from %s", lost, where)
    else:
        logger.error("run %s lost %d event(s) and its directory %s cannot be read here; "
                     "once it can, run:  --mode index ingest <that directory>  (idempotent)",
                     run_id, lost, where or "is unknown")
