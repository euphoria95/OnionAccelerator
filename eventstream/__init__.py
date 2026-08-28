"""Live event streaming for a run in progress.

A crawl of an open directory over Tor takes hours, and everything it learns is written
to disk where nobody can read it until it stops. This package is the other half: the run
publishes each fact as it learns it, and an external process reads them as they happen.

    python3 OnionAccelerator.py --mode crawl --url http://target.onion/ --stream &
    curl -sN 'http://127.0.0.1:8787/events?kinds=crawl.file&match=(?i)passw'

The intended consumer is a script somebody wrote for one incident: attach, grep for the
names in scope, and act on a hit at minute three instead of at hour four. See
contrib/streamhunt.py for one that does exactly that.

Three pieces, and the seam between them is deliberately narrow:

* `EventBus`  -- the sequence, the replay ring and the fan-out. A bus *is* a sink: it is
                 callable as ``bus(kind, data)``, so the producers in crawler/ and
                 remote_viewer/ take "something callable" and never import this package.
* `Selector`  -- what a consumer asked for, parsed from the query string.
* `StreamServer` -- the HTTP face: /events, /status, /schema, /health.

Standard library only. This has to run on the same bare host that `--farm` does, and a
diagnostic channel that can fail to import is a diagnostic channel nobody switches on.
"""

from .bus import (
    CRAWL_DIR,
    CRAWL_FAIL,
    CRAWL_FILE,
    CRAWL_PAGE,
    CRAWL_PROGRESS,
    CRAWL_SKIP,
    DEFAULT_BUFFER,
    DEFAULT_QUEUE,
    DETECT_RESULT,
    KINDS,
    RUN_START,
    RUN_STOP,
    STREAM_GAP,
    STREAM_HEARTBEAT,
    STREAM_HELLO,
    TREE_DONE,
    TREE_ENTRY,
    TREE_OPEN,
    Event,
    EventBus,
    Subscription,
)
from .filters import Selector, SelectorError
from .server import API_VERSION, DEFAULT_BIND, StreamServer, is_loopback, parse_bind

__all__ = [
    "API_VERSION",
    "CRAWL_DIR",
    "CRAWL_FAIL",
    "CRAWL_FILE",
    "CRAWL_PAGE",
    "CRAWL_PROGRESS",
    "CRAWL_SKIP",
    "DEFAULT_BIND",
    "DEFAULT_BUFFER",
    "DEFAULT_QUEUE",
    "DETECT_RESULT",
    "Event",
    "EventBus",
    "KINDS",
    "RUN_START",
    "RUN_STOP",
    "STREAM_GAP",
    "STREAM_HEARTBEAT",
    "STREAM_HELLO",
    "Selector",
    "SelectorError",
    "StreamServer",
    "Subscription",
    "TREE_DONE",
    "TREE_ENTRY",
    "TREE_OPEN",
    "is_loopback",
    "parse_bind",
]
