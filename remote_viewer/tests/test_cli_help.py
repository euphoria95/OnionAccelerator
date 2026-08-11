"""The help text is a reference, and a reference that drifts is worse than none.

Everything here is offline and needs no fixtures: it reads the parsers themselves.
"""

from __future__ import annotations

import argparse
import re

import pytest

from rvtree.cli import EXIT_CODES, MODES, build_parser, main


def _help(monkeypatch, columns: str = "100") -> str:
    monkeypatch.setenv("COLUMNS", columns)
    return build_parser().format_help()


def _subparsers(parser) -> dict:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices
    raise AssertionError("the parser grew no subcommands")


# ----------------------------------------------------------------- completeness


def test_every_option_of_every_mode_appears_in_the_top_level_help(monkeypatch):
    """The drift guard: an option added to a mode cannot go missing from the reference."""
    text = _help(monkeypatch)
    missing = []
    for name, parser in _subparsers(build_parser()).items():
        for action in parser._actions:
            for flag in action.option_strings:
                if flag not in ("-h", "--help") and flag not in text:
                    missing.append(f"{name} {flag}")
    assert not missing, f"absent from `rvtree --help`: {missing}"


def test_every_mode_is_named_with_its_summary(monkeypatch):
    text = _help(monkeypatch)
    for name, summary in MODES.items():
        assert name in text
        assert summary in text


def test_positional_arguments_are_shown_with_their_syntax(monkeypatch):
    text = _help(monkeypatch)
    assert "URL" in text
    assert "PATH" in text  # extract's member path, easy to omit and impossible to guess


# ----------------------------------------------------------------- syntax


@pytest.mark.parametrize(
    "expected,rejected",
    [
        ("--proxy URL", "--proxy PROXY"),
        ("--circuits N", "--circuits CIRCUITS"),
        ("--timeout SECONDS", "--timeout TIMEOUT"),
        ("--retries N", "--retries RETRIES"),
        ("--ca-bundle PATH", "--ca-bundle CA_BUNDLE"),
        ("--archive-type TYPE", "--archive-type ARCHIVE_TYPE"),
        ("--limit N", "--limit LIMIT"),
        ("--limit-blocks N", "--limit-blocks LIMIT_BLOCKS"),
        ("--log-file PATH", "--log-file LOG_FILE"),
    ],
)
def test_metavars_show_what_you_would_actually_type(monkeypatch, expected, rejected):
    text = _help(monkeypatch)
    assert expected in text
    assert rejected not in text


def test_short_and_long_flags_share_one_metavar(monkeypatch):
    """``-o, --output FILE``, not argparse's default ``-o FILE, --output FILE``."""
    text = _help(monkeypatch)
    assert "-o, --output FILE" in text
    assert "-o FILE, --output FILE" not in text


def test_the_synopsis_names_every_mode(monkeypatch):
    usage = build_parser().format_usage()
    for name in MODES:
        assert f"rvtree {name}" in usage


def test_help_wraps_to_the_terminal(monkeypatch):
    """A reference that wraps at the wrong column is a reference nobody reads."""
    for columns in ("80", "100"):
        text = _help(monkeypatch, columns)
        too_long = [line for line in text.splitlines() if len(line) > int(columns)]
        assert not too_long, f"at {columns} columns: {too_long[:2]}"


# ----------------------------------------------------------------- the extras


def test_examples_cover_every_mode(monkeypatch):
    text = _help(monkeypatch)
    assert "Examples" in text
    for name in MODES:
        assert f"rvtree {name}" in text.split("Examples", 1)[1]


def test_exit_codes_are_documented_and_match_the_source_of_truth(monkeypatch):
    text = _help(monkeypatch)
    codes = text.split("Exit codes", 1)[1]
    for code, description in EXIT_CODES:
        assert re.search(rf"^\s+{code}\s+\S", codes, re.M), f"exit code {code} undocumented"
        assert description.splitlines()[0] in codes
    assert {code for code, _ in EXIT_CODES} == {0, 1, 2, 3, 4, 130}


def test_diagnostics_flags_are_discoverable(monkeypatch):
    """The reason the reference exists: these are the flags you need when stuck."""
    text = _help(monkeypatch)
    for flag in ("-v, --verbose", "--debug", "--progress", "--log-file PATH"):
        assert flag in text


# ----------------------------------------------------------------- invocation


def test_bare_rvtree_prints_the_reference_and_fails(capsys):
    """Better than argparse's one-line complaint, which tells you nothing about the tool."""
    assert main([]) == 2
    err = capsys.readouterr().err
    for name in MODES:
        assert name in err
    assert "Exit codes" in err


def test_help_subcommand_matches_the_top_level_help(capsys):
    assert main(["help"]) == 0
    assert "Exit codes" in capsys.readouterr().out


def test_help_for_one_mode_shows_that_mode(capsys):
    assert main(["help", "extract"]) == 0
    out = capsys.readouterr().out
    assert "--max-fetch MiB" in out
    assert "--limit-blocks" not in out  # that one belongs to list


def test_build_parser_keeps_its_name_and_still_parses(monkeypatch):
    """tests/test_tls.py imports this by name; so may anything else built on it."""
    args = build_parser().parse_args(["list", "--verify-tls", "http://example.invalid/a.zip"])
    assert args.command == "list"
    assert args.verify_tls is True
    assert args.verbose == 0
    assert args.progress == "auto"


def test_mode_help_still_lists_the_common_options(capsys):
    """The grouping has to survive argparse's ``parents=`` copy into each subparser."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["list", "--help"])
    out = capsys.readouterr().out
    assert "connection options" in out
    assert "diagnostics" in out
    assert "--proxy URL" in out
