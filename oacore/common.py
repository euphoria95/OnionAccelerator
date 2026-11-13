"""Helpers the download modes, the size probe, the farm and the CLI all lean on.

User-agent loading, URL-to-local-path mapping, the SOCKS proxy dict, tor's
ExtendedErrors replies, and local SOCKS port discovery. Moved out of
OnionAccelerator.py unchanged.
"""

import logging
import os
import random
import re
import socket
from urllib.parse import unquote, urlparse

import requests.packages.urllib3
from requests.packages.urllib3.exceptions import InsecureRequestWarning

from .config import BASE_PORT, MAX_WORKERS, USERAGENTS_FILE

# TLS verification is off by design (an onion address authenticates the peer), so the
# warning urllib3 prints for every unverified request is noise. Silenced here rather
# than only in OnionAccelerator.py so that importing oacore on its own is quiet too.
requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)

# One logger for the whole program, configured by OnionAccelerator.setup_logging().
logger = logging.getLogger("OnionAccelerator")


# Tor's ExtendedErrors SOCKS replies, for the download modes' log lines. The crawler's
# copy, crawler.fetcher.TOR_SOCKS_REPLIES, is the one that decides retries; it cannot be
# imported from here without dragging aiohttp into --farm. tests/test_farm_tune.py keeps the
# two equal.
TOR_SOCKS_REPLIES = {
    0xF0: "onion descriptor not found",
    0xF1: "onion descriptor invalid",
    0xF2: "onion introduction failed",
    0xF3: "onion rendezvous failed",
    0xF4: "onion client authorization missing",
    0xF5: "onion client authorization wrong",
    0xF6: "invalid onion address",
    0xF7: "onion introduction timed out",
}

# =========== UTILS ===========

def load_user_agents(filepath=USERAGENTS_FILE):
    """
    Load user-agent strings from a TSV file (first column). Fallback if missing.
    """
    if not os.path.isfile(filepath):
        logger.warning(f"UserAgents file not found: {filepath}. Using fallback.")
        return [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Mozilla/5.0 (X11; Linux x86_64)"
        ]
    agents = []
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            # TSV: first column is the UA string, remaining columns are metadata
            parts = line.strip().split('\t')
            ua = parts[0].strip()
            if ua:
                agents.append(ua)
    if not agents:
        agents = ["Mozilla/5.0 (Windows NT 10.0; Win64; x64)"]
    return agents


def random_user_agent(uas):
    return random.choice(uas)


def url_to_local_path(url: str, base_dir: str, preserve_path: bool = False) -> str:
    """
    Derive a collision-free local path from a URL using hostname as a subdirectory.
    e.g. http://example.onion/file.zip -> base_dir/example.onion/file.zip

    With `preserve_path`, the URL's directories are mirrored under the hostname:
    http://example.onion/a/b/file.zip -> base_dir/example.onion/a/b/file.zip. The
    hostname alone is enough for a hand-written URL list, where two entries rarely
    share a basename, but not for a crawl: an open directory routinely holds a
    README.txt in every subdirectory, and flattening those would have each overwrite
    the last. `--mode crawl --download` always passes True; multi and partial mode
    take it from --preserve-path and default to the flat layout, which
    _warn_on_path_collisions() counts the cost of before a run starts.
    """
    parsed = urlparse(url)
    host = parsed.netloc.replace(":", "_") or "unknown"
    filename = os.path.basename(parsed.path) or "index.html"
    if not preserve_path:
        return os.path.join(base_dir, host, filename)

    # Decoded before the split, for the same reason crawler.urlnorm.path_segments is:
    # a file manager addresses a nested directory by encoding the separator into one
    # segment, and `/TOK/dumps%2Fraw/part.bin` is three levels, not two. Splitting first
    # would mirror it as a single directory named "dumps_raw" sitting beside the "dumps"
    # its own subdirectories were written into, flattening the tree being preserved.
    #
    # Sanitise every component afterwards: a path traversal in a URL served by a hostile
    # onion must not be able to write outside base_dir. Decoding first cannot weaken
    # that -- it is what exposes an encoded `..` to the check, rather than hiding it
    # inside a segment that is sanitised as one opaque name.
    parts = [_safe_path_component(p) for p in unquote(parsed.path).split("/") if p]
    parts = [p for p in parts if p]
    if not parts:
        parts = ["index.html"]
    return os.path.join(base_dir, host, *parts)


def _safe_path_component(component: str) -> str:
    """One URL path segment, made safe to use as a filename."""
    if component in (".", ".."):
        return ""
    cleaned = re.sub(r'[\x00-\x1f/\\:*?"<>|]', "_", component).strip(". ")
    return cleaned[:120]


def make_proxies(proxy: str) -> dict:
    """
    Build a SOCKS5 proxy dict for a 'host:port' endpoint (local or remote).
    Accept any cert (verify=False), resolve DNS via Tor (socks5h).
    """
    return {
        "http": f"socks5h://{proxy}",
        "https": f"socks5h://{proxy}",
    }


_TOR_REPLY_RE = re.compile(r"\b0x(f[0-7])\b", re.IGNORECASE)


def explain_error(exc):
    """`exc` as text, with an ExtendedErrors reply translated.

    PySocks has no names for tor's onion codes and says '0xf0: Unknown error', which
    reads like a broken proxy when it means the onion's descriptor is gone.
    """
    text = str(exc)
    m = _TOR_REPLY_RE.search(text)
    if m:
        text += f" (tor: {TOR_SOCKS_REPLIES[int(m.group(1), 16)]})"
    return text



# =========== LOCAL PORT DISCOVERY ===========

PORT_PROBE_TIMEOUT = 0.3       # seconds, per-port local liveness probe

def _port_is_open(host, port, timeout=PORT_PROBE_TIMEOUT):
    """Return True if a TCP connect to host:port succeeds within `timeout`."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def discover_local_socks_ports(base_port=BASE_PORT, max_ports=MAX_WORKERS, host="127.0.0.1"):
    """
    Probe host:base_port .. host:base_port+max_ports-1 and return the
    'host:port' endpoints that actually accept a TCP connection.

    This makes local mode adapt to however many SOCKS proxies are really up —
    a native farm of any size (see --farm) or legacy Docker instances — instead
    of blindly assuming exactly MAX_WORKERS are listening.
    """
    live = []
    for i in range(max_ports):
        port = base_port + i
        if _port_is_open(host, port):
            live.append(f"{host}:{port}")
    return live
