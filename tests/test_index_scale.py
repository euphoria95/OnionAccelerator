"""The Phase 3 scale check: a million entries ingest, and a path search stays under a second.

Marked `slow` (a few minutes of local work, no network); CI runs it once, on 3.13.
"""

import os
import stat
import time

import pytest

from index import query, store
from index.ingest import Ingester
from index.rules import load_rules
from indexfixtures import file_record

N = 1_000_000


def synthetic(n):
    """Deterministic, varied, and entirely invented: depth, names and sizes all differ."""
    words = ["alpha", "bravo", "delta", "echo", "foxtrot", "gamma", "kilo", "lima", "oscar",
             "papa", "romeo", "sierra", "tango", "victor", "xray", "zulu"]
    exts = [".txt", ".bin", ".dat", ".log", ".csv", ".zip", ".pdf", ".cfg"]
    for i in range(n):
        d1, d2, d3 = words[i % 16], words[(i // 16) % 16], words[(i // 256) % 16]
        yield file_record("scale.test", f"/{d1}/{d2}/{d3}/{i // 4096}/file{i}{exts[i % 8]}",
                          (i * 37) % 100000)


@pytest.mark.slow
def test_a_million_entries_ingest_and_a_path_search_is_fast(tmp_path):
    with store.Index.open(str(tmp_path / "scale" / "index.db")) as ix:
        ing = Ingester(ix, load_rules())
        started = time.monotonic()
        for record in synthetic(N):
            ing.apply("scale-run", "crawl.file", record)
        # A handful of needles in the haystack, so the search has something to find.
        for i in range(5):
            ing.apply("scale-run", "crawl.file",
                      file_record("scale.test", f"/hr/needle_payroll_{i}.xlsx", 1))
        ing.apply("scale-run", "run.stop", {"mode": "crawl", "stopped_because": "completed"})
        ing.flush()
        ingest_seconds = time.monotonic() - started
        assert ingest_seconds < 1800, f"{N} entries took {ingest_seconds:.0f}s to ingest"

        count = ix.conn.execute("SELECT COUNT(*) FROM entries WHERE kind='file'").fetchone()[0]
        assert count == N + 5

        for pattern, expect in (("needle_payroll_3", 1), ("needle*", 5), ("payroll", 5)):
            t0 = time.monotonic()
            found = query.search(ix, pattern)
            took = time.monotonic() - t0
            assert len(found) == expect, pattern
            assert took < 1.0, f"search {pattern!r} took {took:.2f}s over {N} entries"

        t0 = time.monotonic()
        query.top_hits(ix, min_score=50, limit=20)
        assert time.monotonic() - t0 < 1.0
        # A file this size has been through hundreds of WAL checkpoints; it must still be
        # exactly as private as the empty one was.
        assert stat.S_IMODE(os.stat(ix.path).st_mode) == 0o600
        size_mib = os.path.getsize(ix.path) / 2**20
        print(f"\n{N} entries: ingest {ingest_seconds:.0f}s ({N / ingest_seconds:.0f}/s), "
              f"db {size_mib:.0f} MiB")
