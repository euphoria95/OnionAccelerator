"""Command line front-end."""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
import textwrap
import time
from typing import Callable, Iterable, Iterator, Optional
from urllib.parse import urlsplit

from . import __version__, archive, progress, render
from .formats import detect as detect_mod
from .formats import rar, sevenzip
from .formats.xz import XzError
from .model import Entry
from .transport import (
    DEFAULT_PROXY,
    PooledTransport,
    Transport,
    TransportError,
    load_user_agents,
    probe,
)
from .util import human_bytes

# Event kinds published to an embedder's sink. Plain strings, and deliberately not an
# import: rvtree ships as its own package and must not acquire a dependency on
# OnionAccelerator's event stream to hand it a listing. The kinds are checked against
# that stream's table by tests/test_stream_contract.py in the parent project.
EVENT_OPEN = "tree.open"
EVENT_ENTRY = "tree.entry"
EVENT_DONE = "tree.done"

# What a live consumer of this listing looks like from in here: a callable, nothing
# more. Never blocks, never raises -- see _sink().
Sink = Callable[[str, dict], None]

log = logging.getLogger("rvtree.cli")

DESCRIPTION = (
    "List or extract from very large remote archives over Tor, without downloading "
    "them. Each supported format keeps its metadata somewhere a handful of HTTP Range "
    "requests can reach, so a 100 GB archive costs kilobytes to read."
)

# What each return value of ``main`` means, written down once so the help text and the
# README quote the same wording. A test checks the set is complete.
EXIT_CODES = (
    (0, "success"),
    (1, "the archive or the transfer failed — unreachable, unreadable, unparseable"),
    (2, "bad arguments, or a proxy setting that would leak the target hostname"),
    (
        3,
        "refused pending your decision — a single-block .xz, a server with no\n"
        "       ranges, or an extraction over --max-fetch. Re-run with --force,\n"
        "       --spool, or a higher --max-fetch",
    ),
    (4, "encrypted headers: the file list itself needs the password"),
    (130, "interrupted (Ctrl-C)"),
)

MODES = {
    "list": "list the archive's file tree",
    "extract": "write one member's bytes",
    "probe": "report server capabilities and archive shape",
}

# Written out rather than left to argparse: its generated usage reflows all fourteen
# shared options into every mode's line, burying the two or three flags that actually
# distinguish one mode from another. A test asserts this stays in step with the parsers.
SYNOPSIS = """\
rvtree list    URL [-f SHAPE] [--limit N] [--limit-blocks N] [--force]
                   [--no-size]
       rvtree extract URL PATH [-o FILE] [--max-fetch MiB] [--force]
       rvtree probe   URL [--multirange]
       rvtree help    [MODE]

       SHAPE is tree | long | json | ndjson. Every mode also takes the
       common options listed at the end."""

EXAMPLES = """\
Examples
  rvtree probe   https://example.onion/backup.tar.xz
      Start here. About 65 KiB, and it says whether the job ahead is cheap.

  rvtree list    https://example.onion/backup.tar.xz
  rvtree list    https://example.onion/big.tar.xz -f ndjson > tree.ndjson
      ndjson streams entries as they are found, so an interrupted run keeps what
      it had.

  rvtree list    https://example.onion/big.tar.xz --limit-blocks 4
      Sample a huge archive by reading only its first four xz blocks.

  rvtree extract https://example.onion/backup.tar.xz etc/hosts -o hosts
  rvtree list    http://127.0.0.1:8000/a.zip --proxy none
      Direct connection, no Tor — for local testing.

  rvtree list    https://example.onion/big.tar.xz -v
  rvtree list    https://example.onion/big.tar.xz -vv --log-file rv.log
      Show the pipeline while it runs; -vv adds one line per range request.
"""


def _width() -> int:
    """Help width, capped: prose past a hundred columns is harder to read, not easier."""
    return min(shutil.get_terminal_size((100, 24)).columns, 100)


class _Formatter(argparse.RawDescriptionHelpFormatter):
    """Wider help column, and ``-o, --output FILE`` instead of ``-o FILE, --output FILE``.

    Python 3.13's argparse renders the invocation this way already; the override is
    what makes 3.9 through 3.12 agree with it.
    """

    def __init__(self, prog, **kwargs):
        kwargs.setdefault("max_help_position", 34)
        kwargs.setdefault("width", _width())
        super().__init__(prog, **kwargs)

    def _format_action_invocation(self, action: argparse.Action) -> str:
        if not action.option_strings or action.nargs == 0:
            return super()._format_action_invocation(action)
        args = self._format_args(action, self._get_default_metavar_for_optional(action))
        return ", ".join(action.option_strings) + " " + args


def _common_parser() -> argparse.ArgumentParser:
    """Options every mode accepts, grouped so ``--help`` reads as a reference.

    argparse's ``parents=`` recreates these groups by title in each subparser, so the
    grouping survives into ``rvtree list --help`` as well.
    """
    common = argparse.ArgumentParser(add_help=False)

    net = common.add_argument_group("connection options")
    net.add_argument(
        "--proxy",
        metavar="URL",
        default=DEFAULT_PROXY,
        help=f"SOCKS proxy (default: {DEFAULT_PROXY}). Use 'none' for a direct "
        "connection. socks5:// is refused because it resolves DNS locally.",
    )
    net.add_argument(
        "--circuits",
        metavar="N",
        type=int,
        default=4,
        help="Number of isolated Tor circuits to fetch over (default: 4). "
        "Measured gain flattens past ~6.",
    )
    net.add_argument(
        "--endpoints",
        metavar="LIST",
        help="Comma-separated SOCKS5 endpoints, host:port[,host:port...]. Each is treated "
        "as an independent Tor daemon and gets its own circuits, which is where real "
        "parallel bandwidth comes from. Overrides --proxy.",
    )
    net.add_argument(
        "--endpoint-file",
        metavar="PATH",
        help="Read endpoints from this file, one per line; blank lines and # comments "
        "are skipped.",
    )
    net.add_argument(
        "--circuits-per-endpoint",
        metavar="N",
        type=int,
        default=1,
        help="Circuits to open on each endpoint (default: 1). Total lanes is this times "
        "the number of endpoints.",
    )
    net.add_argument(
        "--hedge",
        metavar="N",
        type=int,
        default=3,
        help="Copies of a small (under 64 KiB) range to race across endpoints, keeping "
        "the first answer (default: 3; 1 disables). Bulk reads are never duplicated. "
        "This is what makes a header chain, which cannot be parallelised, finish sooner.",
    )
    net.add_argument(
        "--walkers",
        metavar="N",
        type=int,
        help="Walk a RAR or plain tar header chain with this many walkers at once "
        "(default: one per lane; 1 walks serially). Each enters at a header verified by "
        "its own checksum, and is trusted only once the walker before it arrives there.",
    )
    net.add_argument(
        "--user-agents",
        metavar="PATH",
        help="Tab-separated file whose first column is a User-Agent. One is pinned per "
        "endpoint for the whole run.",
    )
    net.add_argument(
        "--timeout",
        metavar="SECONDS",
        type=int,
        default=120,
        help="Per-request read timeout (default: 120)",
    )
    net.add_argument(
        "--retries",
        metavar="N",
        type=int,
        default=3,
        help="Retries per range request (default: 3)",
    )

    tls = common.add_argument_group("tls options")
    tls.add_argument(
        "--verify-tls",
        action="store_true",
        help="Verify TLS certificates. Off by default: an onion address already "
        "authenticates the service, so onion sites rarely hold a CA-issued certificate. "
        "Worth passing for clearnet hosts, where the Tor exit node is untrusted.",
    )
    tls.add_argument(
        "--ca-bundle",
        metavar="PATH",
        help="Verify TLS against this CA bundle, for a private or self-signed CA "
        "(implies --verify-tls)",
    )

    fmt = common.add_argument_group("format options")
    fmt.add_argument(
        "--archive-type",
        metavar="TYPE",
        choices=("auto",) + detect_mod.SUPPORTED,
        default="auto",
        help="Force the archive format instead of detecting it: "
        + " | ".join(("auto",) + detect_mod.SUPPORTED)
        + " (default: auto, which reads the magic bytes, then Content-Disposition, "
        "Content-Type and the URL). Unrelated to -f/--format, which is the output shape.",
    )

    slow = common.add_argument_group("rangeless servers")
    slow.add_argument(
        "--spool",
        metavar="FILE",
        help="If the server refuses byte ranges, stream the whole entity to FILE and "
        "read the archive from there. The only thing that works against a download gate "
        "that hands the file out itself, and it costs a full transfer with no resume. "
        "FILE is kept, and an existing one of the right size is reused instead of "
        "fetching again.",
    )
    slow.add_argument(
        "--spool-temp",
        action="store_true",
        help="Same as --spool, into a temporary file that is deleted afterwards. Only "
        "worth it when the listing is all you wanted.",
    )

    dbg = common.add_argument_group("diagnostics")
    dbg.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress the summary line and the progress bar",
    )
    dbg.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Say what is happening, on stderr. -v names each stage of the run; "
        "-vv adds one line per range request, plus retries and readahead decisions.",
    )
    dbg.add_argument(
        "--debug",
        action="store_true",
        help="Everything -vv shows, plus timings, logger and thread names, and a full "
        "traceback instead of a one-line error.",
    )
    dbg.add_argument(
        "--progress",
        choices=("auto", "always", "never"),
        default="auto",
        help="Live progress bar on stderr. auto (the default) draws one for a terminal, "
        "and stays out of the way when payload is going to that same terminal.",
    )
    dbg.add_argument(
        "--log-file",
        metavar="PATH",
        help="Also write the full debug log here, whatever -v says. This is the file "
        "to attach to a bug report.",
    )
    return common


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rvtree",
        usage=SYNOPSIS,
        # Wrapped here rather than by argparse: the raw-description formatter that keeps
        # the epilog's layout intact necessarily leaves the description alone too.
        description=textwrap.fill(DESCRIPTION, width=_width() - 2),
        formatter_class=_Formatter,
    )
    p.add_argument("--version", action="version", version=f"rvtree {__version__}")

    common = _common_parser()
    # ``required=False`` so that a bare ``rvtree`` reaches main() and can print the
    # reference, rather than argparse's one-line "the following arguments are required".
    # ``prog`` is explicit because argparse otherwise derives each subparser's prog from
    # the parent's usage string, and ours is a four-line synopsis.
    sub = p.add_subparsers(dest="command", metavar="MODE", prog="rvtree")

    lst = sub.add_parser(
        "list",
        parents=[common],
        formatter_class=_Formatter,
        help=MODES["list"],
        description="List the archive's file tree.",
        epilog="Examples\n"
        "  rvtree list https://example.onion/backup.tar.xz\n"
        "  rvtree list https://example.onion/big.tar.xz -f ndjson > tree.ndjson\n"
        "  rvtree list https://example.onion/big.tar.xz --limit-blocks 4 -v\n",
    )
    lst.add_argument("url", metavar="URL", help="URL of the remote archive")
    lst.add_argument(
        "-f",
        "--format",
        metavar="SHAPE",
        choices=("tree", "long", "json", "ndjson"),
        default="tree",
        help="Output shape: tree | long | json | ndjson (default: tree). "
        "ndjson streams entries as they are found.",
    )
    lst.add_argument("--limit", metavar="N", type=int, help="Stop after this many entries")
    lst.add_argument(
        "--limit-blocks", metavar="N", type=int, help="Only read the first N xz blocks"
    )
    lst.add_argument(
        "--force",
        action="store_true",
        help="Proceed even when the archive has no random access (single-block .xz)",
    )
    lst.add_argument("--no-size", action="store_true", help="Omit sizes from tree output")

    ext = sub.add_parser(
        "extract",
        parents=[common],
        formatter_class=_Formatter,
        help=MODES["extract"],
        description="Extract a single member, touching as little of the archive as possible.",
        epilog="Examples\n"
        "  rvtree extract https://example.onion/backup.tar.xz etc/hosts -o hosts\n"
        "  rvtree extract https://example.onion/backup.7z big.iso --max-fetch 512\n",
    )
    ext.add_argument("url", metavar="URL", help="URL of the remote archive")
    ext.add_argument("path", metavar="PATH", help="Member path inside the archive")
    ext.add_argument(
        "-o", "--output", metavar="FILE", help="Write here instead of stdout"
    )
    ext.add_argument(
        "--force",
        action="store_true",
        help="Proceed even when a solid 7z folder makes this expensive",
    )
    ext.add_argument(
        "--max-fetch",
        type=int,
        default=64,
        metavar="MiB",
        help="Refuse an extraction that would download more than this (default: 64 MiB)",
    )

    prb = sub.add_parser(
        "probe",
        parents=[common],
        formatter_class=_Formatter,
        help=MODES["probe"],
        description="Report server capabilities and archive shape, for about 65 KiB.",
        epilog="Examples\n  rvtree probe https://example.onion/backup.tar.xz --multirange\n",
    )
    prb.add_argument("url", metavar="URL", help="URL of the remote archive")
    prb.add_argument("--multirange", action="store_true", help="Also test multipart/byteranges")

    hlp = sub.add_parser("help", help="show this reference, or one mode's options")
    hlp.add_argument(
        "topic",
        metavar="MODE",
        nargs="?",
        choices=tuple(MODES),
        help="list | extract | probe",
    )

    p.epilog = _full_reference(sub, common)
    return p


def _mode_parsers(parser: argparse.ArgumentParser) -> dict:
    """The subparsers, for ``rvtree help MODE``."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices
    return {}


def _full_reference(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> str:
    """Every mode and every option, rendered from the parsers themselves.

    Composed rather than written out, so an option added below can never go missing
    from the reference above it. Each mode shows only what is its own; the shared set
    is printed once at the end instead of three times.
    """
    shared = {action.dest for action in common._actions} | {"help"}
    f = _Formatter("rvtree")

    for name in MODES:
        parser = sub.choices[name]
        f.start_section(f"{name} — {MODES[name]}")
        f.add_arguments(
            [
                a
                for a in parser._actions
                if a.dest not in shared and a.help is not argparse.SUPPRESS
            ]
        )
        f.end_section()

    f.start_section("common options (accepted by every mode)")
    f.add_arguments(common._actions)
    f.end_section()

    codes = "Exit codes\n" + "".join(
        f"  {code:<4} {text}\n" for code, text in EXIT_CODES
    )
    return "\n".join((f.format_help(), EXAMPLES, codes))


# ----------------------------------------------------------------- diagnostics


class _LogFormat(logging.Formatter):
    """Errors keep the ``rvtree: message`` shape; diagnostics say where they came from."""

    def __init__(self, debug: bool = False):
        super().__init__()
        self.debug = debug

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if self.debug:
            head = f"{record.relativeCreated / 1000:7.3f}s {_short(record.name):<16} "
            if record.threadName and record.threadName != "MainThread":
                head += f"[{record.threadName}] "
            text = head + message
        elif record.levelno >= logging.WARNING:
            text = f"rvtree: {message}"
        elif record.name == "rvtree.stage":
            # The stage name is already the first word of the message.
            text = message
        else:
            text = f"{_short(record.name):<16} {message}"
        if record.exc_info:
            text += "\n" + self.formatException(record.exc_info)
        return text


def _short(name: str) -> str:
    return name[len("rvtree.") :] if name.startswith("rvtree.") else name


class _BarAwareHandler(logging.StreamHandler):
    """Emit through the reporter, so a log line never lands on top of a live bar."""

    def __init__(self, reporter: progress.Reporter):
        super().__init__(reporter.stream)
        self.reporter = reporter

    def emit(self, record: logging.LogRecord) -> None:
        with self.reporter.suspended():
            super().emit(record)


def _setup_logging(args, reporter: progress.Reporter) -> None:
    """Configure the ``rvtree`` logger only — never the root logger.

    Handlers are cleared first: ``main()`` is called repeatedly inside one pytest
    process, and without this every message would be emitted once per previous run.
    """
    root = logging.getLogger("rvtree")
    for handler in list(root.handlers):
        if not isinstance(handler, logging.NullHandler):
            root.removeHandler(handler)
            handler.close()

    console = _BarAwareHandler(reporter)
    console.setFormatter(_LogFormat(debug=args.debug))
    if args.debug or args.verbose >= 2:
        console.setLevel(logging.DEBUG)
    elif args.verbose == 1:
        console.setLevel(logging.INFO)
    elif args.quiet:
        console.setLevel(logging.ERROR)
    else:
        console.setLevel(logging.WARNING)
    root.addHandler(console)

    level = console.level
    if args.log_file:
        # Always the full story, whatever the console was asked for: this is the file
        # you attach to a bug report.
        to_file = logging.FileHandler(args.log_file, mode="w", encoding="utf-8")
        to_file.setFormatter(_LogFormat(debug=True))
        to_file.setLevel(logging.DEBUG)
        root.addHandler(to_file)
        level = logging.DEBUG

    root.setLevel(level)
    root.propagate = False


def _payload_goes_to_terminal(args) -> bool:
    """Would this run write payload to the same terminal the bar wants?

    ``probe`` prints its report to stdout as it goes, ndjson streams entries there, and
    ``extract`` without ``-o`` writes raw member bytes. A bar sharing that terminal
    would interleave with all three.
    """
    try:
        if not sys.stdout.isatty():
            return False
    except Exception:  # noqa: BLE001 - a stdout without isatty is not a terminal
        return False
    if args.command == "probe":
        return True
    if args.command == "list":
        return args.format == "ndjson"
    return args.command == "extract" and not args.output


def _endpoints(args) -> list[str]:
    """Endpoints from --endpoints and --endpoint-file, in the order given."""
    out: list[str] = []
    if args.endpoints:
        out += [p.strip() for p in args.endpoints.split(",") if p.strip()]
    if args.endpoint_file:
        with open(args.endpoint_file, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if line:
                    out.append(line)
    return out


def _build_transport(args):
    """Pick a transport: pooled when endpoints were named, the single-proxy one otherwise."""
    verify = args.ca_bundle or args.verify_tls
    endpoints = _endpoints(args)
    if not endpoints:
        return Transport(
            proxy=args.proxy,
            circuits=args.circuits,
            timeout=(60, args.timeout),
            retries=args.retries,
            verify=verify,
        )
    return PooledTransport(
        endpoints=endpoints,
        circuits_per_endpoint=args.circuits_per_endpoint,
        user_agents=load_user_agents(args.user_agents) if args.user_agents else None,
        timeout=(60, args.timeout),
        retries=args.retries,
        verify=verify,
        hedge=args.hedge,
    )


def main(argv: Optional[list[str]] = None, transport=None, sink=None) -> int:
    """Run one rvtree invocation.

    ``transport`` lets an embedder — OnionAccelerator's ``--mode tree`` — hand in a
    transport it has already built over its own proxy fleet. When it does, it keeps
    ownership: the caller opened it and the caller closes it.

    ``sink`` is the same idea for output: anything callable as ``sink(kind, data)``
    receives each member as it is walked, so a listing that takes an hour over Tor can
    be grepped while it runs instead of after it. Stdout is unaffected — a sink is an
    addition to the chosen ``--format``, never a replacement for it.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help(sys.stderr)
        return 2
    if args.command == "help":
        _mode_parsers(parser)[args.topic].print_help() if args.topic else parser.print_help()
        return 0

    when = "never" if _payload_goes_to_terminal(args) and args.progress == "auto" else args.progress
    reporter = progress.make(when, quiet=args.quiet)
    _setup_logging(args, reporter)

    owned = transport is None
    if owned:
        try:
            transport = _build_transport(args)
        except (TransportError, OSError) as exc:
            log.error("%s", exc, exc_info=args.debug)
            return 2

    reporter.attach(transport)
    try:
        with progress.activate(reporter):
            # The inner block closes the reporter before any handler below prints, so
            # an error message always lands on a line the bar has already vacated.
            if args.command == "list":
                return _cmd_list(args, transport, reporter, sink)
            if args.command == "extract":
                return _cmd_extract(args, transport, reporter)
            if args.command == "probe":
                return _cmd_probe(args, transport)
            return 2
    except (archive.SingleBlockWarning, archive.RangelessWarning) as exc:
        log.error("%s", exc, exc_info=args.debug)
        return 3
    except (sevenzip.EncryptedHeader, rar.EncryptedArchive) as exc:
        log.error("%s", exc, exc_info=args.debug)
        return 4
    except (
        archive.ArchiveError,
        TransportError,
        XzError,
        sevenzip.SevenZipError,
        rar.RarError,
    ) as exc:
        log.error("%s", exc, exc_info=args.debug)
        return 1
    except archive.PARSER_ERRORS as exc:
        # A backstop: archive.py converts these where it can say something useful, so
        # anything landing here is a parser giving up somewhere unanticipated. Still a
        # bad archive rather than a bug in rvtree, so report it like one.
        log.error("could not parse this archive: %s", exc, exc_info=args.debug)
        return 1
    except KeyboardInterrupt:
        log.error("interrupted", exc_info=args.debug)
        return 130
    finally:
        if owned:
            transport.close()


def _open(args, transport: Transport) -> archive.Archive:
    fmt = None if args.archive_type == "auto" else args.archive_type
    return archive.open_archive(
        transport,
        args.url,
        fmt=fmt,
        spool_to=args.spool,
        allow_spool=bool(args.spool or args.spool_temp),
    )


def _counted(
    entries: Iterable[Entry],
    reporter: progress.Reporter,
    arc: archive.Archive,
    sink: Optional[Sink] = None,
) -> Iterator[Entry]:
    """Count entries as they stream past, and announce the walk when it truly starts.

    A generator on purpose: ``list_archive`` hands back a lazy iterator, so the work it
    describes begins on the first ``next()`` rather than when it returned.

    It is also where an embedder's ``sink`` sees each member, for the same reason the
    counter lives here: every entry the walk produces passes through this loop exactly
    once, whatever format it came out of and whichever renderer is about to consume it.
    The published payload is ``render.as_dict``, so a streamed member and a member from
    ``-f ndjson`` are the same object.
    """
    reporter.stage("walk", detect_mod.describe(arc.fmt))
    source = arc.walk_source
    if source is not None:
        reporter.track(source, getattr(source, "size", 0) or 0)
    for entry in entries:
        reporter.entry()
        if sink is not None:
            _sink(sink, EVENT_ENTRY, render.as_dict(entry))
        yield entry


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _sink(sink: Sink, kind: str, data: dict) -> None:
    """Publish one event. A failing consumer must never take the listing down with it."""
    try:
        sink(kind, data)
    except Exception as exc:  # noqa: BLE001 - a broken consumer is not a broken archive
        log.debug("event sink failed on %s: %s", kind, exc)


def _walkers(args, transport) -> int:
    """How many walkers to split a header chain between.

    Defaults to one per lane, because a walker spends its life waiting on a round trip
    and a lane is exactly the thing that can carry one.
    """
    if args.walkers is not None:
        return max(1, args.walkers)
    return max(1, getattr(transport, "lanes", 1))


def _cmd_list(args, transport: Transport, reporter: progress.Reporter,
              sink: Optional[Sink] = None) -> int:
    arc = _open(args, transport)
    fmt = arc.fmt
    if sink is not None:
        # Published before the walk starts. Opening an archive over Tor is itself
        # minutes of work -- a proof-of-work gate, a spool of a rangeless server -- so a
        # consumer that hears the format and the size knows the run got that far, and
        # roughly what the silence before the first entry is going to cost.
        _sink(sink, EVENT_OPEN, {
            "url": args.url,
            "format": fmt,
            "format_name": detect_mod.describe(fmt),
            "size": arc.size,
            "at": _now(),
        })
    entries = _counted(
        archive.list_archive(
            arc,
            force=args.force,
            circuits=args.circuits,
            limit=args.limit,
            limit_blocks=args.limit_blocks,
            segments=_walkers(args, transport),
        ),
        reporter,
        arc,
        sink,
    )

    if args.format == "ndjson":
        count = render.render_ndjson(entries, sys.stdout)
        total = None
    else:
        items = list(entries)
        count = len(items)
        total = sum(e.size for e in items)
        # Nothing may reach stdout while the bar owns the bottom of the terminal.
        reporter.close()
        reporter.stage("render", args.format)
        if args.format == "tree":
            render.render_tree(items, sys.stdout, show_size=not args.no_size)
        elif args.format == "long":
            render.render_long(items, sys.stdout)
        else:
            render.render_json(
                items,
                sys.stdout,
                meta={
                    "url": args.url,
                    "format": fmt,
                    "entries": count,
                    "total_size": total,
                    "bytes_fetched": transport.bytes_fetched,
                    "requests": transport.requests_made,
                },
            )
    reporter.close()

    if sink is not None:
        _sink(sink, EVENT_DONE, {
            "url": args.url,
            "format": fmt,
            "entries": count,
            "total_size": total,
            "bytes_fetched": transport.bytes_fetched,
            "requests": transport.requests_made,
            "at": _now(),
        })

    if not args.quiet and args.format != "json":
        pct = transport.bytes_fetched / arc.size * 100 if arc.size else 0
        summary = (
            f"{count:,} entries"
            + (f", {human_bytes(total)} uncompressed" if total is not None else "")
            + f" | fetched {human_bytes(transport.bytes_fetched)} of "
            f"{human_bytes(arc.size)} ({pct:.3f}%) in {transport.requests_made} requests"
        )
        print(f"\n{detect_mod.describe(fmt)}: {summary}", file=sys.stderr)
    return 0


def _cmd_extract(args, transport: Transport, reporter: progress.Reporter) -> int:
    arc = _open(args, transport)

    reporter.stage("locate", args.path)
    cost = archive.extraction_cost(arc, args.path)
    if cost > args.max_fetch * 1024 * 1024 and not args.force:
        log.error(
            "extracting %r requires downloading %s, over the %d MiB limit. "
            "Re-run with --force or raise --max-fetch.",
            args.path,
            human_bytes(cost),
            args.max_fetch,
        )
        return 3

    # The only bar in the tool with an exact denominator: the cost is known before the
    # first byte of it moves.
    baseline = transport.bytes_fetched
    reporter.stage("fetch", human_bytes(cost) if cost else "")
    if cost:
        # In-flight bytes included: a solid folder comes down in one long request, and
        # completed-request accounting alone would leave this bar at 0% until it lands.
        reporter.track_bytes(
            lambda: transport.bytes_fetched + transport.bytes_inflight - baseline, cost
        )
    data = archive.extract(arc, args.path, circuits=args.circuits)
    reporter.close()

    if cost and not args.quiet and len(data) and cost > 2 * len(data):
        # Solid compression means the whole group comes down together; worth saying out
        # loud, because it is why a small file can take a long time. The comparison above
        # is what distinguishes a solid member from a merely large one.
        where = "a solid 7z folder" if arc.fmt == detect_mod.SEVENZIP else "a solid block"
        print(
            f"note: {args.path!r} is in {where}, so extracting "
            f"{human_bytes(len(data))} required {human_bytes(cost)} of downloading",
            file=sys.stderr,
        )
    if args.output:
        with open(args.output, "wb") as fh:
            fh.write(data)
        if not args.quiet:
            print(
                f"wrote {human_bytes(len(data))} to {args.output} "
                f"(fetched {human_bytes(transport.bytes_fetched)})",
                file=sys.stderr,
            )
    else:
        sys.stdout.buffer.write(data)
    return 0


def _cmd_probe(args, transport: Transport) -> int:
    caps = probe(transport, args.url, check_multirange=args.multirange)
    print(f"url            {caps.url}")
    print(f"size           {caps.size:,} bytes ({human_bytes(caps.size)})")
    if caps.gated:
        print("gate           proof-of-work — solved; the size above is the real entity")
    ranges = "yes"
    if not caps.accepts_ranges:
        ranges = "NO — only --spool can read this archive, at a full download"
    print(f"accept-ranges  {ranges}")
    if args.multirange:
        print(f"multi-range    {'yes' if caps.multirange else 'no (single ranges only)'}")
    print(f"server         {caps.server or '-'}")
    print(f"content-type   {caps.content_type or '-'}")
    print(f"etag           {caps.etag or '-'}")
    print(f"last-modified  {caps.last_modified or '-'}")
    if urlsplit(caps.url).scheme == "https":
        state = "verified" if transport.verify else "not verified (default; --verify-tls to enforce)"
        print(f"tls            {state}")

    if caps.accepts_ranges or args.spool or args.spool_temp:
        try:
            arc = _open(args, transport)
            det = arc.detection
            print(
                f"format         {arc.fmt} ({detect_mod.describe(arc.fmt)})"
                + (f" via {det.via}" if det else "")
            )
            _print_xz_shape(arc)
            _print_rar_shape(arc)
        except Exception as exc:  # noqa: BLE001 - probe should never hard-fail
            print(f"format         could not determine ({exc})")
    print(f"fetched        {human_bytes(transport.bytes_fetched)} in {transport.requests_made} requests")
    return 0


def _print_xz_shape(arc) -> None:
    index = arc.xz_index
    if index is None:
        return
    print(f"xz blocks      {len(index.blocks)} in {index.streams} stream(s)")
    print(f"uncompressed   {index.uncompressed_size:,} bytes ({human_bytes(index.uncompressed_size)})")
    if index.seekable:
        avg = index.uncompressed_size / len(index.blocks)
        print(f"random access  yes — {human_bytes(avg)} granularity")
    else:
        print("random access  NO — single block, listing requires the whole stream")


def _print_rar_shape(arc) -> None:
    """The two facts that change what you can do next: is it solid, is it a volume."""
    if arc.fmt != detect_mod.RAR:
        return
    info = arc.rarinfo()
    print(f"rar headers    RAR {'5.0' if info.version == 5 else '4.x'}")
    if info.base_offset:
        print(f"sfx stub       {human_bytes(info.base_offset)} ahead of the signature")
    if info.solid:
        print("solid          yes — a member can only be reached through those before it")
    else:
        print("solid          no")
    if info.volume:
        number = f" {info.volume_number}" if info.volume_number is not None else ""
        print(f"volume         yes{number} — split members need the other volumes too")
    else:
        print("volume         no")


if __name__ == "__main__":
    sys.exit(main())
