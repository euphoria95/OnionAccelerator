"""Two real tor instances through the process backend. Network: run with `-m network`.

Everything else about the process farm is tested against tests/stubtor.py, which proves
the farm speaks the control protocol the way the spec says. It cannot prove that the real
tor accepts this torrc, writes the cookie where the farm looks for it, or answers
GETINFO status/bootstrap-phase the way the spec says it does. This test does, and it is
the only one that bootstraps against the live Tor network, so it is excluded by default
and never run by a coding session unless asked.

It uses a private state directory and ports it checks are free first, and removes what it
made; it needs a `tor` binary on PATH and skips without one.
"""

import os
import shutil

import pytest

from oacore import farm_process
from test_farm_process import free_block, port_open

pytestmark = pytest.mark.network


@pytest.mark.skipif(not shutil.which("tor"), reason="needs a tor binary on PATH")
def test_two_real_instances_bootstrap_and_are_torn_down(monkeypatch, tmp_path):
    # conftest.py points both at nothing for every test; this one means it.
    monkeypatch.setenv("ONIONACCELERATOR_FARM_DIR", str(tmp_path / "s"))
    monkeypatch.delenv("ONIONACCELERATOR_TOR")
    base = free_block(2)
    backend = farm_process.ProcessBackend()
    try:
        done = backend.up(count=2, base_port=base, timeout=240)
        assert done == {"oa00", "oa01"}, "tor did not bootstrap in time"
        assert port_open(base) and port_open(base + 1)
        for row in backend.status(base):
            assert row["bootstrap"] == 100 and row["ok"], row
            assert row["circuits_built"] >= 1, row
    finally:
        backend.destroy()
    assert not port_open(base) and not os.path.exists(tmp_path / "s")
