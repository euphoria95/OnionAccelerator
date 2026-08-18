"""Finding, loading and validating templates.

Built-in templates ship next to this file; `--templates DIR` adds more. That second half
matters as much as the first: the interesting targets are the ones nobody publishes a
profile for, and writing one has to be something you do in a scratch directory during an
engagement rather than a patch to a repository.

Load errors are fatal by design. A template that fails to parse is a rule that silently
stopped applying, and the crawl it produces looks exactly like a target with nothing in
it -- which is the failure this whole redesign exists to make impossible.
"""

from __future__ import annotations

import logging
import os
from typing import Iterable, Optional, Sequence

from .profile import Profile, TemplateError

logger = logging.getLogger("OnionAccelerator.crawl.listing.registry")

BUILTIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
TEMPLATE_SUFFIX = ".toml"

# The profile every target falls back to: the structural reader, no match rules, lowest
# priority. Its presence is the compatibility guarantee -- a target no template knows is
# crawled exactly as it was before any of this existed.
FALLBACK = "generic-structural"

try:  # pragma: no cover - one of the two always exists on a supported interpreter
    import tomllib as _toml
except ImportError:  # pragma: no cover - Python < 3.11
    try:
        import tomli as _toml  # type: ignore[no-redef]
    except ImportError:
        _toml = None  # type: ignore[assignment]


def load_profiles(extra_dirs: Sequence[str] = ()) -> tuple[Profile, ...]:
    """Every built-in template plus everything in `extra_dirs`, best-first.

    Sorted by priority so the caller can read the list as a precedence order; ties break
    on name so two runs of the same set never disagree about which of two equal profiles
    was tried first.
    """
    profiles: dict[str, Profile] = {}
    for directory in (BUILTIN_DIR, *extra_dirs):
        for profile in load_dir(directory):
            if profile.name in profiles:
                # A user template deliberately shadows a built-in of the same name: that
                # is how you fix a shipped profile mid-engagement without editing it.
                logger.info("template %s overrides %s",
                            profile.source, profiles[profile.name].source)
            profiles[profile.name] = profile
    if FALLBACK not in profiles:
        raise TemplateError(
            f"the {FALLBACK!r} template is missing from {BUILTIN_DIR}; without it a target "
            f"no profile matches cannot be crawled at all")
    return tuple(sorted(profiles.values(), key=lambda p: (-p.priority, p.name)))


def load_dir(directory: str) -> Iterable[Profile]:
    """Every template in one directory, in filename order."""
    if not os.path.isdir(directory):
        logger.warning("template directory does not exist: %s", directory)
        return []
    loaded = []
    for filename in sorted(os.listdir(directory)):
        if filename.startswith((".", "_")) or not filename.endswith(TEMPLATE_SUFFIX):
            continue
        loaded.append(load_file(os.path.join(directory, filename)))
    return loaded


def load_file(path: str) -> Profile:
    """One template file, parsed, validated, and checked against its strategy."""
    if _toml is None:  # pragma: no cover - depends on the interpreter
        raise TemplateError(
            f"cannot read {path}: crawl templates are TOML, which needs Python 3.11+ "
            f"(for tomllib) or the 'tomli' package on an older interpreter")
    try:
        with open(path, "rb") as handle:
            data = _toml.load(handle)
    except OSError as exc:
        raise TemplateError(f"{path}: cannot be read: {exc}") from exc
    except Exception as exc:                          # tomllib.TOMLDecodeError and kin
        raise TemplateError(f"{path}: not valid TOML: {exc}") from exc

    profile = Profile.from_dict(data, source=os.path.basename(path))
    _check_strategy(profile, path)
    return profile


def _check_strategy(profile: Profile, path: str) -> None:
    """The strategy exists, and every option it was given is one it takes.

    Skipped -- with a warning, not an error -- when the strategies cannot be imported at
    all, which happens only on a host without BeautifulSoup. Listing and validating
    templates is still useful there; crawling is not possible there either way.
    """
    try:
        from . import strategies
    except ImportError as exc:  # pragma: no cover - depends on the host
        logger.warning("cannot validate [extract] in %s: %s", path, exc)
        return

    strategy = strategies.get(profile.extract.strategy)
    if strategy is None:
        raise TemplateError(
            f"{os.path.basename(path)}: unknown [extract].strategy "
            f"{profile.extract.strategy!r} (have: {', '.join(strategies.names())})")
    unknown = sorted(set(profile.extract.options) - set(strategy.options))
    if unknown:
        raise TemplateError(
            f"{os.path.basename(path)}: [extract] option(s) {', '.join(unknown)} are not "
            f"taken by the {strategy.name!r} strategy "
            f"(allowed: {', '.join(sorted(strategy.options))})")


def find(profiles: Sequence[Profile], name: str) -> Optional[Profile]:
    for profile in profiles:
        if profile.name == name:
            return profile
    return None


def describe(profiles: Sequence[Profile]) -> str:
    """The table `--list-profiles` prints."""
    width = max((len(p.name) for p in profiles), default=4)
    lines = [
        f"{'PROFILE'.ljust(width)}  PRI  STATUS      STRATEGY   TITLE",
    ]
    for profile in profiles:
        lines.append(
            f"{profile.name.ljust(width)}  {profile.priority:>3}  "
            f"{profile.status:<10}  {profile.extract.strategy:<9}  {profile.title}"
        )
    return "\n".join(lines)
