"""bench/onion.py: the one benchmark that needs Tor. Marked `network`: never run by default.

    BENCH_ONION_URL=http://YOURS.onion/test.bin pytest -m network tests/test_bench_onion.py

It needs an onion service you operate and a farm that is up. The test checks the table
carries its conditions and does not carry the address.
"""

import os

import pytest

from bench import onion


def test_the_table_never_contains_the_address_and_does_contain_the_conditions(capsys):
    """Offline: drive main() with a stand-in for the transfer, so the output can be checked."""
    url = "http://example-not-real.onion/test.bin"
    original = onion.one_run
    onion.one_run = lambda *a, **k: (2.0, 8 << 20, True)
    try:
        assert onion.main(["--url", url, "--lanes", "1,2", "--socks",
                           "127.0.0.1:1,127.0.0.1:2"]) == 0
    finally:
        onion.one_run = original
    out = capsys.readouterr()
    assert "example-not-real" not in out.out + out.err
    assert "<onion>" in out.out and "UTC" in out.out and "SOCKS endpoints" in out.out
    assert "| 2 | 1/1 |" in out.out


def test_it_says_what_is_missing_instead_of_guessing(capsys):
    assert onion.main(["--url", "http://x.test/f", "--lanes", "8", "--socks", "127.0.0.1:1"]) == 1
    assert "need 8 SOCKS endpoints" in capsys.readouterr().err


@pytest.mark.network
def test_a_real_transfer_through_a_farm():
    url = os.environ.get("BENCH_ONION_URL")
    if not url:
        pytest.skip("set BENCH_ONION_URL to an onion service you operate")
    assert onion.main(["--url", url, "--lanes", "1,4"]) == 0
