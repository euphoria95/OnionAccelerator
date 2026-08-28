"""The published event kinds, checked against the producers that publish them.

Three modules name event kinds and none of them import the others, on purpose: the
crawler has to work on a host with nothing but aiohttp, and rvtree ships as its own
package. That independence is bought with duplicated string literals, and this is the
test that stops them drifting -- the same bargain tests/test_cli.py already strikes
between the argument parser and the reference it is documented in.

The reason drift matters here rather than being cosmetic: /schema is what a consumer
reads to find out what a run can tell it. A kind published but not described is a
finding nobody wrote a rule for.
"""

import os
import re

from crawler import report as crawl_report
from eventstream.bus import KINDS

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RVTREE_CLI = os.path.join(ROOT, "remote_viewer", "rvtree", "cli.py")
ONIONACCELERATOR = os.path.join(ROOT, "OnionAccelerator.py")


def published_in(module) -> set[str]:
    """Every EVENT_* constant a producer module declares."""
    return {value for name, value in vars(module).items()
            if name.startswith("EVENT_") and isinstance(value, str)}


def declared_in(path) -> set[str]:
    """The same, read out of a source file.

    rvtree is not importable from this suite -- it ships as its own package under
    remote_viewer/ and only --mode tree puts it on the path -- so its constants are read
    the way tests/test_cli.py already reads its list of subcommands.
    """
    with open(path, encoding="utf-8") as handle:
        return set(re.findall(r'^EVENT_[A-Z_]+ = "([a-z.]+)"', handle.read(), re.M))


def literals_in(path) -> set[str]:
    """Kinds published as bare strings, e.g. sink("run.stop", {...})."""
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    return set(re.findall(r'sink\(\s*"([a-z]+\.[a-z_]+)"', source))


def test_the_crawler_publishes_only_kinds_the_schema_describes():
    assert published_in(crawl_report) <= set(KINDS)


def test_rvtree_publishes_only_kinds_the_schema_describes():
    declared = declared_in(RVTREE_CLI)
    assert declared, "no EVENT_* constants found; has rvtree's sink been removed?"
    assert declared <= set(KINDS)


def test_the_kinds_published_as_literals_are_described_too():
    """--mode tree and --detect publish their own run facts without a constant."""
    literals = literals_in(ONIONACCELERATOR)
    assert literals, "the drift test found nothing to check; has the call shape changed?"
    assert literals <= set(KINDS)


def test_every_described_kind_is_actually_published_by_something():
    """A schema entry nobody emits is a rule somebody writes and never sees fire."""
    # stream.* are the protocol's own, emitted by the server rather than by a producer.
    protocol = {kind for kind in KINDS if kind.startswith("stream.")}
    produced = (published_in(crawl_report) | declared_in(RVTREE_CLI)
                | literals_in(ONIONACCELERATOR) | literals_in(RVTREE_CLI))
    # run.start is published by the streaming() context manager, from the constant.
    produced |= {"run.start"}
    assert set(KINDS) - protocol - produced == set()


def test_each_kind_is_namespaced_so_a_family_can_be_selected():
    for kind in KINDS:
        assert re.fullmatch(r"[a-z]+\.[a-z_]+", kind), kind


def test_every_kind_says_what_it_means():
    """/schema is documentation a consumer reads at runtime; empty entries are not."""
    for kind, description in KINDS.items():
        assert description.strip().endswith("."), kind
        assert len(description) > 20, kind
