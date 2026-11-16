"""The documentation makes promises a test can keep: its links resolve and its examples parse.

Prose rots by being right once. These two checks are what stops a renamed flag or a moved page
from going unnoticed: every relative link in README.md and docs/ must point at a file that
exists, and every command line shown as an example must be accepted by the real argument
parser (flags and values, not network behaviour -- nothing here runs a mode).
"""

import glob
import os
import re
import shlex

import pytest

import OnionAccelerator as oa

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGES = sorted(glob.glob(os.path.join(ROOT, "docs", "**", "*.md"), recursive=True)
               + [os.path.join(ROOT, "README.md")])
# The roadmap quotes commands as history, and the generated benchmark page quotes ours.
EXAMPLE_PAGES = [p for p in PAGES if os.path.basename(p) != "ROADMAP.md"]
RAW = "https://github.com/euphoria95/OnionAccelerator/blob/main/"


def rel(path):
    return os.path.relpath(path, ROOT)


@pytest.mark.parametrize("page", PAGES, ids=rel)
def test_every_link_points_at_a_file_that_exists(page):
    text = open(page, encoding="utf-8").read()
    broken = []
    for target in re.findall(r"\]\(([^)\s]+)\)", text):
        if target.startswith(RAW):                       # the README's absolute links
            local = os.path.join(ROOT, target[len(RAW):].split("#")[0])
        elif target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        else:
            local = os.path.normpath(os.path.join(os.path.dirname(page), target.split("#")[0]))
        if not os.path.exists(local):
            broken.append(target)
    assert not broken, f"{rel(page)}: {broken}"


def test_every_docs_page_is_reachable_from_the_docs_home():
    home = open(os.path.join(ROOT, "docs", "README.md"), encoding="utf-8").read()
    linked = {os.path.normpath(os.path.join("docs", t.split("#")[0]))
              for t in re.findall(r"\]\(([^)\s]+\.md[^)]*)\)", home)}
    pages = {rel(p) for p in PAGES if p.startswith(os.path.join(ROOT, "docs"))
             and os.path.basename(p) != "README.md"}
    assert pages - linked == set(), "docs pages nobody links to"


def commands(text):
    """Every `onionaccelerator ...` / `python3 OnionAccelerator.py ...` example, joined across
    backslash continuations, minus a trailing '&', a comment and anything after a pipe.
    A line with placeholders in it (`<mode>`, `[--url URL ...]`) is a synopsis and is skipped."""
    out, current = [], None
    for raw in text.splitlines():
        line = raw.strip()
        if current is None:
            m = re.match(r"^(?:python3 OnionAccelerator\.py|onionaccelerator)\s+(.*)$", line)
            if not m:
                continue
            current = m.group(1)
        else:
            current += " " + line
        if current.endswith("\\"):
            current = current[:-1].rstrip()
            continue
        current = re.split(r"\s+#\s", current)[0]
        if re.search(r"<[A-Za-z|\-]+>|\[[-\w ]+[^\]]*\]|<[a-z|]+", current) or "..." in current:
            current = None                    # a synopsis, not an example: placeholders in it
            continue
        # A pipeline's `|` has spaces round it; a regex alternation inside quotes does not.
        current = re.split(r"\s+\|\s+", current)[0].rstrip("& ").strip()
        out.append(current)
        current = None
    return out


def parse(command):
    argv = shlex.split(command.replace("`", ""))
    parser = oa.build_parser()
    args, rest = parser.parse_known_args(argv)
    return args, rest


def all_examples():
    seen = []
    for page in EXAMPLE_PAGES:
        text = open(page, encoding="utf-8").read()
        for in_fence in re.findall(r"```[a-z]*\n(.*?)```", text, re.S):
            seen += [(rel(page), c) for c in commands(in_fence)]
    return seen


EXAMPLES = all_examples()


def test_there_are_examples_to_check():
    assert len(EXAMPLES) > 40, "the example extractor found almost nothing; it has broken"


@pytest.mark.parametrize("page, command", EXAMPLES, ids=[f"{p}:{c[:50]}" for p, c in EXAMPLES])
def test_every_example_is_accepted_by_the_real_parser(page, command):
    # `--help`, `--full-help` and `--list-profiles` act at parse time; they are examples of
    # asking, not of running.
    try:
        args, rest = parse(command)
    except SystemExit as exc:
        assert exc.code in (0, None), f"{page}: the parser rejected: {command}"
        return
    # A mode that takes leftovers (tree -> rvtree, index -> its action) is checked further;
    # for every other mode a leftover is a typo.
    if rest and args.mode not in ("tree", "index"):
        pytest.fail(f"{page}: unrecognised arguments {rest} in: {command}")
    if args.mode == "index":
        from index import cli
        assert rest and rest[0] in cli.ACTIONS, f"{page}: not an index action: {command}"
        try:
            cli._parser().parse_args(rest)
        except cli.UsageError as exc:
            pytest.fail(f"{page}: index action rejected ({exc}): {command}")
