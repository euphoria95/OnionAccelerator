"""Output renderers.

``ndjson`` is the streaming one: it emits each entry as it is discovered, so a tree
with millions of members stays usable and a run interrupted halfway still leaves
something behind. ``tree`` and ``json`` necessarily buffer.
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
from typing import Iterable, Iterator, Optional, TextIO

from .model import DIR, Entry
from .util import human_bytes

BRANCH = "├── "
LAST = "└── "
VERT = "│   "
BLANK = "    "


class _Node:
    __slots__ = ("name", "children", "entry")

    def __init__(self, name: str):
        self.name = name
        self.children: dict[str, _Node] = {}
        self.entry: Optional[Entry] = None

    def add(self, parts: list[str], entry: Entry) -> None:
        node = self
        for part in parts:
            node = node.children.setdefault(part, _Node(part))
        node.entry = entry


def build_tree(entries: Iterable[Entry]) -> _Node:
    root = _Node("")
    for entry in entries:
        parts = [p for p in entry.path.replace("\\", "/").split("/") if p not in ("", ".")]
        if parts:
            root.add(parts, entry)
    return root


def render_tree(entries: Iterable[Entry], out: TextIO = sys.stdout, show_size: bool = True) -> None:
    root = build_tree(entries)
    out.write(".\n")
    _render_children(root, "", out, show_size)


def _render_children(node: _Node, prefix: str, out: TextIO, show_size: bool) -> None:
    # Directories first, then files, each alphabetically.
    items = sorted(node.children.values(), key=lambda n: (not _is_dir(n), n.name.lower()))
    for i, child in enumerate(items):
        last = i == len(items) - 1
        out.write(f"{prefix}{LAST if last else BRANCH}{_label(child, show_size)}\n")
        if child.children:
            _render_children(child, prefix + (BLANK if last else VERT), out, show_size)


def _is_dir(node: _Node) -> bool:
    if node.children:
        return True
    return node.entry is not None and node.entry.kind == DIR


def _label(node: _Node, show_size: bool) -> str:
    entry = node.entry
    name = node.name
    if _is_dir(node):
        return name + "/"
    if entry is None:
        return name
    if entry.kind == "symlink" and entry.linkname:
        return f"{name} -> {entry.linkname}"
    if show_size:
        return f"{name}  ({human_bytes(entry.size)})"
    return name


def render_long(entries: Iterable[Entry], out: TextIO = sys.stdout) -> None:
    for entry in entries:
        when = "-" * 16
        if entry.mtime:
            when = _dt.datetime.utcfromtimestamp(entry.mtime).strftime("%Y-%m-%d %H:%M")
        suffix = f" -> {entry.linkname}" if entry.linkname else ""
        out.write(f"{entry.mode_string()} {entry.size:>12,} {when}  {entry.path}{suffix}\n")


def render_json(entries: Iterable[Entry], out: TextIO = sys.stdout, meta: Optional[dict] = None) -> None:
    payload = {"entries": [_as_dict(e) for e in entries]}
    if meta:
        payload["meta"] = meta
    json.dump(payload, out, indent=2)
    out.write("\n")


def render_ndjson(entries: Iterator[Entry], out: TextIO = sys.stdout) -> int:
    count = 0
    for entry in entries:
        out.write(json.dumps(_as_dict(entry), separators=(",", ":")) + "\n")
        out.flush()
        count += 1
    return count


def _as_dict(entry: Entry) -> dict:
    d = {
        "path": entry.path,
        "size": entry.size,
        "kind": entry.kind,
    }
    if entry.csize is not None:
        d["csize"] = entry.csize
    if entry.mtime is not None:
        d["mtime"] = entry.mtime
    if entry.mode is not None:
        d["mode"] = entry.mode
    if entry.linkname:
        d["linkname"] = entry.linkname
    return d
