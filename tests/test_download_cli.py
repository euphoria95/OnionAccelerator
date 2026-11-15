"""How the command line reaches the download engine.

Nothing here resolves endpoints for real: this host (and any developer's) may have a live
farm on 127.0.0.1:5000-5019, and a test that let `main` probe it would be one step from
sending a request through it. Every edge -- endpoint discovery, the download functions,
the event bus -- is replaced, so what is left to assert is the wiring itself.
"""

import argparse
import types

import pytest

import OnionAccelerator as oa
from oacore.engine import DownloadResult


@pytest.mark.parametrize("text, expected", [
    ("4M", 4 * 1024 ** 2), ("4MiB", 4 * 1024 ** 2), ("512k", 512 * 1024),
    ("256M", 256 * 1024 ** 2), ("65536", 65536), ("1.5M", 1572864),
])
def test_piece_size_accepts_bytes_and_binary_suffixes(text, expected):
    assert oa.parse_piece_size(text) == expected


@pytest.mark.parametrize("text", ["", "fast", "-4M", "1K", "64K1", "257M", "1G", "2T"])
def test_piece_size_rejects_what_would_not_work(text):
    with pytest.raises(argparse.ArgumentTypeError):
        oa.parse_piece_size(text)


def test_the_defaults_are_the_documented_ones():
    args, _ = oa.build_parser().parse_known_args(["--mode", "partial"])
    assert args.piece_size == 4 * 1024 * 1024 and args.fresh is False


@pytest.fixture
def faked(monkeypatch, tmp_path):
    """main() with the network edges cut: returns what the download function received."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "URLs.txt").write_text("http://a.onion/x\nhttp://a.onion/y\n")
    seen = {}

    def endpoints(args, urls):
        seen["endpoint_args"] = (args.socks, args.external)
        return ["127.0.0.1:1", "127.0.0.1:2"]

    def fake_modes(name, ok=True):
        def run(urls, proxies, **kwargs):
            seen[name] = dict(kwargs, urls=urls, proxies=proxies)
            return [DownloadResult(url=u, path=u, ok=ok) for u in urls]
        return run

    monkeypatch.setattr(oa, "resolve_crawl_endpoints", endpoints)
    monkeypatch.setattr(oa, "multi_download_mode", fake_modes("multi"))
    monkeypatch.setattr(oa, "partial_download_mode", fake_modes("partial"))
    # setup_logging() adds file handlers per call; keep them out of the test session.
    monkeypatch.setattr(oa, "setup_logging", lambda: None)
    return types.SimpleNamespace(seen=seen, modes=fake_modes)


@pytest.mark.parametrize("mode", ["multi", "partial"])
def test_the_download_modes_get_endpoints_from_the_shared_ladder(faked, mode):
    oa.main(["--mode", mode, "--piece-size", "1M", "--fresh", "--circuits-per-endpoint", "3",
             "--retries", "5", "--preserve-path"])
    got = faked.seen[mode]
    assert got["proxies"] == ["127.0.0.1:1", "127.0.0.1:2"]
    assert got["piece_size"] == 1024 * 1024 and got["fresh"] is True
    assert got["circuits_per_endpoint"] == 3 and got["retries"] == 5
    assert got["preserve_path"] is True
    assert got["urls"] == ["http://a.onion/x", "http://a.onion/y"]
    assert "endpoint_args" in faked.seen


@pytest.mark.parametrize("mode", ["multi", "partial"])
def test_an_incomplete_file_makes_the_run_exit_nonzero(faked, monkeypatch, mode):
    monkeypatch.setattr(oa, f"{mode}_download_mode", faked.modes(mode, ok=False))
    with pytest.raises(SystemExit) as excinfo:
        oa.main(["--mode", mode])
    assert excinfo.value.code == 1


@pytest.mark.parametrize("mode", ["multi", "partial"])
def test_stream_publishes_the_transfer_through_the_bus(faked, monkeypatch, mode):
    events = []

    class Bus:
        def __call__(self, kind, data=None):
            events.append(kind)

    import contextlib

    @contextlib.contextmanager
    def streaming(args, name, target, status=None):
        events.append(("streaming", name, target))
        yield Bus()

    monkeypatch.setattr(oa, "streaming", streaming)
    oa.main(["--mode", mode, "--stream"])
    assert events == [("streaming", mode, ["http://a.onion/x", "http://a.onion/y"])]
    assert isinstance(faked.seen[mode]["sink"], Bus)


def test_crawl_download_hands_the_engine_its_flags_and_the_stream(monkeypatch, tmp_path):
    """`crawl --download` is the third door into the engine, and it has its own call site."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(oa, "JOB_ID", "job-under-test")     # main() sets it; crawl_mode reads it
    seen = {}

    class FakeConfig:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

        compile_filter = staticmethod(lambda patterns: None)

    fake_pkg = types.SimpleNamespace(
        CrawlConfig=FakeConfig,
        crawl=lambda config, proxies, agents, sink=None: (
            {"stopped_because": "exhausted", "totals": {"directories": 1}, "elapsed_s": 0.1},
            ["http://a.onion/d/f.bin"]),
        config=types.SimpleNamespace(URLS_FILE="urls.txt"),
    )
    monkeypatch.setattr(oa, "_import_crawler", lambda: fake_pkg)
    monkeypatch.setattr(oa, "multi_download_mode",
                        lambda urls, proxies, **kw: seen.update(kw, urls=urls, proxies=proxies))

    args, _ = oa.build_parser().parse_known_args(
        ["--mode", "crawl", "--download", "--piece-size", "1M", "--fresh",
         "--circuits-per-endpoint", "3", "--retries", "5"])
    published = []

    def sentinel(kind, data=None):
        published.append(kind)

    code = oa.crawl_mode(["http://a.onion/d/"], ["127.0.0.1:1"], args, sink=sentinel)

    assert code == 0
    assert seen["urls"] == ["http://a.onion/d/f.bin"] and seen["preserve_path"] is True
    assert seen["piece_size"] == 1024 * 1024 and seen["fresh"] is True
    assert seen["circuits_per_endpoint"] == 3 and seen["retries"] == 5
    assert seen["sink"] is sentinel
    assert published == ["run.stop"], "the run must end after its downloads, not before"
