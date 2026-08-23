"""HTTP-over-Tor transport primitives."""

from .gate import Challenge, GateError
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
from .spoolfile import SpoolError, SpooledFile, spool
from .tor import (
    DEFAULT_PROXY,
    DEFAULT_VERIFY,
    GATE_HEADER,
    RangeNotHonoured,
    Stream,
    Transport,
    TransportError,
)

__all__ = [
    "HttpRangeFile",
    "Capabilities",
    "Challenge",
    "GateError",
    "GATE_HEADER",
    "Stream",
    "SpooledFile",
    "SpoolError",
    "spool",
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
