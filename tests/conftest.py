"""Path setup and shared fixtures for the OnionAccelerator tests.

OnionAccelerator.py lives at the repo root and rangeserver.py -- the range-capable local
server the rvtree suite already drives -- lives under remote_viewer/tests/, so both
directories have to be importable before anything here can run.

Importing OnionAccelerator is safe at collection time by design: its logger is configured
in setup_logging(), called from main(), precisely so that import creates no files and
writes no output.
"""

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RANGESERVER_DIR = os.path.join(ROOT, "remote_viewer", "tests")

for _path in (ROOT, RANGESERVER_DIR, HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import OnionAccelerator as _oa  # noqa: E402  (needs the sys.path above)

PAYLOAD_SIZE = 1048576


@pytest.fixture
def oa(monkeypatch):
    """OnionAccelerator with the SOCKS proxy stripped out.

    Every probe routes through make_proxies(), so replacing it with an empty dict is
    the whole of what it takes to point the ladder at a local server instead of Tor.
    """
    monkeypatch.setattr(_oa, "make_proxies", lambda proxy: {})
    return _oa


@pytest.fixture
def payload_dir(tmp_path):
    """A directory holding one file of a known size, for RangeServer to serve."""
    (tmp_path / "file.zip").write_bytes(b"x" * PAYLOAD_SIZE)
    return str(tmp_path)
