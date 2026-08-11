import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

# Rebuild when any of these is missing, so a corpus built by an older revision of
# make_fixtures.sh picks up the files added since. The self-signed certificate is
# deliberately not listed: it needs openssl, and a host without it would rebuild the
# whole corpus on every run. The TLS tests skip instead.
REQUIRED = ("test.tar", "test.tar.gz", "test.rar4.rar", "test.rar5.rar")


@pytest.fixture(scope="session")
def fixtures() -> str:
    if not all(os.path.isfile(os.path.join(FIXTURES, f)) for f in REQUIRED):
        subprocess.run(
            ["bash", os.path.join(os.path.dirname(FIXTURES), "make_fixtures.sh")],
            check=True,
            capture_output=True,
        )
    return FIXTURES


@pytest.fixture
def server(fixtures):
    from rangeserver import RangeServer

    with RangeServer(fixtures) as srv:
        yield srv


@pytest.fixture(params=["classic", "pooled"])
def transport(request):
    """Every transport-facing test runs against both engines.

    A ``PooledTransport`` with one lane has nothing to split a range across and nothing to
    race a small read against, so both regimes disable themselves and it issues exactly
    the request sequence ``Transport`` does. Running the existing byte budgets and request
    counts against it unchanged is the cheapest proof that the new engine is a drop-in.
    """
    if request.param == "classic":
        from rvtree.transport import Transport

        t = Transport(proxy=None, circuits=1)
    else:
        from rvtree.transport import PooledTransport

        t = PooledTransport(endpoints=None, circuits_per_endpoint=1)
    yield t
    t.close()


@pytest.fixture
def pooled():
    """A genuinely multi-lane pool over direct connections, for the pooling tests."""
    from rvtree.transport import PooledTransport

    t = PooledTransport(endpoints=None, circuits_per_endpoint=8)
    yield t
    t.close()
