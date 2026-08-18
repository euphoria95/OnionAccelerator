"""`--detect`: ask what a target is before spending a crawl finding out.

A crawl of an open directory over Tor is thousands of requests, and the failure mode when
the wrong reading is used is silence -- pages parse as empty, the run reports success, and
the tree stops three levels down. One request that says "this is a Tiny File Manager, the
path is in `?p=`, here is the flag to pass" is worth a great deal more than the retry it
costs.

Detection has two halves. The passive half ranks every profile against the seed page and
is exactly what the crawler itself does. The active half sends each candidate's `[probe]`
request -- the only way to identify a single-page app, which serves the same JavaScript
shell whatever you ask it for, and therefore something the user opts into rather than
something a crawl does behind their back.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Awaitable, Callable, Optional, Sequence
from urllib.parse import urljoin

from .engine import ListingEngine
from .model import PageRequest, headers_tuple
from .page import Page
from .profile import NAV_HREF, Profile
from .registry import FALLBACK

logger = logging.getLogger("OnionAccelerator.crawl.detect")

Fetch = Callable[[PageRequest], Awaitable[Optional[Page]]]


@dataclasses.dataclass
class Finding:
    """One profile's verdict on one target."""

    profile: Profile
    score: float
    entries: int = 0
    directories: int = 0
    files: int = 0
    sample: Optional[str] = None
    probed: bool = False
    error: Optional[str] = None
    is_index: bool = False

    @property
    def usable(self) -> bool:
        """Did reading the page this way produce a listing the crawler would expand?

        Finding entries is not enough: a forum read structurally yields a hundred thread
        links and is still not an open directory. `is_index` is the same judgement the
        crawl makes, so what --detect reports and what a crawl would do cannot drift.
        """
        return self.error is None and self.entries > 0 and self.is_index


def rank(engine: ListingEngine, page: Page) -> list[Finding]:
    """Every profile that matches the fetched page, best first, each one tried.

    Matching is cheap and reading is not much dearer, so every candidate is actually run
    against the page: a profile that matches on a banner but extracts nothing is a worse
    answer than one that matches on less and finds the rows, and that only shows up if
    both are tried.
    """
    findings: list[Finding] = []
    for score, profile in engine.rank(page):
        findings.append(_try(engine, page, profile, score))
    findings.sort(key=_order)
    return findings


async def detect(
    engine: ListingEngine,
    seed: str,
    fetch: Fetch,
    *,
    probe: bool = True,
) -> list[Finding]:
    """Fetch the seed, rank the profiles, then probe the ones that ask for it."""
    page = await fetch(PageRequest.get(seed))
    if page is None:
        return [Finding(profile=p, score=0.0, error="seed could not be fetched")
                for p in engine.profiles[:1]]

    findings = rank(engine, page)
    if not probe:
        return findings

    # Probing happens in two rounds, because a probe is a real request against somebody's
    # onion and there are a dozen profiles that offer one.
    #
    # Round one asks only the profiles whose [match] rules already fired but which read
    # nothing out of the page. That is the exact signature of a single-page app: the shell
    # carries the fingerprint of the software serving it, so the profile recognises the
    # target and then finds no listing in the JavaScript. It is one request, and it
    # answers the common case.
    #
    # Round two -- everything else that offers a probe -- runs only if round one found
    # nothing at all, because at that point the alternative is telling the user their
    # target is unreadable when a single POST would have shown otherwise.
    matched = {f.profile.name for f in findings}
    round_one = [p for p in engine.profiles
                 if p.probe is not None and p.name in matched]
    round_two = [p for p in engine.profiles
                 if p.probe is not None and p.name not in matched]

    for candidates in (round_one, round_two):
        if any(f.usable for f in findings):
            break
        for profile in candidates:
            finding = await _probe(engine, profile, seed, fetch)
            if finding is None:
                continue
            # The probe read the real listing; it supersedes whatever the shell said.
            findings = [f for f in findings if f.profile.name != profile.name]
            findings.append(finding)
            if finding.usable:
                break

    findings.sort(key=_order)
    return findings


async def _probe(engine: ListingEngine, profile: Profile, seed: str,
                 fetch: Fetch) -> Optional[Finding]:
    """Send one profile's probe request and read the answer with that profile."""
    spec = profile.probe
    assert spec is not None
    request = PageRequest(
        url=urljoin(seed, spec.path) if spec.path else seed,
        method=spec.method,
        headers=headers_tuple(dict(spec.headers)),
        body=(spec.body or "").replace("{path}", "").replace("{seed}", seed) or None,
        path="",
        profile=profile.name,
    )
    try:
        page = await fetch(request)
    except Exception as exc:                          # noqa: BLE001 - reported, not raised
        logger.debug("probe %s failed: %s", profile.name, exc)
        return Finding(profile=profile, score=0.0, probed=True, error=str(exc))
    if page is None:
        return None
    if spec.expect_regex and not spec.expect_regex.search(page.body[:65536]):
        return None

    finding = _try(engine, page, profile, score=0.5)
    finding.probed = True
    return finding


def _try(engine: ListingEngine, page: Page, profile: Profile, score: float) -> Finding:
    try:
        listing = engine.parse(page, profile=profile)
    except Exception as exc:                          # noqa: BLE001 - reported, not raised
        return Finding(profile=profile, score=score, error=f"{type(exc).__name__}: {exc}")
    sample = None
    if listing.directories:
        sample = listing.directories[0].url
    elif listing.files:
        sample = listing.files[0].fetch_url
    return Finding(
        profile=profile,
        score=score,
        entries=listing.total,
        directories=len(listing.directories),
        files=len(listing.files),
        sample=sample,
        is_index=listing.is_index,
    )


def _reads_like_the_fallback(profile: Profile) -> bool:
    """Does this template read a page the way the crawler would with no template at all?

    True for every plain autoindex profile: they use the structural reader and address
    children by their links, and exist only to *recognise* a server -- which spares a
    directory holding one file from the index-confidence guard. Nothing about the crawl
    command changes, so nothing about it should be printed.
    """
    return (profile.extract.strategy == "anchors"
            and profile.navigate.kind == NAV_HREF
            and not profile.paginate.enabled
            and not profile.download.enabled)


def _order(finding: Finding) -> tuple:
    """Best first: something that worked, then the strongest match, then priority."""
    return (not finding.usable, -finding.score, -finding.profile.priority, finding.profile.name)


def render(findings: Sequence[Finding], seed: str, *, extra_flags: str = "") -> str:
    """The report `--detect` prints, ending in the command to run next."""
    if not findings:
        return f"{seed}\n  nothing matched, and the structural reader found no entries.\n"

    width = max(len(f.profile.name) for f in findings)
    lines = [seed, ""]
    lines.append(f"  {'PROFILE'.ljust(width)}  MATCH  ENTRIES  DIRS  FILES  NOTE")
    for finding in findings:
        note = finding.error or ("probed" if finding.probed else "")
        if not note and finding.entries and not finding.is_index:
            note = f"refused: not a listing ({finding.entries} link(s) read anyway)"
        if not note and not finding.profile.verified:
            note = "unverified template"
        lines.append(
            f"  {finding.profile.name.ljust(width)}  {finding.score:>5.2f}  "
            f"{finding.entries:>7}  {finding.directories:>4}  {finding.files:>5}  {note}"
        )
        if finding.sample:
            lines.append(f"  {' ' * width}         e.g. {finding.sample}")

    best = findings[0]
    lines.append("")
    if not best.usable:
        lines.append("  No profile read this page as a listing. If the target is a "
                     "single-page app, its API is what to point --templates at.")
    elif _reads_like_the_fallback(best.profile):
        # Naming a profile that reads the page exactly as the default does would be noise
        # dressed up as advice: every anchors/href template differs from the structural
        # reader only in recognising the target, which detection has already done.
        if best.profile.name != FALLBACK:
            lines.append(f"  Recognised as {best.profile.name}, which reads the page the "
                         f"same way the default does.")
        lines.append("  The structural reader handles this target; no --profile needed:")
        lines.append(f"    python3 OnionAccelerator.py --mode crawl --url {seed}{extra_flags}")
    else:
        verify = "" if best.profile.verified else \
            "\n  (that template is unverified -- check the first few pages of the run)"
        lines.append("  Crawl it with:")
        lines.append(f"    python3 OnionAccelerator.py --mode crawl --url {seed} "
                     f"--profile {best.profile.name}{extra_flags}{verify}")
    return "\n".join(lines) + "\n"
