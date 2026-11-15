"""Scoring paths: the shipped rule packs, the operator's own scope.toml, and the hits.

Every rule explains itself: a hit stores the rule that fired, why, and the span of the
path it matched, so an analyst reading a score of 80 can see which three characters made
it 80. The shipped packs carry their own examples, and a test here runs every one.
"""

import glob
import os
import re

import pytest

from index import rules as rulesmod
from index import store
from index.ingest import ingest_dir
from indexfixtures import HOST_A, dump, write_crawl_dir

PACK_DIR = rulesmod.PACK_DIR
CATEGORIES = {"credentials", "mail", "directory-service", "remote-access", "database", "hr",
              "finance", "legal"}


@pytest.fixture
def ix(tmp_path):
    with store.Index.open(str(tmp_path / "idx" / "index.db")) as index:
        yield index


def rows(ix, sql, *args):
    return ix.conn.execute(sql, args).fetchall()


def write_scope(tmp_path, text):
    path = tmp_path / "scope.toml"
    path.write_text(text)
    return str(path)


# ----------------------------------------------------------------------- the packs


def test_every_category_the_roadmap_names_has_a_pack():
    packs = {os.path.splitext(os.path.basename(p))[0] for p in glob.glob(PACK_DIR + "/*.toml")}
    assert CATEGORIES <= packs, CATEGORIES - packs


def test_every_shipped_rule_is_well_formed_and_proves_itself():
    loaded = rulesmod.load_rules()
    assert len(loaded.rules) >= 40
    seen = set()
    for rule in loaded.rules:
        assert rule.id not in seen, f"duplicate rule id {rule.id}"
        seen.add(rule.id)
        assert 1 <= rule.score <= 100, rule.id
        assert rule.reason.strip(), rule.id
        assert rule.on in ("path", "name"), rule.id
        assert rule.positive and rule.negative, f"{rule.id} has no examples to test it with"
        for example in rule.positive:
            assert rule.search(example), f"{rule.id} does not match its own example {example!r}"
        for example in rule.negative:
            assert not rule.search(example), f"{rule.id} matches {example!r}, which it must not"


def test_the_packs_contain_no_onion_address_and_no_real_name():
    """Rule data ships in a public repository: file-type patterns only, synthetic examples."""
    onion = re.compile(r"[a-z2-7]{16,56}\.onion")
    for path in glob.glob(PACK_DIR + "/*.toml"):
        text = open(path, encoding="utf-8").read()
        assert not onion.search(text), path


def test_rule_scores_rank_what_matters_higher():
    loaded = rulesmod.load_rules()
    key = max(h.score for h in loaded.score_path("/home/u/.ssh/id_rsa"))
    mail = max(h.score for h in loaded.score_path("/mail/john.pst"))
    boring = loaded.score_path("/images/holiday.jpg")
    assert key >= 80 and mail >= 50 and boring == []


@pytest.mark.parametrize("path, rule_prefix", [
    ("/it/keys/id_rsa", "credentials"),
    ("/backup/vault.kdbx", "credentials"),
    ("/mail/archive.pst", "mail"),
    ("/dc/ntds.dit", "directory-service"),
    ("/vpn/client.ovpn", "remote-access"),
    ("/db/prod_dump.sql", "database"),
    ("/hr/payroll_2024.xlsx", "hr"),
    ("/finance/invoices/INV-2024-001.pdf", "finance"),
    ("/legal/nda_signed.pdf", "legal"),
])
def test_each_category_fires_on_a_typical_path(path, rule_prefix):
    hits = rulesmod.load_rules().score_path(path)
    assert any(h.rule.startswith(rule_prefix + ":") for h in hits), [h.rule for h in hits]


def test_a_hit_records_the_span_it_matched(tmp_path):
    loaded = rulesmod.load_rules()
    path = "/IT/Keys/id_rsa"
    hit = next(h for h in loaded.score_path(path) if h.rule.startswith("credentials:"))
    assert path[hit.start:hit.end].lower().endswith("id_rsa")
    assert hit.reason and 1 <= hit.score <= 100


def test_a_name_rule_is_anchored_to_the_name_not_the_directory_it_sits_in(tmp_path):
    scope = write_scope(tmp_path, '[[rule]]\nid = "x"\nscore = 50\non = "name"\n'
                                  'regex = "^secret"\nreason = "r"\n')
    # /secret/doc.txt has a directory called secret, but its name is doc.txt
    loaded = rulesmod.load_rules(scope_path=scope)
    assert [h.rule for h in loaded.score_path("/secret/doc.txt") if h.rule == "x"] == []
    hit = [h for h in loaded.score_path("/a/secret.txt") if h.rule == "x"][0]
    assert "/a/secret.txt"[hit.start:hit.end] == "secret", "spans are offsets into the whole path"


# ----------------------------------------------------------------------- scope.toml


SCOPE = """
[scope]
organisations = ["Example Corp", "Exemplar GmbH"]
domains = ["example.test"]
suppliers = ["Widget Supply Ltd"]
regexes = ['(?i)\\bPO-\\d{6}\\b']

[scope.scores]
organisations = 75
"""


def test_scope_terms_become_rules_with_their_own_scores(tmp_path):
    loaded = rulesmod.load_rules(scope_path=write_scope(tmp_path, SCOPE))
    org = loaded.score_path("/shares/EXAMPLE CORP/q3.xlsx")
    assert [(h.rule, h.score) for h in org if h.rule.startswith("scope:")] == [
        ("scope:organisation:Example Corp", 75)]
    dom = [h for h in loaded.score_path("/mail/ceo@example.test/inbox.eml")
           if h.rule.startswith("scope:")]
    assert dom and dom[0].rule == "scope:domain:example.test" and dom[0].score == 70
    sup = [h for h in loaded.score_path("/contracts/widget supply ltd.pdf")
           if h.rule.startswith("scope:")]
    assert sup and sup[0].rule == "scope:supplier:Widget Supply Ltd" and sup[0].score == 50
    rx = [h for h in loaded.score_path("/orders/po-123456.pdf") if h.rule.startswith("scope:")]
    assert rx and rx[0].rule.startswith("scope:regex:")


def test_a_scope_term_is_a_literal_not_a_pattern(tmp_path):
    """'Acme (UK) Ltd.' must not be read as a regex with a group and a wildcard."""
    scope = write_scope(tmp_path, '[scope]\norganisations = ["Acme (UK) Ltd."]\n')
    loaded = rulesmod.load_rules(scope_path=scope)
    assert loaded.score_path("/x/acme (uk) ltd./f")
    assert not [h for h in loaded.score_path("/x/acme uk ltdx/f") if h.rule.startswith("scope:")]


def test_the_scope_hit_names_the_term_and_where_it_matched(tmp_path):
    loaded = rulesmod.load_rules(scope_path=write_scope(tmp_path, SCOPE))
    path = "/shares/EXAMPLE CORP/q3.xlsx"
    hit = next(h for h in loaded.score_path(path) if h.rule.startswith("scope:organisation"))
    assert path[hit.start:hit.end] == "EXAMPLE CORP"
    assert "Example Corp" in hit.reason


@pytest.mark.parametrize("text, message", [
    ("[scope]\nregexes = ['(unclosed']\n", "regex"),
    ("[scope]\norganisations = 'not a list'\n", "list"),
    ("[scope.scores]\norganisations = 500\n", "score"),
    ("[scope]\nwidgets = ['x']\n", "widgets"),
    ("not toml at all [[[", "TOML"),
])
def test_a_bad_scope_file_is_refused_with_the_reason(tmp_path, text, message):
    with pytest.raises(rulesmod.RuleError, match=message):
        rulesmod.load_rules(scope_path=write_scope(tmp_path, text))


def test_a_missing_scope_file_is_an_error_when_asked_for_by_name(tmp_path):
    with pytest.raises(rulesmod.RuleError, match="scope"):
        rulesmod.load_rules(scope_path=str(tmp_path / "absent.toml"))


@pytest.mark.parametrize("pattern", ["(a+)+$", "(.*)*x", "(a*)*b", "^(\\w+\\s?)*$", "(x+x+)+y"])
def test_a_regex_that_backtracks_exponentially_is_refused_at_load(tmp_path, pattern):
    """Scope regexes are the operator's, but paths are an attacker's: refuse the shape."""
    scope = write_scope(tmp_path, f"[scope]\nregexes = ['{pattern}']\n")
    with pytest.raises(rulesmod.RuleError, match="backtrack"):
        rulesmod.load_rules(scope_path=scope)


@pytest.mark.parametrize("pattern", [r"(?i)\bPO-\d{6}\b", r"a+b+c+", r"(foo|bar)+", r"[^/]*\.pdf$",
                                     r"(?:ab){1,3}c", r"x(\d+)y(\d+)z"])
def test_ordinary_patterns_are_not_refused(tmp_path, pattern):
    scope = write_scope(tmp_path, f"[scope]\nregexes = ['''{pattern}''']\n")
    rulesmod.load_rules(scope_path=scope)


def test_a_huge_path_is_matched_by_its_tail_only(tmp_path):
    scope = write_scope(tmp_path, "[scope]\nregexes = ['secret$']\n")
    loaded = rulesmod.load_rules(scope_path=scope)
    hits = loaded.score_path("/" + "x" * 2_000_000 + "/secret")
    assert [h.rule for h in hits if h.rule.startswith("scope:")]
    hit = next(h for h in hits if h.rule.startswith("scope:"))
    assert hit.end == 2_000_008, "spans are offsets into the whole path, not the truncated tail"


# ----------------------------------------------------------------------- in the index


def test_ingest_scores_new_entries_and_stores_why(ix, tmp_path):
    ingest_dir(ix, write_crawl_dir(tmp_path, "run1", files=[
        ("/hr/payroll_2024.xlsx", 10), ("/pub/holiday.jpg", 5), ("/it/id_rsa", 2)]),
        rules=rulesmod.load_rules())
    hits = rows(ix, "SELECT e.path, h.rule, h.score, h.reason, h.span_start, h.span_end "
                    "FROM hits h JOIN entries e ON e.id=h.entry_id ORDER BY e.path, h.rule")
    assert {h[0] for h in hits} == {"/hr/payroll_2024.xlsx", "/it/id_rsa"}
    for path, rule, score, reason, a, b in hits:
        assert reason and 0 <= a < b <= len(path)


def test_ingesting_twice_does_not_duplicate_hits(ix, tmp_path):
    job = write_crawl_dir(tmp_path, "run1", files=[("/it/id_rsa", 2)])
    ingest_dir(ix, job, rules=rulesmod.load_rules())
    first = dump(ix)
    ingest_dir(ix, job, rules=rulesmod.load_rules())
    assert dump(ix) == first


def test_rescoring_replaces_hits_when_the_rules_change(ix, tmp_path):
    ingest_dir(ix, write_crawl_dir(tmp_path, "run1", files=[
        ("/shares/Example Corp/a.txt", 1), ("/it/id_rsa", 2)]), rules=rulesmod.load_rules())
    assert not [r for r in rows(ix, "SELECT rule FROM hits") if r[0].startswith("scope:")]

    scope = write_scope(tmp_path, '[scope]\norganisations = ["Example Corp"]\n')
    counts = rulesmod.rescore(ix, rulesmod.load_rules(scope_path=scope))
    assert counts.entries > 0
    rules_now = {r[0] for r in rows(ix, "SELECT rule FROM hits")}
    assert "scope:organisation:Example Corp" in rules_now and any(
        r.startswith("credentials:") for r in rules_now)

    rulesmod.rescore(ix, rulesmod.load_rules())               # the term is withdrawn
    assert not [r for r in rows(ix, "SELECT rule FROM hits") if r[0].startswith("scope:")]


def test_an_entrys_score_is_its_best_hit(ix, tmp_path):
    ingest_dir(ix, write_crawl_dir(tmp_path, "run1", files=[("/it/id_rsa", 2)]),
               rules=rulesmod.load_rules())
    from index import query
    top = query.top_hits(ix, min_score=1)
    assert top[0]["path"] == "/it/id_rsa"
    assert top[0]["score"] == max(h["score"] for h in top[0]["hits"])
    assert top[0]["host"] == HOST_A


@pytest.mark.parametrize("path, rule, word", [
    ("/hr/payroll_2024.xlsx", "hr:payroll", "payroll"),
    ("/fin/Bank Statement 2024.pdf", "finance:bank-details", "Bank Statement"),
    ("/legal/nda_signed.pdf", "legal:nda", "nda"),
    ("/mail/archive.pst", "mail:outlook-store", ".pst"),
    ("/ops/api_keys.txt", "credentials:api-token-file", "api_keys.txt"),
])
def test_a_span_covers_what_matched_and_not_the_separators_around_it(path, rule, word):
    """An explanation that highlights the slash and underscore beside the word is wrong."""
    hit = next(h for h in rulesmod.load_rules().score_path(path) if h.rule == rule)
    assert path[hit.start:hit.end] == word
