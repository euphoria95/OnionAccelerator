"""rvtree — list and extract from very large remote archives over Tor.

Reads the file tree of a remote ``.tar.xz``, ``.zip``, ``.7z`` or ``.tar`` using a
handful of HTTP Range requests instead of downloading the archive.
"""

import logging

__version__ = "0.1.0"

# Diagnostics travel by ``logging``; the CLI attaches a handler, and importing rvtree as
# a library stays silent until the caller asks for something.
logging.getLogger(__name__).addHandler(logging.NullHandler())
