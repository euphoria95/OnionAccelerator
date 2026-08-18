"""The long-form reference printed by ``OnionAccelerator.py --full-help [MODE]``.

Plain ``--help`` is argparse's: one flat ``options:`` block that never says which of the
twenty-odd flags belong to which mode, and reflows every carefully written sentence into
the same paragraph. This module is the other half — per mode, what it does, which flags
actually reach it, what their defaults are and why, and what it costs to get them wrong.

It is deliberately import-free: ``--full-help`` must work on a machine with neither
aiohttp nor the vendored rvtree installed, which is exactly the machine whose user is
reading it. Nothing here imports from OnionAccelerator either, so the drift tests in
tests/test_cli.py can compare the two without a cycle.

The structure mirrors remote_viewer/rvtree/cli.py, which had this idea first: MODES and
EXIT_CODES are exported constants so the tests assert against a source of truth rather
than against a string somebody typed twice.
"""

from __future__ import annotations

import textwrap

# Everything is rendered to this width. Not the terminal's: this is a reference meant to
# be paged and pasted, and prose that reflows to 210 columns on a wide terminal is prose
# nobody finishes a line of.
WIDTH = 96

# mode -> one-line summary. The source of truth for --mode's own help and for the tests
# that check every mode has a section.
MODES = {
    "multi": "download every URL in parallel, one worker per proxy",
    "partial": "download each URL as parallel byte-range chunks across the proxies",
    "speedtest": "measure every proxy and check the URLs are reachable",
    "tree": "list or extract members of a huge remote archive without downloading it",
    "crawl": "walk an open directory recursively and write a manifest of every file",
}

EXIT_CODES = (
    (0, "Success. For crawl, at least one directory was listed."),
    (1, "Failure: no usable proxies, no URLs.txt and no --url, an unimportable\n"
        "optional package (aiohttp for crawl, rvtree for tree), a bad\n"
        "--include/--exclude regex, or a crawl that listed no directory at all."),
    (2, "Usage error from argparse: an unknown flag, a bad --mode/--farm\n"
        "combination, or a --url with no scheme."),
)


# --------------------------------------------------------------------------- content

_URL_FLAG = (
    "--url URL",
    "A target URL, given on the command line instead of read from URLs.txt. "
    "Repeat it for several. When --url is given at least once it replaces the "
    "file entirely -- URLs.txt is not read and need not exist.",
)

_PROXY_FLAGS = [
    ("--external",
     "Fetch ip:port SOCKS5 proxies from the public list instead of using local Tor. "
     "The list is re-fetched every run and never written to disk, capped at 100, and "
     "connectivity-checked before any work starts. It routes your traffic through "
     "untrusted third parties, which is weaker than a local Tor setup, and the script "
     "says so every time."),
    ("--test-url URL",
     "The URL used to verify --external proxy liveness, overriding the PROXY_TEST_URL "
     "constant. Point it at an endpoint you control. With neither set, a random one of "
     "your real targets is used and is thereby revealed to every candidate proxy, "
     "including the ones that fail and are discarded. Only read when --external is on."),
]

_RETRY_FLAG = (
    "--retries N",
    "Attempts per failed download before giving up (default 3). Each retry draws a "
    "fresh proxy from the pool, so a dead endpoint costs one attempt, not the URL.",
)

_PRESERVE_FLAG = (
    "--preserve-path",
    "Mirror each URL's remote directory tree under downloads/<host>/ instead of "
    "flattening every file to its basename. Use it for anything that came out of a "
    "crawl: an open directory routinely holds a README.txt or Thumbs.db in every "
    "subdirectory, and the flat layout silently keeps only the last one.",
)

_FARM_RANGE_FLAGS = [
    ("--base-port PORT",
     "First port of the local farm range to probe (default 5000)."),
    ("--count N",
     "How many ports of that range to probe (default 20). Together with --base-port "
     "this is the window the endpoint probe looks at; ports outside it are ignored."),
]

SECTIONS = {
    "multi": {
        "what": [
            "Downloads every target in parallel, one worker thread per proxy, capped at "
            "the number of URLs so no idle threads are spawned. Progress is aggregated "
            "into a single bar.",
            "Files land in downloads/<host>/, one subdirectory per hostname, so two URLs "
            "from different hosts that share a filename never overwrite each other. "
            "Within one host they still can -- that is what --preserve-path is for.",
        ],
        "flags": [
            _URL_FLAG,
            _RETRY_FLAG,
            _PRESERVE_FLAG,
        ] + _PROXY_FLAGS,
        "examples": [
            ("python3 OnionAccelerator.py --mode multi --url https://example.onion/a.iso",
             "one file, no URLs.txt needed"),
            ("python3 OnionAccelerator.py --mode multi",
             "every line of URLs.txt"),
            ("python3 OnionAccelerator.py --mode multi --preserve-path --retries 5",
             "a crawl manifest, paths kept"),
        ],
    },
    "partial": {
        "what": [
            "Splits each file into parallel byte-range chunks -- up to one per proxy, but "
            "never more chunks than the size warrants, since a chunk is at least 1 MB -- "
            "and merges them when they all land. A failed chunk is retried on a different "
            "proxy, not the one that just died.",
            "The size is established by a ladder of four probes, none of which transfers a "
            "payload: a HEAD; then the size= parameter of that same response's "
            "Content-Disposition; then the total in the Content-Range of a one-byte "
            "'Range: bytes=0-0' GET; and finally a plain streamed GET aborted the moment "
            "its headers arrive. A server that withholds Content-Length therefore no "
            "longer costs you the parallelism.",
            "If all four fail, the download falls back to a single sequential GET with its "
            "own progress bar. So does a response that arrives under a Content-Encoding: "
            "the length it advertises describes the compressed transfer rather than the "
            "file, so it cannot be split into ranges safely.",
        ],
        "flags": [
            _URL_FLAG,
            _RETRY_FLAG,
            _PRESERVE_FLAG,
        ] + _PROXY_FLAGS,
        "examples": [
            ("python3 OnionAccelerator.py --mode partial --url https://example.onion/big.iso",
             "one big file, chunked"),
            ("python3 OnionAccelerator.py --mode partial",
             "every line of URLs.txt"),
        ],
    },
    "speedtest": {
        "what": [
            "Tests every SOCKS5 port in parallel and prints a per-port summary of speed "
            "and failures. The transfer target is the first URL; the remaining ones are "
            "then checked for availability through a randomly chosen working port, rather "
            "than always through the first.",
            "Run it after '--farm up' and before a long download: it is the cheapest way "
            "to find out that four of your twenty instances never bootstrapped.",
        ],
        "flags": [
            _URL_FLAG,
        ] + _PROXY_FLAGS,
        "examples": [
            ("python3 OnionAccelerator.py --mode speedtest --url https://example.onion/1MB.bin",
             "measure against one known file"),
            ("python3 OnionAccelerator.py --mode speedtest --external",
             "grade the public proxy list"),
        ],
    },
    "tree": {
        "what": [
            "Lists or extracts members of a huge remote archive (.rar, .tar, .zip, .7z, "
            ".tar.xz) using HTTP Range requests, without downloading it. A 100 GB archive "
            "costs kilobytes to read, because every supported format keeps its metadata "
            "somewhere a handful of range requests can reach.",
            "The work is done by the vendored rvtree package, driven over the same "
            "host:port SOCKS5 pool the download modes use. Everything after '--' is passed "
            "to rvtree untouched, so its full list / extract / probe surface is available "
            "with no flags here to keep in sync. Run '-- --help' to see it.",
            "Why the farm matters here: rvtree's own default is one Tor daemon with "
            "several credential-isolated circuits, which is one guard and one "
            "single-threaded crypto path, so the circuits queue behind each other. This "
            "mode replaces that with independent daemons -- round-robin lanes with "
            "failover, size-aware range splitting, small header reads hedged across two "
            "or three different daemons with the first answer winning, and parallel "
            "segmented walking for RAR and plain tar.",
            "With no farm up it falls back to the local Tor client on 127.0.0.1:9150, "
            "warns that this is a single daemon, and suggests bringing a farm up. It "
            "works; it is also the thing that made it slow in the first place.",
        ],
        "flags": [
            ("--url URL",
             "The archive URL, injected as rvtree's positional argument so you do not "
             "have to remember where it goes. '--url X -- extract etc/hosts -o h' becomes "
             "'extract X etc/hosts -o h'. With no '--' block at all it defaults to "
             "'list X'. Giving the URL both ways, or giving more than one, is an error: "
             "rvtree reads one archive per run."),
            ("-- ARGS...",
             "Everything after the separator goes to rvtree verbatim: a subcommand (list, "
             "extract or probe), the archive URL if you did not pass --url, and whatever "
             "flags that subcommand takes. '-v' traces the pipeline, '-vv' logs one line "
             "per range request."),
            _RETRY_FLAG,
        ] + _PROXY_FLAGS + _FARM_RANGE_FLAGS,
        "examples": [
            ("python3 OnionAccelerator.py --mode tree --url https://example.onion/backup.rar",
             "what is in it, and what did it cost"),
            ("python3 OnionAccelerator.py --mode tree -- probe https://example.onion/b.tar.xz",
             "server capabilities first, ~65 KiB"),
            ("python3 OnionAccelerator.py --mode tree --url https://example.onion/b.rar \\\n"
             "    -- extract etc/hosts -o hosts",
             "pull one member out"),
            ("python3 OnionAccelerator.py --mode tree --url https://example.onion/big.tar.xz \\\n"
             "    -- list -f ndjson > tree.ndjson",
             "stream it, so an interrupt keeps what it had"),
            ("python3 OnionAccelerator.py --mode tree -- --help",
             "rvtree's own reference"),
        ],
        "notes": [
            "rvtree writes its own full debug log to logs/rvtree_<job_id>.log every run, "
            "and its exit code is passed straight through -- so 3, 4 and 130 come from "
            "rvtree and are documented in its help, not in the table below.",
        ],
    },
    "crawl": {
        "what": [
            "The one mode that discovers URLs instead of consuming a list of them. It "
            "walks an 'Index of /' open directory recursively from each seed and writes a "
            "manifest of every file it finds.",
            "The listing parser is server-agnostic: it reads every <a href> on the page "
            "and discards junk structurally rather than by matching templates. Anything "
            "that does not resolve strictly below the directory being listed is "
            "navigation, which kills '../', '/', 'Parent Directory' and every breadcrumb "
            "on every server without matching a word of link text. Apache, nginx, "
            "lighttpd, Caddy, hand-rolled templates and nginx's JSON autoindex all come "
            "out as the same directories/files split.",
            "It also walks the file managers leak sites run, which serve a whole tree from "
            "one route and encode the path into it -- /r/filemanager/TOK/dumps%2Fraw/x -- "
            "rather than serving the tree at its own paths. Every scope, parent and "
            "dedup test reads the path through one function that decodes before splitting "
            "on '/', so a folded separator is not mistaken for a directory literally "
            "named 'dumps/raw' and written off as off-tree.",
            "Where a target's listing is not links at all -- a path in ?p=, a JSON API "
            "behind a JavaScript shell, a WebDAV collection, an S3 bucket, or the target's "
            "own published 'tree' dump -- a listing template says so, and the crawler asks "
            "for each directory the way that target expects, up to and including a POST "
            "whose body names the directory. Run --detect against an unknown target to be "
            "told which template fits and what to pass; --list-profiles shows them all.",
            "Concurrency is circuits, not just threads. Each endpoint is opened "
            "--circuits-per-endpoint times and each circuit gets its own SOCKS "
            "username/password, which is what makes them separate Tor circuits rather "
            "than one shared one. Lanes go round-robin across daemons; a failing endpoint "
            "is parked after three consecutive failures, a dropped circuit is rotated "
            "rather than retried into the same hole, timeouts and 429/502/503/504 back "
            "off exponentially with full jitter, and 401/403/404/410 are recorded once "
            "and never retried.",
        ],
        "flags": [
            ("--url URL",
             "A seed open-directory URL, repeatable, in place of URLs.txt. Every seed is "
             "depth 0."),
            ("--socks LIST",
             "Comma-separated host:port SOCKS5 endpoints to crawl through, e.g. "
             "'127.0.0.1:9050,127.0.0.1:9052'. Overrides farm discovery and --external. "
             "This is how you point the crawler at Tor daemons this script does not "
             "manage; the --farm tooling never touches them."),
            ("--max-depth N",
             "Directory levels below each seed. Default: unlimited -- harvesting a whole "
             "open directory is the point of the mode. A finite tree terminates on its "
             "own, because deduplication means it cannot recurse into itself; a symlink "
             "loop does not, because it produces a genuinely new URL at every level. "
             "Those runs need --max-depth, --max-pages or --time-budget to end."),
            ("--order {bfs,dfs}",
             "Traversal order. bfs (default) maps the whole tree shallow-first; dfs "
             "finishes branches."),
            ("--switch-after N",
             "Flip from --order to the other one once N directories have been listed. "
             "Maps the shape of the tree breadth-first, then dives. It re-orders work "
             "that is already queued, which a priority queue could not."),
            ("--workers N",
             "Concurrent workers. Default: one per circuit, and capped at the circuit "
             "count -- extra workers would only queue."),
            ("--circuits-per-endpoint N",
             "Isolated circuits per SOCKS endpoint (default 2). Raise it when you have "
             "few daemons and a host that tolerates load."),
            ("--per-host N",
             "Concurrent requests against any one target host (default 8). An onion "
             "service is usually one small process; past this it starts refusing "
             "connections."),
            ("--max-pages N",
             "Stop after listing this many directories. Default: unlimited."),
            ("--time-budget SECONDS",
             "Stop after this many seconds and write the report. Every stop is clean: "
             "the report is streamed and flushed per line, so even Ctrl-C leaves a valid "
             "manifest, complete as far as it got."),
            ("--max-page-bytes N",
             "Abandon a body larger than this instead of parsing it, recording the fetch "
             "as a failure (default 16 MiB). One directory of a leaked fileshare can be "
             "thousands of rows, so raise this rather than lose its subtree."),
            ("--include REGEX",
             "Only crawl directory URLs matching this regex."),
            ("--exclude REGEX",
             "Never crawl directory URLs matching this regex."),
            ("--allow-offsite",
             "Follow links off the seed hosts. Off by default: on Tor this is how a "
             "directory walk becomes an unbounded crawl."),
            ("--download",
             "After crawling, download every discovered file through the same proxies as "
             "a second, sequential phase -- sequential on purpose, because the download "
             "stack is threads over requests and running it alongside the event loop "
             "would put two unrelated concurrency models on the same daemons at once. "
             "Paths are always preserved under downloads/<host>/, never flattened."),
            ("--detect",
             "Fetch each seed once, report which listing templates match it and what each "
             "one reads out of it, print the --profile command to crawl with, and stop. "
             "One request against thousands: run this first on a target you do not know."),
            ("--profile NAME",
             "Read every page with this template instead of detecting one per host. "
             "Required for API-driven targets (AList, WebDAV, h5ai, FileGator): it is "
             "what lets the seed itself be requested in the target's own scheme, rather "
             "than fetched as a page that has no listing in it."),
            ("--list-profiles",
             "Print the available templates and exit -- name, priority, status, strategy. "
             "'verified' means a fixture in the test suite pins that template against a "
             "real listing; 'unverified' means it was written from documented request "
             "shapes and wants checking against a live target."),
            ("--templates DIR",
             "Load extra templates from DIR (repeatable). One of the same name replaces a "
             "built-in, which is how a profile for one engagement's target stays out of "
             "the repository."),
            _RETRY_FLAG,
        ] + _PROXY_FLAGS + _FARM_RANGE_FLAGS,
        "examples": [
            ("python3 OnionAccelerator.py --mode crawl --url https://example.onion/dumps/",
             "one seed, whole tree"),
            ("python3 OnionAccelerator.py --mode crawl --detect --url https://example.onion/",
             "what is this target, and how is it read?"),
            ("python3 OnionAccelerator.py --mode crawl --url https://example.onion/ \\\n"
             "    --profile tiny-file-manager",
             "a manager whose paths live in ?p="),
            ("python3 OnionAccelerator.py --mode crawl --profile tree-dump \\\n"
             "    --url https://example.onion/List_of_files.txt",
             "read the target's own index: one request, whole tree"),
            ("python3 OnionAccelerator.py --mode crawl --max-depth 2 --max-pages 50",
             "a quick reconnaissance pass"),
            ("python3 OnionAccelerator.py --mode crawl --order bfs --switch-after 200",
             "map the shape, then dive"),
            ("python3 OnionAccelerator.py --mode crawl --include '/(dumps?|leaks)/' \\\n"
             "    --exclude '/thumbs/'",
             "only the dumps, never the thumbnails"),
            ("python3 OnionAccelerator.py --mode crawl --socks 127.0.0.1:9050,127.0.0.1:9052 \\\n"
             "    --circuits-per-endpoint 4",
             "daemons this script does not manage"),
            ("python3 OnionAccelerator.py --mode crawl --url https://example.onion/ --download",
             "map it, then pull it all down"),
        ],
        "notes": [
            "Reading a page and crawling it are separate: the engine moves bytes, and a "
            "listing template decides what they mean. A template is a small TOML file "
            "saying how to recognise a target, where its rows are, and how a child "
            "directory is addressed -- a path, a query parameter, an escaped segment, or "
            "a field in an API request. Targets nothing matches are read structurally, "
            "exactly as before templates existed.",
            "Output lands in crawls/<job_id>/ : listing.jsonl (one record per file), "
            "dirs.jsonl (one per directory, where is_index=false marks a page that was "
            "read and refused by the confidence guard), failed.jsonl, stats.json, "
            "tree.txt for eyeballing, and urls.txt -- bare file URLs ready to feed back "
            "in with 'cp crawls/<job_id>/urls.txt URLs.txt'.",
            "The periodic progress line's per-endpoint balance "
            "(127.0.0.1:9050=214 127.0.0.1:9052=209/3f) is the one number that says "
            "whether the multi-circuit spread is actually working.",
            "This mode needs aiohttp, aiohttp-socks, beautifulsoup4 and lxml, and Python "
            "3.8+. The other modes run without any of them.",
        ],
    },
}

FARM = {
    "summary": "provision and manage a farm of native client-only Tor instances",
    "what": [
        "Not a --mode, and mutually exclusive with one. --farm provisions SOCKS5 proxies "
        "using the Debian/Ubuntu tor-instance-create + systemd tor@<name> stack, with no "
        "containers. Each instance is client-only (ClientOnly 1, no ORPort or DirPort, so "
        "it never publishes a relay descriptor) and binds 127.0.0.1:<base-port + NN>, "
        "which is exactly where the other modes look for it.",
        "It is namespaced and it stays in its lane: every privileged action is gated by a "
        "name guard that matches ^oa[0-9]+$ and excludes 'default', 'relay2' and "
        "'torclient'. A stray '--farm destroy' cannot stop or delete your own relay or "
        "client instances -- only oaNN are ever touched.",
        "'up' and 'down'/'destroy' need root. 'status' does not.",
    ],
    "flags": [
        ("--farm up",
         "Create, start and wait for each instance to report 'Bootstrapped 100%' with its "
         "SOCKS port listening."),
        ("--farm status",
         "Report each instance's systemd state, SOCKS port and bootstrap percentage."),
        ("--farm down",
         "Stop the farm, keeping configs and data."),
        ("--farm destroy",
         "Stop the farm and purge it entirely."),
        ("--count N",
         "How many instances '--farm up' creates (default 20)."),
        ("--base-port PORT",
         "First SOCKS port (default 5000); instance NN binds base_port + NN."),
        ("--bootstrap-timeout SECONDS",
         "How long to wait for each instance to bootstrap on '--farm up' (default 120). "
         "Only read by '--farm up'."),
    ],
    "examples": [
        ("sudo python3 OnionAccelerator.py --farm up --count 20", "bring proxies up"),
        ("python3 OnionAccelerator.py --farm status", "check on them"),
        ("sudo python3 OnionAccelerator.py --farm destroy", "tear down when done"),
    ],
}

COMMON = [
    ("Targets",
     ["Every mode but --farm needs at least one URL. Pass them with --url (repeatable), "
      "or leave it off and they are read from URLs.txt in the working directory, one per "
      "line, blank lines ignored. --url wins outright: when it is given the file is not "
      "read at all and need not exist. There is no way to use both in one run, on "
      "purpose -- a half-remembered file left over from the last job is the failure mode "
      "this avoids."]),
    ("Where the proxies come from",
     ["--socks if given (crawl only), else --external, else a live probe of "
      "127.0.0.1:<base-port> .. <base-port + count - 1>, which is why a farm of any size "
      "just works without editing constants. Tree and crawl then fall back to the local "
      "Tor client on 127.0.0.1:9150 and say so; the download modes instead fall back to "
      "the fixed twenty-port list, a deliberate bet on a Docker setup that raced the "
      "probe. Note that the download modes always probe the default range: --base-port "
      "and --count steer the farm, tree and crawl."]),
    ("Output",
     ["downloads/<host>/ for multi, partials/<host>/ for partial's chunk merge, and "
      "crawls/<job_id>/ for crawl manifests. Add --preserve-path to mirror the remote "
      "directory tree instead of flattening to basenames; --mode crawl --download always "
      "does."]),
    ("Logging",
     ["Every run gets a job_id. DEBUG goes to logs/OnionAccelerator_<job_id>.log, INFO to "
      "the console, and tree mode additionally gets logs/rvtree_<job_id>.log. Nothing is "
      "written at import time, so importing this script from a test is free."]),
    ("User-Agents",
     ["Drawn at random from the first column of UserAgents.tsv (tab-separated; later "
      "columns such as usage weights are ignored). Crawl pins one per circuit for the "
      "circuit's lifetime -- a UA that changes per request on a fixed circuit is itself a "
      "fingerprint."]),
]


# --------------------------------------------------------------------------- rendering

def _para(text, indent=0, width=WIDTH):
    pad = " " * indent
    return textwrap.fill(text, width=width, initial_indent=pad, subsequent_indent=pad)


def _flag(name, text, indent=2, hang=4, width=WIDTH):
    """A flag and its description as a hanging-indent block."""
    pad = " " * indent
    body = textwrap.fill(text, width=width,
                         initial_indent=pad + " " * hang,
                         subsequent_indent=pad + " " * hang)
    return f"{pad}{name}\n{body}"


def _example(command, comment, indent=2):
    pad = " " * indent
    lines = [f"{pad}# {comment}"] if comment else []
    lines += [pad + line for line in command.splitlines()]
    return "\n".join(lines)


def _heading(text):
    return f"{text}\n{'-' * len(text)}"


def _block(title, what, flags, examples, notes=()):
    out = [_heading(title), ""]
    for paragraph in what:
        out.append(_para(paragraph))
        out.append("")
    if flags:
        out.append("Arguments")
        out.append("")
        for name, text in flags:
            out.append(_flag(name, text))
            out.append("")
    if examples:
        out.append("Examples")
        out.append("")
        for command, comment in examples:
            out.append(_example(command, comment))
            out.append("")
    for note in notes:
        out.append(_para(note))
        out.append("")
    return "\n".join(out)


def _mode_block(mode):
    section = SECTIONS[mode]
    return _block(f"--mode {mode}  --  {MODES[mode]}",
                  section["what"], section["flags"], section["examples"],
                  section.get("notes", ()))


def _farm_block():
    return _block(f"--farm  --  {FARM['summary']}",
                  FARM["what"], FARM["flags"], FARM["examples"])


def _common_block():
    out = [_heading("Common to every mode"), ""]
    for title, paragraphs in COMMON:
        out.append(f"  {title}")
        for paragraph in paragraphs:
            out.append(_para(paragraph, indent=4))
        out.append("")
    return "\n".join(out)


def _exit_codes_block():
    out = [_heading("Exit codes"), ""]
    for code, text in EXIT_CODES:
        first, *rest = text.split("\n")
        out.append(_flag(str(code), " ".join([first] + rest), indent=2, hang=4))
        out.append("")
    return "\n".join(out)


_SYNOPSIS = """\
OnionAccelerator -- multi/partial download, remote-archive listing and open-directory
crawling over a pool of SOCKS5 proxies (normally Tor), plus a native Tor-instance farm.

  python3 OnionAccelerator.py --mode multi|partial|speedtest [--url URL ...] [OPTIONS]
  python3 OnionAccelerator.py --mode tree [--url URL] [OPTIONS] [-- RVTREE ARGS...]
  python3 OnionAccelerator.py --mode crawl [--url URL ...] [OPTIONS]
  python3 OnionAccelerator.py --farm up|status|down|destroy [OPTIONS]

Exactly one of --mode or --farm is required. '--full-help MODE' narrows this reference to
a single mode."""


def render(mode=None):
    """The reference, whole or for one mode.

    `mode` is a key of MODES, or "farm", or None for everything. Unknown names are the
    caller's problem to reject -- the CLI validates against MODES before calling.
    """
    if mode == "farm":
        return "\n".join([_farm_block(), _common_block(), _exit_codes_block()]).rstrip() + "\n"
    if mode:
        return "\n".join([_mode_block(mode), _common_block(),
                          _exit_codes_block()]).rstrip() + "\n"

    parts = [_SYNOPSIS, ""]
    parts += [_mode_block(name) for name in MODES]
    parts += [_farm_block(), _common_block(), _exit_codes_block()]
    return "\n".join(parts).rstrip() + "\n"


TOPICS = tuple(MODES) + ("farm",)
