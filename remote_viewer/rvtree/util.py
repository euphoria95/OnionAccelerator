"""Small formatting helpers shared across the package."""

from __future__ import annotations


def human_bytes(n: float) -> str:
    if abs(n) < 1024:
        return f"{int(n)} B"
    for unit in ("KiB", "MiB", "GiB", "TiB", "PiB"):
        n /= 1024
        if abs(n) < 1024 or unit == "PiB":
            return f"{n:.1f} {unit}"
    return f"{n:.1f} PiB"


def human_duration(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} seconds"
    if seconds < 5400:
        return f"{seconds / 60:.0f} minutes"
    if seconds < 172800:
        return f"{seconds / 3600:.1f} hours"
    return f"{seconds / 86400:.1f} days"
