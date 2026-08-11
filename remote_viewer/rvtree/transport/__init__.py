"""HTTP-over-Tor transport primitives."""

from .httpfile import HttpRangeFile
from .pooled import (
    MIN_SPLIT,
    Endpoint,
    LanePool,
    PooledTransport,
    load_user_agents,
    parse_endpoint,
    split_spans,
)
from .probe import Capabilities, probe
from .tor import DEFAULT_PROXY, DEFAULT_VERIFY, RangeNotHonoured, Transport, TransportError

__all__ = [
    "HttpRangeFile",
    "Capabilities",
    "probe",
    "DEFAULT_PROXY",
    "DEFAULT_VERIFY",
    "RangeNotHonoured",
    "Transport",
    "TransportError",
    "PooledTransport",
    "Endpoint",
    "LanePool",
    "MIN_SPLIT",
    "load_user_agents",
    "parse_endpoint",
    "split_spans",
]
