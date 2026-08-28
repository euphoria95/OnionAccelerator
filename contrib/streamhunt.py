#!/usr/bin/env python3
"""Hunt a running OnionAccelerator job for the keywords in scope.

    python3 OnionAccelerator.py --mode crawl --url http://target.onion/ --stream &
    python3 contrib/streamhunt.py --keywords scope.txt --out hits.jsonl

Attaches to a run's event stream, matches every finding against a list of keywords, and
writes the hits somewhere they can be handed to somebody. It is meant to be read as much
as run: this is the shape of the "external script" the streaming API exists for, and
about a hundred lines of it are the parts anyone would write anyway -- reconnecting,
recording where it got to, and being honest about what it missed.

Two things it does that a naive `curl | grep` does not:

* **Reconnects from where it stopped.** A dropped socket resumes with ?since=<last seq>,
  so a laptop that slept through twenty minutes of a crawl still gets those twenty
  minutes -- as long as they are still in the run's replay buffer.
* **Records gaps.** If this process falls behind far enough to lose events, the run says
  so and the loss is written into the output as a hit of its own. An index with a hole
  in it that nobody knows about is worse than no index; the run's own JSONL on disk is
  complete, and the gap record says which range to reconcile.

Standard library only, so it runs on the analysis box without installing anything.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

RECONNECT_DELAY = 3.0


def load_keywords(path):
    """One pattern per line. '#' comments and blank lines ignored.

    A line is a regex if it is wrapped in slashes -- /^\\d{4}-payroll/ -- and a plain
    case-insensitive substring otherwise, because most of a scope list is names and
    nobody should have to escape a full stop in "Acme Ltd." to search for it.

    A '#' only starts a comment at the beginning of a line or after whitespace: a scope
    list has "ticket#4471" and "/invoice#\\d+/" in it, and silently searching for
    "ticket" instead is the kind of quiet wrongness that ends up in a report.
    """
    patterns = []
    with open(path, "r", encoding="utf-8") as handle:
        for number, raw in enumerate(handle, 1):
            line = raw.strip()
            if line.startswith("#"):
                continue
            line = re.split(r"\s+#", line, maxsplit=1)[0].strip()
            if not line:
                continue
            try:
                if len(line) > 2 and line.startswith("/") and line.endswith("/"):
                    patterns.append((line, re.compile(line[1:-1], re.IGNORECASE)))
                else:
                    patterns.append((line, re.compile(re.escape(line), re.IGNORECASE)))
            except re.error as exc:
                raise SystemExit(f"{path}:{number}: bad pattern {line!r}: {exc}")
    if not patterns:
        raise SystemExit(f"{path}: no keywords in it")
    return patterns


def events(base, selector, token, since):
    """Yield decoded events forever, reconnecting from the last sequence number seen.

    A cursor belongs to one run. Every job numbers its events from one, so a reconnect
    that lands on a *different* run -- a crawl restarted on the same port -- must start
    that run from the beginning; carrying seq 4812 across would skip its first 4812
    findings and report nothing missing, which is the one thing this script must never
    do. The run id arrives on the stream.hello line, before any finding.
    """
    run = None
    while True:
        query = dict(selector)
        if since is not None:
            query["since"] = str(since)
        url = f"{base.rstrip('/')}/events?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(url)
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request) as response:
                restart = False
                for raw in response:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    named = event.get("run") or (event.get("data") or {}).get("run")
                    if named and named != run:
                        if run is not None:
                            print(f"[!] a different run ({named}) is on this port; "
                                  f"starting it from its first event", file=sys.stderr)
                            restart = True
                        run = named
                        if restart:
                            since = 0
                            break
                    if event.get("seq"):
                        since = event["seq"]
                    yield event
                if restart:
                    continue
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace").strip()
            raise SystemExit(f"{url}\n  HTTP {exc.code}: {body}")
        except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
            # The run may simply have ended. Say which, and try again either way -- a
            # crawl restarted under a new job id will be a new run on the same port.
            print(f"[!] disconnected ({exc}); retrying in {RECONNECT_DELAY:.0f}s "
                  f"from seq {since}", file=sys.stderr)
            time.sleep(RECONNECT_DELAY)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Match a running OnionAccelerator job against a keyword list.")
    parser.add_argument("--url", default="http://127.0.0.1:8787",
                        help="Where the run is streaming (default %(default)s).")
    parser.add_argument("--keywords", required=True, metavar="FILE",
                        help="One keyword per line; /regex/ for a regex.")
    parser.add_argument("--out", metavar="FILE",
                        help="Append hits here as JSONL. Otherwise they go to stdout.")
    parser.add_argument("--kinds", default="crawl.file,crawl.dir,crawl.page,tree.entry",
                        help="Event kinds to search (default %(default)s). Use 'crawl' "
                             "or 'tree' for a whole family, or '' for everything.")
    parser.add_argument("--since", type=int, default=0, metavar="SEQ",
                        help="Start from this sequence number. 0, the default, takes "
                             "everything the run still holds; omit it with --live.")
    parser.add_argument("--live", action="store_true",
                        help="Start from now instead of replaying the buffer.")
    parser.add_argument("--token", default=None,
                        help="Bearer token, if the run was started with --stream-token.")
    parser.add_argument("--quiet", action="store_true",
                        help="Only write hits; do not print the running tally.")
    args = parser.parse_args(argv)

    patterns = load_keywords(args.keywords)
    selector = {"kinds": args.kinds} if args.kinds else {}
    sink = open(args.out, "a", encoding="utf-8") if args.out else sys.stdout

    tally = {name: 0 for name, _ in patterns}
    seen = gaps = 0
    started = time.time()
    if not args.quiet:
        print(f"[*] {len(patterns)} keyword(s) against {args.url}", file=sys.stderr)

    try:
        for event in events(args.url, selector, args.token,
                            None if args.live else args.since):
            kind = event.get("kind", "")
            if kind == "stream.gap":
                gaps += event["data"]["lost"]
                _write(sink, {"hit": "GAP", "lost": event["data"]["lost"],
                              "resuming_at": event["data"].get("resuming_at"),
                              "note": "events lost here; reconcile against the run's JSONL",
                              "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
                print(f"[!] lost {event['data']['lost']} event(s) -- this process fell "
                      f"behind; the run's listing.jsonl is complete", file=sys.stderr)
                continue
            if kind.startswith("stream.") or kind.startswith("run."):
                if kind == "run.stop" and not args.quiet:
                    print(f"[*] run ended: {event['data'].get('stopped_because')}",
                          file=sys.stderr)
                continue

            seen += 1
            # Matched against the whole re-serialised event, not just its locator: a
            # keyword can as easily be in a listing's title, a parent path or -- with
            # --stream-bodies -- the page text itself.
            line = json.dumps(event, ensure_ascii=False, default=str)
            for name, pattern in patterns:
                match = pattern.search(line)
                if not match:
                    continue
                tally[name] += 1
                data = event.get("data", {})
                _write(sink, {
                    "keyword": name,
                    "matched": match.group(0),
                    "seq": event.get("seq"),
                    "kind": kind,
                    "run": event.get("run"),
                    "locator": data.get("url") or data.get("path"),
                    "size": data.get("size_bytes", data.get("size")),
                    "ts": event.get("ts"),
                    "event": data,
                })
                if not args.quiet:
                    print(f"[+] {name}: {data.get('url') or data.get('path')}",
                          file=sys.stderr)
            if not args.quiet and seen % 500 == 0:
                _tally(tally, seen, gaps, started)
    except KeyboardInterrupt:
        pass
    finally:
        if not args.quiet:
            _tally(tally, seen, gaps, started)
        if sink is not sys.stdout:
            sink.close()
    return 0


def _write(sink, record):
    sink.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    sink.flush()


def _tally(tally, seen, gaps, started):
    hits = sum(tally.values())
    print(f"[*] {seen} event(s) searched, {hits} hit(s), {gaps} lost, "
          f"{time.time() - started:.0f}s", file=sys.stderr)
    for name, count in sorted(tally.items(), key=lambda kv: -kv[1]):
        if count:
            print(f"      {count:>6}  {name}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
