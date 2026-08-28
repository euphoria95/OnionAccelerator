"""The command line: --url, --full-help, and the parser they hang off.

Everything here is offline and needs no proxies. It reads the parser itself, and derives
its expectations from the exported tables (fullhelp.MODES, fullhelp.EXIT_CODES) rather
than from strings typed a second time -- the convention
remote_viewer/tests/test_cli_help.py set, for the reason it gives: a reference that
drifts is worse than none.

Any test that touches URLs.txt must chdir first. URLS_FILE is a relative path, so
without it the suite would read the repo's own list.
"""

from __future__ import annotations

import argparse
import ast
import os

import pytest

import fullhelp
import OnionAccelerator as oa

RVTREE_CLI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "remote_viewer", "rvtree", "cli.py")


def _parse(argv):
    """parse_known_args with main()'s '--' strip, so tests see what main() sees."""
    parser = oa.build_parser()
    args, rv_args = parser.parse_known_args(argv)
    if "--" in rv_args:
        rv_args.remove("--")
    return parser, args, rv_args


def _inject(argv):
    parser, args, rv_args = _parse(argv)
    return oa.inject_tree_url(parser, args, rv_args)


def _options(parser):
    """Every option string the parser accepts, minus the ones argparse supplies."""
    return [flag for action in parser._actions for flag in action.option_strings
            if flag not in ("-h", "--help")]


# ----------------------------------------------------------------- the parser seam


def test_build_parser_keeps_its_name_and_still_parses():
    """Extracting it from main() must not have changed what it accepts."""
    args, rv_args = oa.build_parser().parse_known_args(
        ["--mode", "partial", "--retries", "7", "--preserve-path", "--external"])
    assert (args.mode, args.retries, args.preserve_path, args.external) == \
        ("partial", 7, True, True)
    assert rv_args == []


def test_mode_choices_come_from_the_modes_table():
    """One table, so a new mode cannot appear in the parser without a help section."""
    mode = next(a for a in oa.build_parser()._actions if a.dest == "mode")
    assert tuple(mode.choices) == tuple(fullhelp.MODES)


def test_full_help_topics_are_the_modes_plus_the_farm():
    topic = next(a for a in oa.build_parser()._actions if a.dest == "full_help")
    assert tuple(topic.choices) == fullhelp.TOPICS
    assert fullhelp.TOPICS == tuple(fullhelp.MODES) + ("farm",)


def test_building_the_parser_needs_no_optional_dependencies(monkeypatch):
    """crawler_default() exists so --help survives a host with no aiohttp. Prove it."""
    def no_crawler(name, fromlist=(), *rest, **kwargs):
        if name.startswith("crawler"):
            raise ImportError("simulated: crawler dependencies absent")
        return real_import(name, fromlist, *rest, **kwargs)

    real_import = __import__
    monkeypatch.setattr("builtins.__import__",
                        lambda name, g=None, l=None, fromlist=(), level=0:
                        no_crawler(name, fromlist))
    assert oa.build_parser().parse_known_args(["--mode", "crawl"])[0].mode == "crawl"


def test_building_the_parser_writes_nothing(tmp_path, monkeypatch):
    """No log directory, no job id -- main() owns those, and only once it has to."""
    monkeypatch.chdir(tmp_path)
    oa.build_parser()
    assert os.listdir(tmp_path) == []


# ----------------------------------------------------------------- --url


def test_url_replaces_the_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / oa.URLS_FILE).write_text("http://stale.onion/old\n")
    _, args, _ = _parse(["--mode", "multi", "--url", "http://fresh.onion/new"])
    assert oa.resolve_urls(args) == ["http://fresh.onion/new"]


def test_url_needs_no_file_at_all(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _, args, _ = _parse(["--mode", "multi", "--url", "http://a.onion/x"])
    assert oa.resolve_urls(args) == ["http://a.onion/x"]


def test_url_is_repeatable_and_keeps_order_and_duplicates(tmp_path, monkeypatch):
    """URLs.txt preserves both, so --url has to as well: two copies is two downloads."""
    monkeypatch.chdir(tmp_path)
    _, args, _ = _parse(["--mode", "multi",
                         "--url", "http://b.onion/2",
                         "--url", "http://a.onion/1",
                         "--url", "http://b.onion/2"])
    assert oa.resolve_urls(args) == ["http://b.onion/2", "http://a.onion/1",
                                     "http://b.onion/2"]


def test_without_url_the_file_is_still_read(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / oa.URLS_FILE).write_text("http://a.onion/1\n\n  http://b.onion/2  \n")
    _, args, _ = _parse(["--mode", "multi"])
    assert oa.resolve_urls(args) == ["http://a.onion/1", "http://b.onion/2"]


def test_missing_file_without_url_still_exits_one(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _, args, _ = _parse(["--mode", "multi"])
    with pytest.raises(SystemExit) as excinfo:
        oa.resolve_urls(args)
    assert excinfo.value.code == 1


def test_empty_file_without_url_still_exits_zero(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / oa.URLS_FILE).write_text("\n\n   \n")
    _, args, _ = _parse(["--mode", "multi"])
    with pytest.raises(SystemExit) as excinfo:
        oa.resolve_urls(args)
    assert excinfo.value.code == 0


@pytest.mark.parametrize("value", ["example.onion/a.iso", "/etc/passwd", ""])
def test_url_without_a_scheme_is_rejected(value):
    """It would otherwise reach --external's liveness check and be shown to 100 proxies."""
    with pytest.raises(SystemExit) as excinfo:
        oa.build_parser().parse_known_args(["--mode", "multi", "--url", value])
    assert excinfo.value.code == 2


def test_url_is_stripped():
    _, args, _ = _parse(["--mode", "multi", "--url", "  http://a.onion/x  "])
    assert args.url == ["http://a.onion/x"]


def test_url_before_the_separator_does_not_eat_it():
    """'--url X -- list' must leave the separator and the rvtree arguments alone."""
    _, args, rv_args = _parse(["--mode", "tree", "--url", "http://a.onion/x",
                               "--", "list", "-f", "ndjson"])
    assert args.url == ["http://a.onion/x"]
    assert rv_args == ["list", "-f", "ndjson"]


# ----------------------------------------------------------------- tree injection


@pytest.mark.parametrize("rv_args,expected", [
    ([], ["list", "http://a.onion/x.rar"]),
    (["list"], ["list", "http://a.onion/x.rar"]),
    (["probe", "--multirange"], ["probe", "http://a.onion/x.rar", "--multirange"]),
    (["list", "-f", "ndjson"], ["list", "http://a.onion/x.rar", "-f", "ndjson"]),
    (["extract", "etc/hosts", "-o", "h"],
     ["extract", "http://a.onion/x.rar", "etc/hosts", "-o", "h"]),
])
def test_url_becomes_rvtrees_positional_after_the_subcommand(rv_args, expected):
    """extract is why this is an insert and not an append: it takes URL *then* PATH."""
    argv = ["--mode", "tree", "--url", "http://a.onion/x.rar"]
    if rv_args:
        argv += ["--"] + rv_args
    assert _inject(argv) == expected


def test_an_option_value_that_looks_like_a_url_is_not_a_collision():
    """rvtree's own --proxy takes a URL. Rejecting that command line would be wrong."""
    assert _inject(["--mode", "tree", "--url", "http://a.onion/x.rar",
                    "--", "list", "--proxy", "socks5://127.0.0.1:9050"]) == \
        ["list", "http://a.onion/x.rar", "--proxy", "socks5://127.0.0.1:9050"]


def test_the_url_stays_visible_to_the_liveness_target_search():
    """resolve_tree_endpoints() reads tree_urls() of the injected list, not of --url."""
    injected = _inject(["--mode", "tree", "--url", "http://a.onion/x.rar"])
    assert "http://a.onion/x.rar" in oa.tree_urls(injected)


@pytest.mark.parametrize("argv", [
    # the same URL given both ways
    ["--mode", "tree", "--url", "http://a.onion/x.rar", "--", "list", "http://b.onion/y"],
    # two archives, which rvtree cannot read in one run
    ["--mode", "tree", "--url", "http://a.onion/x", "--url", "http://b.onion/y",
     "--", "list"],
    # a flag where the subcommand belongs
    ["--mode", "tree", "--url", "http://a.onion/x", "--", "-v", "list"],
    # a subcommand rvtree does not have
    ["--mode", "tree", "--url", "http://a.onion/x", "--", "inspect"],
])
def test_ambiguous_tree_invocations_are_usage_errors(argv):
    with pytest.raises(SystemExit) as excinfo:
        _inject(argv)
    assert excinfo.value.code == 2


def test_tree_without_url_leaves_the_passthrough_untouched():
    assert _inject(["--mode", "tree", "--", "list", "http://a.onion/x.rar"]) == \
        ["list", "http://a.onion/x.rar"]


def test_tree_subcommands_match_rvtrees_own_modes():
    """The drift guard, read out of rvtree's source so it needs neither httpx nor a skip."""
    tree = ast.parse(open(RVTREE_CLI, encoding="utf-8").read())
    modes = next(ast.literal_eval(node.value) for node in tree.body
                 if isinstance(node, ast.Assign)
                 and any(getattr(t, "id", None) == "MODES" for t in node.targets))
    assert oa.TREE_SUBCOMMANDS == tuple(modes)


# ----------------------------------------------------------------- --full-help


def _reference():
    return fullhelp.render()


def test_every_option_of_the_parser_is_documented():
    """The guard that a flag added to the parser cannot go missing from the reference."""
    text = _reference()
    missing = [flag for flag in _options(oa.build_parser()) if flag not in text]
    assert not missing, f"absent from --full-help: {missing}"


def test_every_mode_is_named_with_its_summary():
    text = _reference()
    for mode, summary in fullhelp.MODES.items():
        assert f"--mode {mode}" in text
        assert summary in text


def test_every_mode_has_examples_you_could_paste():
    for mode in fullhelp.MODES:
        section = fullhelp.render(mode)
        assert "Examples" in section
        assert f"python3 OnionAccelerator.py --mode {mode}" in section


def _own_section(topic):
    """One topic's own block, without the common matter every topic carries."""
    return fullhelp.render(topic).split("Common to every mode", 1)[0]


def test_full_help_for_one_mode_shows_only_that_mode():
    crawl = _own_section("crawl")
    assert "--max-depth" in crawl
    assert "--preserve-path" not in crawl        # that one belongs to the download modes
    assert "--mode multi" not in crawl


def test_the_farm_gets_its_own_section():
    farm = _own_section("farm")
    for flag in ("--count", "--base-port", "--bootstrap-timeout"):
        assert flag in farm
    assert "--mode crawl" not in farm


def test_the_reference_wraps():
    """A reference that runs off the right of the terminal is one nobody reads."""
    for mode in (None,) + fullhelp.TOPICS:
        too_long = [line for line in fullhelp.render(mode).splitlines()
                    if len(line) > fullhelp.WIDTH]
        assert not too_long, f"{mode}: {too_long[:2]}"


def test_exit_codes_are_documented_and_match_the_source_of_truth():
    text = _reference()
    codes = text.split("Exit codes", 1)[1]
    for code, description in fullhelp.EXIT_CODES:
        assert f"  {code}\n" in codes, f"exit code {code} undocumented"
        assert description.split("\n")[0].rstrip() in " ".join(codes.split())
    assert {code for code, _ in fullhelp.EXIT_CODES} == {0, 1, 2}


def test_full_help_prints_the_reference_and_exits_zero(capsys):
    with pytest.raises(SystemExit) as excinfo:
        oa.build_parser().parse_known_args(["--full-help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    for mode in fullhelp.MODES:
        assert f"--mode {mode}" in out


def test_full_help_needs_neither_mode_nor_farm(capsys):
    """It fires during parsing, so main()'s 'exactly one of --farm or --mode' never runs."""
    with pytest.raises(SystemExit) as excinfo:
        oa.build_parser().parse_known_args(["--full-help", "tree"])
    assert excinfo.value.code == 0
    assert "--mode tree" in capsys.readouterr().out


def test_full_help_does_not_swallow_the_next_option(capsys):
    """'--full-help --mode crawl' asks for the whole reference, not for crawl's."""
    with pytest.raises(SystemExit) as excinfo:
        oa.build_parser().parse_known_args(["--full-help", "--mode", "crawl"])
    assert excinfo.value.code == 0
    assert "--mode multi" in capsys.readouterr().out


def test_full_help_rejects_an_unknown_mode():
    with pytest.raises(SystemExit) as excinfo:
        oa.build_parser().parse_known_args(["--full-help", "bogus"])
    assert excinfo.value.code == 2


def test_plain_help_points_at_the_full_reference():
    assert "--full-help" in oa.build_parser().format_help()


def test_full_help_after_the_separator_belongs_to_rvtree():
    """Past '--' nothing is ours, including our own flags."""
    _, args, rv_args = _parse(["--mode", "tree", "--", "list", "u://x", "--full-help"])
    assert args.full_help is None
    assert rv_args == ["list", "u://x", "--full-help"]


def test_the_reference_imports_nothing_but_the_standard_library():
    """--full-help has to work on the host that has not installed anything yet."""
    tree = ast.parse(open(fullhelp.__file__, encoding="utf-8").read())
    imported = {node.module.split(".")[0] for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module}
    imported |= {alias.name.split(".")[0] for node in ast.walk(tree)
                 if isinstance(node, ast.Import) for alias in node.names}
    assert imported <= {"__future__", "textwrap"}


# ----------------------------------------------------------------- main() wiring


def test_farm_and_url_together_is_a_usage_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        oa.main(["--farm", "status", "--url", "http://a.onion/x"])
    assert excinfo.value.code == 2


def test_a_stray_argument_outside_tree_mode_is_still_an_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        oa.main(["--mode", "multi", "--nonsense"])
    assert excinfo.value.code == 2


def test_main_takes_argv_so_it_can_be_driven_from_a_test():
    """rvtree's main(argv) is called this way from tree_mode(); ours now matches."""
    import inspect
    assert "argv" in inspect.signature(oa.main).parameters


# ----------------------------------------------------------------- the streaming flags


def test_stream_defaults_to_loopback_and_takes_an_optional_bind():
    _, args, _ = _parse(["--mode", "crawl", "--stream"])
    assert args.stream == oa.STREAM_BIND

    _, args, _ = _parse(["--mode", "crawl", "--stream", "127.0.0.1:9999"])
    assert args.stream == "127.0.0.1:9999"

    _, args, _ = _parse(["--mode", "crawl"])
    assert args.stream is None


def test_the_streaming_flags_do_not_leak_into_rvtrees_arguments():
    """Tree mode passes everything after '--' through; these are parsed before it."""
    _, args, rv_args = _parse(
        ["--mode", "tree", "--stream", "--stream-wait", "--", "list", "http://a.onion/x.rar"])
    assert args.stream == oa.STREAM_BIND and args.stream_wait is True
    assert rv_args == ["list", "http://a.onion/x.rar"]


def test_every_streaming_flag_is_documented_in_the_reference():
    """A flag the long-form help does not mention is a flag nobody finds."""
    documented = fullhelp.render()
    for flag in _options(oa.build_parser()):
        if flag.startswith("--stream"):
            assert flag in documented, flag


@pytest.mark.parametrize("mode", ["multi", "partial", "speedtest"])
def test_stream_is_refused_for_the_modes_that_discover_nothing(mode, tmp_path, monkeypatch):
    """--stream mirrors what a run *finds*; these consume a URL list rather than make one."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "URLs.txt").write_text("http://a.onion/x\n")
    with pytest.raises(SystemExit) as excinfo:
        oa.main(["--mode", mode, "--stream"])
    assert excinfo.value.code == 2


def test_the_no_stream_sink_answers_everything_and_is_falsy():
    """Call sites pass it straight to a producer without asking whether streaming is on."""
    assert not oa.NO_STREAM
    assert oa.NO_STREAM("crawl.file", {"url": "u"}) is None
    assert oa.NO_STREAM.publish("run.stop") is None


def test_streaming_yields_the_no_op_sink_when_the_flag_is_absent():
    _, args, _ = _parse(["--mode", "crawl"])
    with oa.streaming(args, "crawl", ["http://a.onion/"]) as stream:
        assert stream is oa.NO_STREAM


def test_streaming_serves_the_run_and_stops_when_it_ends():
    """Port 0 so the suite never fights the operator's own 8787 for the socket."""
    _, args, _ = _parse(["--mode", "crawl", "--stream", "127.0.0.1:0"])
    with oa.streaming(args, "crawl", ["http://a.onion/"]) as stream:
        assert stream
        stream("crawl.file", {"url": "http://a.onion/found.txt"})
        assert stream.counts["crawl.file"] == 1
        # run.start is published before the run is handed the bus, so a consumer that
        # attached first knows what it is watching.
        assert stream.counts["run.start"] == 1
    assert stream.counts["run.stop"] == 1


def test_a_bind_that_cannot_be_honoured_stops_the_run():
    """Somebody asked for this run to be watched; running it unwatched is not the fix."""
    _, args, _ = _parse(["--mode", "crawl", "--stream", "0.0.0.0:8787"])
    with pytest.raises(SystemExit) as excinfo:
        with oa.streaming(args, "crawl", ["http://a.onion/"]):
            pass
    assert excinfo.value.code == 1


def test_tree_mode_hands_the_sink_to_rvtree_and_closes_the_run(monkeypatch):
    """The passthrough is the whole integration; rvtree's own suite tests what it does."""
    seen = {}

    class FakeCli:
        @staticmethod
        def main(argv, transport=None, sink=None):
            seen["argv"] = argv
            seen["sink"] = sink
            sink("tree.entry", {"path": "etc/hosts", "size": 12})
            return 0

    class FakeTransport:
        lanes = 2
        bytes_fetched = 4096
        requests_made = 7

        def __init__(self, **kwargs):
            pass

        def close(self):
            seen["closed"] = True

    monkeypatch.setattr(oa, "_import_rvtree", lambda: (FakeCli, FakeTransport))

    published = []
    code = oa.tree_mode(["list", "http://a.onion/x.rar"], ["127.0.0.1:9050"],
                        sink=lambda kind, data: published.append((kind, data)))

    assert code == 0
    assert seen["sink"] is not None and seen["closed"]
    kinds = [kind for kind, _ in published]
    assert kinds == ["tree.entry", "run.stop"]
    assert published[-1][1]["exit_code"] == 0
    assert published[-1][1]["bytes_fetched"] == 4096


def test_tree_mode_without_a_stream_passes_rvtree_no_sink(monkeypatch):
    seen = {}

    class FakeCli:
        @staticmethod
        def main(argv, transport=None, sink=None):
            seen["sink"] = sink
            return 0

    class FakeTransport:
        lanes = 1
        bytes_fetched = 0
        requests_made = 0

        def __init__(self, **kwargs):
            pass

        def close(self):
            pass

    monkeypatch.setattr(oa, "_import_rvtree", lambda: (FakeCli, FakeTransport))
    assert oa.tree_mode(["list", "http://a.onion/x.rar"], ["127.0.0.1:9050"],
                        sink=oa.NO_STREAM) == 0
    assert seen["sink"] is None
