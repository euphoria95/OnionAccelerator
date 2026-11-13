"""Settings shared by every mode: worker caps, timeouts, file names, directories.

Moved out of OnionAccelerator.py unchanged. OnionAccelerator re-exports every name here,
and a test that patches one of them there reaches this module too (see the forwarding
table at the bottom of OnionAccelerator.py), so the old spelling keeps working.
"""

# ================== GLOBAL CONFIG ==================
MAX_WORKERS = 20              # e.g. ports 5000..5019
BASE_PORT = 5000
CHUNK_SIZE = 1024 * 64        # 64 KB
MIN_PARTIAL_CHUNK_SIZE = 1024 * 1024  # 1 MB: don't split a file finer than this
REQUEST_TIMEOUT = 20          # seconds, connect

# Connect and read budgets for anything that moves a file body, kept apart because one
# number cannot serve both. Passing a scalar to requests sets the two to the same value,
# which makes the connect budget double as the ceiling on how long the server may think
# before sending its first byte -- and these file managers think for as long as the file
# is big. A 39 MB archive measured 27.2s to response headers against a 20s scalar: the
# transfer was aborted seven seconds before the first byte, then streamed 37 MB with a
# worst inter-chunk gap of 3.1s once the wait was allowed to finish. That is why every
# file above ~5 MB "timed out" while every file below it downloaded cleanly. The read
# half is generous because it is per socket read, not for the whole transfer, so it
# bounds a stalled circuit without capping a large download. Tree mode has always used
# this pair; multi and partial mode are the ones that were passing a scalar.
TRANSFER_TIMEOUT = (REQUEST_TIMEOUT, REQUEST_TIMEOUT * 6)
USERAGENTS_FILE = "UserAgents.tsv"
URLS_FILE = "URLs.txt"

DOWNLOAD_DIR = "downloads"
PARTIAL_DOWNLOAD_DIR = "partials"
LOGS_DIR = "logs"
DEFAULT_RETRIES = 3

# Dla speedtest:
MAX_SPEEDTEST_BYTES = 5 * 1024 * 1024  # 5 MB
SPEEDTEST_CHUNK_SIZE = 1024 * 64

# External proxy list (--external): fetched fresh every run, never persisted.
EXTERNAL_PROXIES_URL = "https://raw.githubusercontent.com/euphoria95/external-tor-proxies/refs/heads/main/proxies.txt"
MAX_EXTERNAL_PROXIES = 100     # hard cap on threads/proxies for external mode
CONNECTIVITY_TIMEOUT = 15      # seconds, per-proxy liveness check

# Proxy connectivity check (--external): the URL hit to confirm a candidate proxy
# is alive. Point this at an endpoint you control or trust so that real target
# URLs are never revealed to proxies that end up discarded. Leave empty to fall
# back to a random URL from URLS_FILE (convenient, but leaks a target to every
# candidate proxy). Override per-run with --test-url.
PROXY_TEST_URL = ""            # e.g. "http://your-own-service.onion/ping"
