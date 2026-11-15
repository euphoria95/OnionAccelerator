"""Scoring a path: rule packs as data, the operator's scope.toml, and explainable hits.

A rule is a regular expression, a score from 1 to 100, and a reason. A hit stores the
rule that fired, the reason, and the span of the path it matched, so a score of 80 can be
read back as "these three characters, because this". Nothing here is a model: the same
path scores the same way on every run, and an analyst can argue with a single rule.

Rule packs are TOML under index/packs/, following the pattern of crawler/listing/
templates/: adding coverage is adding a file, not changing code. Every rule carries
`positive` and `negative` example paths, and tests/test_index_rules.py runs every one --
so a pack cannot ship a rule that does not match what it says it matches, and an edit that
broadens one fails on its own negative examples.

The operator's own terms go in a scope.toml (organisation names, domains, suppliers,
regexes) and become rules of their own. Terms are *literals*: "Acme (UK) Ltd." is searched
for as written, not read as a pattern. Only the `regexes` list and `[[rule]]` tables are
regular expressions.

Paths are written by the server being investigated, so matching is defended:

* a regex with a nested unbounded repeat -- (a+)+, (.*)* -- is refused at load, because
  that shape backtracks exponentially on input crafted for it;
* only the last MAX_MATCH characters of a path are matched. The file name is at the end,
  and a megabyte path is a weapon, not a path.

That is a guard, not a proof: alternations with overlapping branches can still be slow.
The shipped packs are checked by the same loader, and a regex of your own is yours.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import re
from typing import Iterable, Optional, Sequence

try:
    import tomllib as _toml
except ImportError:                                   # pragma: no cover - Python < 3.11
    import tomli as _toml  # type: ignore[no-redef]

try:
    from re import _constants as _sre_constants, _parser as _sre_parse
except ImportError:                                   # pragma: no cover - introspection only
    _sre_constants = _sre_parse = None

logger = logging.getLogger("OnionAccelerator.index.rules")

PACK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "packs")
MAX_MATCH = 1024
MIN_SCORE, MAX_SCORE = 1, 100

# What a scope term scores unless [scope.scores] says otherwise: your own name in a path is
# the strongest signal, a supplier's the weakest of the named ones.
SCOPE_KINDS = {
    "organisations": ("organisation", 70),
    "domains": ("domain", 70),
    "suppliers": ("supplier", 50),
    "regexes": ("regex", 60),
}


class RuleError(ValueError):
    """A rule pack or scope file that cannot be used, with the file and the reason."""


@dataclasses.dataclass(frozen=True)
class RuleHit:
    rule: str
    score: int
    reason: str
    start: int
    end: int


@dataclasses.dataclass(frozen=True)
class Rule:
    id: str
    score: int
    reason: str
    on: str                                   # "path" or "name"
    pattern: str
    positive: tuple = ()
    negative: tuple = ()

    @property
    def _re(self):
        return _COMPILED[self]

    def match(self, path: str) -> Optional[RuleHit]:
        """The hit for `path`, or None. `start`/`end` are offsets into the whole path."""
        text = path[-MAX_MATCH:]
        offset = len(path) - len(text)
        if self.on == "name":
            cut = text.rfind("/") + 1
            offset += cut
            text = text[cut:]
        m = self._re.search(text)
        if not m:
            return None
        return RuleHit(self.id, self.score, self.reason, offset + m.start(), offset + m.end())

    def search(self, path: str) -> bool:
        return self.match(path) is not None


_COMPILED: dict = {}


def _compile(rule: Rule) -> None:
    try:
        compiled = re.compile(rule.pattern)
    except re.error as exc:
        raise RuleError(f"rule {rule.id}: bad regex {rule.pattern!r}: {exc}") from exc
    problem = backtracking_risk(rule.pattern)
    if problem:
        raise RuleError(f"rule {rule.id}: regex {rule.pattern!r} may backtrack "
                        f"catastrophically ({problem}); rewrite it without nested "
                        f"unbounded repeats")
    _COMPILED[rule] = compiled


# ----------------------------------------------------------------------- the guard


def _subpatterns(av):
    """Every sub-pattern directly inside one parsed element, whatever its kind.

    A repeat holds one, a group one, a branch several; they sit at different positions in
    `av`, so look for them by type rather than by the layout of each opcode.
    """
    if isinstance(av, _sre_parse.SubPattern):
        yield av
    elif isinstance(av, (tuple, list)):
        for item in av:
            yield from _subpatterns(item)


def _has_unbounded(pattern, inside: bool) -> bool:
    repeats = (_sre_constants.MAX_REPEAT, _sre_constants.MIN_REPEAT,
               getattr(_sre_constants, "POSSESSIVE_REPEAT", None))
    for op, av in pattern:
        unbounded = op in repeats and av[1] == _sre_constants.MAXREPEAT
        if unbounded and inside:
            return True
        for child in _subpatterns(av):
            if _has_unbounded(child, inside or unbounded):
                return True
    return False


def backtracking_risk(pattern: str) -> Optional[str]:
    """Why `pattern` is likely to backtrack exponentially, or None."""
    if _sre_parse is None:                            # pragma: no cover
        return None
    try:
        parsed = _sre_parse.parse(pattern)
    except Exception:                                 # noqa: BLE001 - re.compile reports it
        return None
    return "a repeat inside an unbounded repeat" if _has_unbounded(parsed, False) else None


# ----------------------------------------------------------------------- rule sets


class RuleSet:
    def __init__(self, rules: Sequence[Rule]):
        self.rules = tuple(rules)

    def __len__(self) -> int:
        return len(self.rules)

    def score_path(self, path: str) -> list[RuleHit]:
        """Every rule that fires on `path`, best first (ties by rule id, for stability)."""
        hits = []
        for rule in self.rules:
            hit = rule.match(path)
            if hit is not None:
                hits.append(hit)
        hits.sort(key=lambda h: (-h.score, h.rule))
        return hits


def _score(value, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not MIN_SCORE <= value <= MAX_SCORE:
        raise RuleError(f"{where}: score must be an integer from {MIN_SCORE} to {MAX_SCORE}, "
                        f"got {value!r}")
    return value


def _rule_from_table(table: dict, where: str, prefix: str = "") -> Rule:
    missing = [k for k in ("id", "score", "regex", "reason") if k not in table]
    if missing:
        raise RuleError(f"{where}: a rule needs {', '.join(missing)}")
    unknown = sorted(set(table) - {"id", "score", "regex", "reason", "on", "positive", "negative"})
    if unknown:
        raise RuleError(f"{where}: unknown rule key(s) {', '.join(unknown)}")
    on = table.get("on", "path")
    if on not in ("path", "name"):
        raise RuleError(f"{where}: 'on' must be \"path\" or \"name\", got {on!r}")
    rule = Rule(id=f"{prefix}{table['id']}", score=_score(table["score"], f"{where} ({table['id']})"),
                reason=str(table["reason"]), on=on, pattern=str(table["regex"]),
                positive=tuple(table.get("positive", ())), negative=tuple(table.get("negative", ())))
    _compile(rule)
    return rule


def _read_toml(path: str) -> dict:
    try:
        with open(path, "rb") as fh:
            return _toml.load(fh)
    except OSError as exc:
        raise RuleError(f"{path}: cannot be read: {exc}") from exc
    except _toml.TOMLDecodeError as exc:
        raise RuleError(f"{path}: not valid TOML: {exc}") from exc


def load_pack(path: str) -> list[Rule]:
    data = _read_toml(path)
    name = (data.get("pack") or {}).get("name") or os.path.splitext(os.path.basename(path))[0]
    return [_rule_from_table(t, os.path.basename(path), prefix=f"{name}:")
            for t in data.get("rule", [])]


def load_scope(path: str) -> list[Rule]:
    """The operator's scope.toml as rules: literals for terms, regexes where asked."""
    if not os.path.isfile(path):
        raise RuleError(f"scope file {path} does not exist")
    data = _read_toml(path)
    where = os.path.basename(path)
    scope = data.get("scope", {})
    scores = {k: v for k, v in scope.get("scores", {}).items()} if isinstance(
        scope.get("scores", {}), dict) else {}
    known = set(SCOPE_KINDS) | {"scores"}
    for key in scope:
        if key not in known:
            raise RuleError(f"{where}: unknown [scope] key {key!r}; expected one of "
                            f"{', '.join(sorted(known))}")
    for key in scores:
        if key not in SCOPE_KINDS:
            raise RuleError(f"{where}: [scope.scores] has no {key!r}; expected one of "
                            f"{', '.join(sorted(SCOPE_KINDS))}")
    rules: list[Rule] = []
    for key, (label, default) in SCOPE_KINDS.items():
        terms = scope.get(key, [])
        if not isinstance(terms, list) or not all(isinstance(t, str) and t for t in terms):
            raise RuleError(f"{where}: [scope] {key} must be a list of non-empty strings")
        score = _score(scores.get(key, default), f"{where} [scope.scores] {key}")
        for term in terms:
            literal = key != "regexes"
            rule = Rule(
                id=f"scope:{label}:{term}", score=score, on="path",
                pattern=("(?i)" + re.escape(term)) if literal else term,
                reason=(f"matches your scope: {label} '{term}'" if literal
                        else f"matches your scope regex {term!r}"))
            _compile(rule)
            rules.append(rule)
    for table in data.get("rule", []):
        rules.append(_rule_from_table(table, where))
    return rules


def load_rules(scope_path: Optional[str] = None, pack_dirs: Iterable[str] = ()) -> RuleSet:
    """The built-in packs, any extra pack directories, and the operator's scope file."""
    by_id: dict[str, Rule] = {}
    for directory in (PACK_DIR, *pack_dirs):
        if not os.path.isdir(directory):
            raise RuleError(f"rule pack directory {directory} does not exist")
        for filename in sorted(os.listdir(directory)):
            if filename.startswith((".", "_")) or not filename.endswith(".toml"):
                continue
            for rule in load_pack(os.path.join(directory, filename)):
                if rule.id in by_id:
                    logger.info("rule %s from %s overrides an earlier one", rule.id, directory)
                by_id[rule.id] = rule
    if scope_path:
        for rule in load_scope(scope_path):
            by_id[rule.id] = rule
    return RuleSet(by_id.values())


# ----------------------------------------------------------------------- re-scoring


@dataclasses.dataclass
class RescoreCounts:
    entries: int = 0
    hits: int = 0


def rescore(ix, rules: RuleSet, batch: int = 20000) -> RescoreCounts:
    """Replace every hit with what `rules` says now. Terms withdrawn lose their hits."""
    conn = ix.conn
    counts = RescoreCounts()
    conn.execute("DELETE FROM hits")
    pending: list = []
    last = 0
    while True:
        # By id range, so a million entries are never all in memory at once.
        rows = conn.execute("SELECT id, path FROM entries WHERE id > ? ORDER BY id LIMIT ?",
                            (last, batch)).fetchall()
        if not rows:
            break
        for entry_id, path in rows:
            counts.entries += 1
            for hit in rules.score_path(path):
                pending.append((entry_id, hit.rule, hit.score, hit.reason, hit.start, hit.end))
        last = rows[-1][0]
        _flush_hits(conn, pending, counts)
    conn.commit()
    return counts


def _flush_hits(conn, pending, counts) -> None:
    conn.executemany("INSERT OR REPLACE INTO hits(entry_id, rule, score, reason, span_start, "
                     "span_end) VALUES (?,?,?,?,?,?)", pending)
    counts.hits += len(pending)
    pending.clear()
