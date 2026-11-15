"""A persistent index of what crawls and archive walks have found, scored and diffable.

    from index import Index
    with Index.open() as ix:
        ...

The pieces, each its own module and each usable alone:

    store    the SQLite file: private, WAL, migrated
    ingest   crawl directories, rvtree ndjson, custody records and live events -> rows
    rules    rule packs and the operator's scope.toml -> scored, explained hits
    diff     what changed between two sightings of a target
    query    search and top hits
    fetch    hand the best hits to the download engine and to rvtree's extract
    export   JSONL and CSV
    live     the same ingester, fed from a running job's --stream

Standard library only, like eventstream/: it has to run on the analysis host, which may
have installed nothing.
"""

from .store import SCHEMA_VERSION, Index, IndexStoreError, default_index_path

__all__ = ["SCHEMA_VERSION", "Index", "IndexStoreError", "default_index_path"]
