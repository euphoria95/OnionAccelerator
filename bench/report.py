"""Tables: what the suites return, how they are written to Markdown, and how a rerun is
compared with what the docs say.

The comparison is the reproducibility claim made executable. Timing is noisy, so a rerun is
checked against the published tables with a tolerance, cell by cell, and the *shape* must
match exactly: the same tables, the same rows, the same columns, the same non-numeric cells.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Optional

NA = "n/a"


@dataclasses.dataclass
class Table:
    title: str
    columns: list
    rows: list                      # list of lists of str
    notes: list = dataclasses.field(default_factory=list)


def render_table(t: Table) -> str:
    lines = [f"### {t.title}", "", "| " + " | ".join(t.columns) + " |",
             "|" + "|".join(["---"] * len(t.columns)) + "|"]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in t.rows]
    for note in t.notes:
        lines += ["", f"*{note}*"]
    return "\n".join(lines) + "\n"


def parse_tables(markdown: str) -> list:
    """Every `### title` followed by a pipe table, as Tables (notes are not recovered)."""
    tables, lines, i = [], markdown.splitlines(), 0
    while i < len(lines):
        if lines[i].startswith("### ") and i + 2 < len(lines) and lines[i + 2].startswith("|"):
            title, j = lines[i][4:].strip(), i + 2
            block = []
            while j < len(lines) and lines[j].startswith("|"):
                block.append([c.strip() for c in lines[j].strip().strip("|").split("|")])
                j += 1
            if len(block) >= 2:
                tables.append(Table(title, block[0], block[2:]))
            i = j
        else:
            i += 1
    return tables


_NUMBER = re.compile(r"^[~<>≈]?\s*(-?\d+(?:\.\d+)?)\s*(%|x|×|s|ms|MiB/s)?$")


def _number(cell: str) -> Optional[float]:
    m = _NUMBER.match(cell.strip())
    return float(m.group(1)) if m else None


def compare(fresh: list, published: list, tolerance: float = 0.35) -> list:
    """Problems found comparing a rerun with the docs; an empty list means it reproduces."""
    problems = []
    by_title = {t.title: t for t in published}
    for t in fresh:
        old = by_title.get(t.title)
        if old is None:
            problems.append(f"{t.title!r}: not in the published page")
            continue
        if t.columns != old.columns:
            problems.append(f"{t.title!r}: columns differ ({t.columns} vs {old.columns})")
            continue
        if [r[0] for r in t.rows] != [r[0] for r in old.rows]:
            problems.append(f"{t.title!r}: rows differ")
            continue
        for new_row, old_row in zip(t.rows, old.rows):
            for col, a, b in zip(t.columns, new_row, old_row):
                x, y = _number(a), _number(b)
                if x is None or y is None:
                    if a != b:
                        problems.append(f"{t.title!r} / {new_row[0]} / {col}: {a!r} vs {b!r}")
                    continue
                scale = max(abs(x), abs(y), 1e-9)
                if abs(x - y) / scale > tolerance and abs(x - y) > 0.15:
                    problems.append(f"{t.title!r} / {new_row[0]} / {col}: {a} vs published {b} "
                                    f"(more than {tolerance:.0%} apart)")
    for old in published:
        if old.title not in {t.title for t in fresh}:
            problems.append(f"{old.title!r}: published but not produced by this run")
    return problems
