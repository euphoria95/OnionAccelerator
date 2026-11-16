"""The benchmark lab: content that is deterministic, and stubs that shape traffic as claimed.

A benchmark whose instrument is unchecked proves nothing, so the shaping is tested against
a stopwatch here. Nothing leaves loopback.
"""

import hashlib
import io
import tarfile
import time

import pytest
import requests

from bench import lab

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ----------------------------------------------------------------------------- content


def test_pattern_source_slices_agree_with_the_whole_and_blocks_never_repeat():
    src = lab.PatternSource(3 * lab.MIB + 12345, seed=5)
    whole = src.read(0, src.size)
    assert len(whole) == src.size
    for start, length in ((0, 10), (lab.MIB - 5, 100), (2 * lab.MIB + 7, lab.MIB), (src.size - 3, 50)):
        assert src.read(start, length) == whole[start:start + length]
    blocks = [whole[i * lab.MIB:(i + 1) * lab.MIB] for i in range(3)]
    assert len(set(blocks)) == 3, "a misplaced piece would be invisible if blocks repeated"
    assert hashlib.sha256(whole).hexdigest() == src.sha256()


def test_tar_source_is_a_real_tar_and_any_slice_matches_it():
    src = lab.TarSource(members=300, avg_size=2000, seed=3)
    whole = src.read(0, src.size)
    assert len(whole) == src.size and src.size % 512 == 0
    with tarfile.open(fileobj=io.BytesIO(whole)) as tf:
        names = [m.name for m in tf]
    assert len(names) == 300 and names[0] == "d0000/file0000000.bin"
    for start, length in ((0, 600), (511, 3), (1000, 5000), (src.size - 700, 700), (src.size - 5, 99)):
        assert src.read(start, length) == whole[start:start + length], (start, length)


# ----------------------------------------------------------------------------- the lab


def fetch(lab_, proxy, name="file.bin", rng=None):
    headers = {"Range": rng} if rng else {}
    proxies = {"http": f"socks5h://{proxy}"}
    t0 = time.perf_counter()
    r = requests.get(lab_.url(name), headers=headers, proxies=proxies, timeout=30)
    return r, time.perf_counter() - t0


def test_the_content_server_serves_ranges_and_counts_what_it_sent():
    with lab.Lab(file_mib=2, stubs=[{}]) as lb:
        r, _ = fetch(lb, lb.endpoints[0], rng="bytes=100-199")
        assert r.status_code == 206 and len(r.content) == 100
        assert r.headers["Content-Range"] == f"bytes 100-199/{2 * lab.MIB}"
        assert lb.stats() == {"requests": 1, "bytes_sent": 100}
        lb.reset()
        assert lb.stats() == {"requests": 0, "bytes_sent": 0}
        whole, _ = fetch(lb, lb.endpoints[0])
        assert hashlib.sha256(whole.content).hexdigest() == lb.info["sha256"]


def test_a_lab_hostname_is_never_resolved():
    """`bench.lab` is a label. The stub dials loopback whatever it is asked for."""
    with lab.Lab(file_mib=1, stubs=[{}], hash=False) as lb:
        r, _ = fetch(lb, lb.endpoints[0], rng="bytes=0-9")
        assert r.status_code == 206 and "bench.lab" in lb.url()


def test_a_per_connection_cap_limits_one_circuit():
    with lab.Lab(file_mib=4, stubs=[{"rate_mib": 8}, {}], hash=False) as lb:
        _, slow = fetch(lb, lb.endpoints[0])
        _, fast = fetch(lb, lb.endpoints[1])
    assert 0.4 < slow < 1.6, f"4 MiB at 8 MiB/s took {slow:.2f}s"
    assert fast < slow


def test_a_shared_cap_is_shared_by_every_connection_through_that_stub():
    """Two simultaneous downloads through one daemon take as long as both in series."""
    import concurrent.futures as cf
    with lab.Lab(file_mib=4, stubs=[{"shared_mib": 8}], hash=False) as lb:
        t0 = time.perf_counter()
        with cf.ThreadPoolExecutor(2) as ex:
            list(ex.map(lambda _: fetch(lb, lb.endpoints[0]), range(2)))
        both = time.perf_counter() - t0
    assert both > 0.8, f"8 MiB through an 8 MiB/s daemon took {both:.2f}s"


def test_connect_delay_and_rtt_are_charged_to_the_right_things():
    with lab.Lab(file_mib=1, stubs=[{"connect_delay": 0.4}, {"rtt": 0.3}, {}], hash=False) as lb:
        _, setup = fetch(lb, lb.endpoints[0], rng="bytes=0-9")
        _, rtt = fetch(lb, lb.endpoints[1], rng="bytes=0-9")
        _, plain = fetch(lb, lb.endpoints[2], rng="bytes=0-9")
    assert setup > 0.35 and rtt > 0.25 and plain < 0.25, (setup, rtt, plain)


def test_a_connection_is_reset_after_roughly_the_configured_bytes():
    with lab.Lab(file_mib=8, stubs=[{"fail_mean_mib": 0.5, "seed": 1}], hash=False) as lb:
        resets = 0
        for _ in range(6):
            try:
                r, _ = fetch(lb, lb.endpoints[0])
                resets += len(r.content) < 8 * lab.MIB
            except requests.RequestException:
                resets += 1
        assert resets >= 5, "an 8 MiB transfer should almost never survive a 0.5 MiB mean"
        assert lb.stub_stats()[0]["resets"] >= 5


def test_per_connection_credentials_are_recorded_so_circuit_isolation_is_visible():
    with lab.Lab(file_mib=1, stubs=[{}], hash=False) as lb:
        for user in ("a", "b", "c"):
            requests.get(lb.url(), headers={"Range": "bytes=0-9"}, timeout=30,
                         proxies={"http": f"socks5h://{user}:pw@{lb.endpoints[0]}"})
        assert lb.stub_stats()[0]["credentials"] == 3
