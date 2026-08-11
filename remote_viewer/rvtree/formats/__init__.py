"""Per-format metadata readers."""

from . import detect, rar, sevenzip, tarwalk, xz, zipfmt

__all__ = ["detect", "rar", "sevenzip", "tarwalk", "xz", "zipfmt"]
