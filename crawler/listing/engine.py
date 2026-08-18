"""The seam: one call that turns a fetched page into entries and next requests.

`crawl.py` knows this class and nothing else about reading a page. Everything a target
can differ in -- markup, addressing, transport, pagination -- is behind `parse()`.

Profile selection is per host and happens once. The first page a host answers decides
which template is used for the rest of that host's crawl, which is both cheaper than
re-matching every page and more correct: a manager whose deeper pages are less
distinctive than its root would otherwise drift onto a different profile halfway down and
start addressing children a different way.
"""

from __future__ import annotations

import logging
from typing import Optional, Sequence

from .model import Entry, Listing, PageRequest, dedupe, split_entries
from .navigate import Location, next_page, resolve
from .page import Page
from .profile import Profile
from .registry import FALLBACK, find, load_profiles
from .strategies import ExtractContext, ExtractResult, Strategy, get as get_strategy

logger = logging.getLogger("OnionAccelerator.crawl.listing")

# How much of a body a [match].body_regex is tested against. A listing can be megabytes;
# the fingerprint that identifies the software is in the shell around it, never buried in
# row nine thousand.
MATCH_BODY_LIMIT = 512 * 1024

# Below this a match is not considered evidence of anything, and the fallback wins.
MATCH_FLOOR = 0.0


class ListingEngine:
    """Chooses a profile per host and reads pages with it."""

    def __init__(
        self,
        profiles: Sequence[Profile],
        *,
        forced: Optional[str] = None,
        allow_offsite: bool = False,
    ) -> None:
        self.profiles = tuple(profiles)
        self.allow_offsite = allow_offsite
        self._locked: dict[str, Profile] = {}
        # Hosts that have already been reported as having no template, so the log says it
        # once rather than once per directory.
        self._announced: set[str] = set()

        self.forced: Optional[Profile] = None
        if forced:
            self.forced = find(self.profiles, forced)
            if self.forced is None:
                raise ValueError(
                    f"unknown crawl profile {forced!r}; "
                    f"have: {', '.join(p.name for p in self.profiles)}")

        self.fallback = find(self.profiles, FALLBACK)
        if self.fallback is None:                     # pragma: no cover - registry guards it
            raise ValueError(f"the {FALLBACK!r} profile is required")

    @classmethod
    def build(cls, *, forced: Optional[str] = None, templates: Sequence[str] = (),
              allow_offsite: bool = False) -> "ListingEngine":
        return cls(load_profiles(templates), forced=forced, allow_offsite=allow_offsite)

    # ------------------------------------------------------------ selection

    def score(self, profile: Profile, page: Page) -> Optional[float]:
        """How well this profile matches, or None if one of its rules said no.

        Every rule present must hold -- the score is not a vote, it is a count of how
        much evidence there was once everything agreed. That is what separates "this is
        Apache's autoindex" from "this is HTML", and it is why a profile that names one
        weak signal cannot outrank one that names four.
        """
        match = profile.match
        if match.content_type:
            content_type = (page.content_type or "").lower()
            if not any(content_type.startswith(ct) for ct in match.content_type):
                return None
        if match.status and page.status not in match.status:
            return None
        if match.url_regex and not match.url_regex.search(page.url):
            return None
        if match.body_regex and not match.body_regex.search(page.body[:MATCH_BODY_LIMIT]):
            return None
        for name, pattern in match.headers:
            if not pattern.search(page.header(name)):
                return None
        if match.title_regex or match.generator_regex:
            from .strategies.anchors import _generator, _title
            soup = page.soup
            if match.title_regex and not match.title_regex.search(_title(soup) or ""):
                return None
            if match.generator_regex and not match.generator_regex.search(_generator(soup) or ""):
                return None
        return min(1.0, 0.25 * match.rule_count)

    def rank(self, page: Page) -> list[tuple[float, Profile]]:
        """Every profile that matches, best first. What `--detect` prints."""
        scored = [
            (score, profile)
            for profile, score in ((p, self.score(p, page)) for p in self.profiles)
            if score is not None
        ]
        scored.sort(key=lambda item: (-item[0], -item[1].priority, item[1].name))
        return scored

    def profile_for(self, page: Page) -> Profile:
        """The profile this host is crawled with, deciding it on the first page that says.

        A decision is only locked once a *named* profile matches. The fallback is not a
        decision -- it is "nothing has identified this yet" -- and locking it would be
        wrong for the shape this crawler meets most: a file manager whose root page links
        its children conventionally and only starts folding path separators one level
        down. Locking the root's structural reading would mean the template that
        recognises the manager never gets to see the page it recognises.
        """
        if self.forced is not None:
            return self.forced
        host = page.host
        locked = self._locked.get(host)
        if locked is not None:
            return locked

        ranked = self.rank(page)
        chosen = self.fallback
        for score, profile in ranked:
            if profile.name == FALLBACK or score <= MATCH_FLOOR:
                continue
            chosen = profile
            break

        if chosen.name == FALLBACK:
            if host not in self._announced:
                self._announced.add(host)
                logger.info("[%s] no template matched; reading structurally (%s)",
                            host, FALLBACK)
        else:
            self._locked[host] = chosen
            score = next((s for s, p in ranked if p is chosen), 0.0)
            unverified = "" if chosen.verified else " [unverified template]"
            logger.info("[%s] profile: %s (match %.2f)%s -- %s",
                        host, chosen.name, score, unverified, chosen.title or chosen.name)
        return chosen

    # ------------------------------------------------------------ reading

    def parse(self, page: Page, *, profile: Optional[Profile] = None) -> Listing:
        """One page as a listing: entries addressed, pagination queued."""
        profile = profile or self.profile_for(page)
        strategy = get_strategy(profile.extract.strategy)
        if strategy is None:                          # pragma: no cover - registry guards it
            raise ValueError(f"profile {profile.name}: unknown strategy "
                             f"{profile.extract.strategy!r}")

        result = self._read(strategy, page, profile)
        request = page.request or PageRequest.get(page.url)
        location = Location.of(profile, result.base_url or page.url, request)

        entries: list[Entry] = []
        for raw in result.entries:
            entry = resolve(location, raw)
            if entry is not None:
                entries.append(entry)
        directories, files = split_entries(dedupe(entries))

        more: tuple[PageRequest, ...] = ()
        if result.expandable:
            following = next_page(location, result, self._page_index(location, request))
            if following is not None:
                more = (following,)

        return Listing(
            base_url=result.base_url or page.url,
            directories=directories,
            files=files,
            is_index=self._is_index(profile, result),
            confidence=result.confidence,
            title=result.title,
            generator=result.generator,
            profile=profile.name,
            more=more,
            expandable=result.expandable,
        )

    def _read(self, strategy: Strategy, page: Page, profile: Profile) -> ExtractResult:
        ctx = ExtractContext(
            allow_offsite=self.allow_offsite,
            address_kind=profile.navigate.kind,
        )
        return strategy.read(page, profile.extract.options, ctx)

    def _is_index(self, profile: Profile, result: ExtractResult) -> bool:
        """May this page be expanded?

        For the structural reader it is the confidence score against its threshold -- the
        guard that keeps a directory walk from becoming a crawl of somebody's forum. For a
        page a named profile matched, the match is the evidence and the heuristic is not
        asked: a template said this is that target, and a target's listing is a listing
        even when it holds one file. A strategy can still veto with a zero confidence,
        which is how "this JSON is an error page, not an empty directory" is expressed.
        """
        if profile.name == FALLBACK:
            return result.is_index
        return result.confidence > 0.0 and result.is_index

    @staticmethod
    def _page_index(location: Location, request: PageRequest) -> int:
        """Which page of this directory we are on, counting from zero."""
        paginate = location.profile.paginate
        if not paginate.enabled:
            return 0
        from urllib.parse import parse_qsl, urlsplit
        query = dict(parse_qsl(urlsplit(request.url).query, keep_blank_values=True))
        value = query.get(paginate.param)
        if value is None or not value.lstrip("-").isdigit():
            return 0
        return max(0, int(value) - paginate.start)
