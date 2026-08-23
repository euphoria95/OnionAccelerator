"""The one exception every part of the transport can raise.

Its own module so that ``gate`` can raise it without importing ``tor``, which imports
``gate``. ``tor`` re-exports it, and ``TransportError`` remains the single thing the CLI
catches to turn a network-level failure into a one-line message instead of a traceback.
"""

from __future__ import annotations


class TransportError(RuntimeError):
    pass
