# Partial-download mode (`--mode partial`)

- The same engine, one file at a time with every lane working on it, into `partials/<host>/`. There is no merge pass: each piece is written at its offset into one preallocated file, so memory is lanes × piece size however large the file is.
- Establishes the file size through a ladder of four probes, so a server that withholds `Content-Length` no longer costs you the parallelism: a `HEAD`, then the `size=` parameter of that same response's `Content-Disposition`, then the total in the `Content-Range` of a one-byte `Range: bytes=0-0` GET, and finally a plain streamed GET aborted the moment its headers arrive. No probe transfers a payload.
- If none of the four can establish a size, or the file fits in one piece, it is fetched as a single GET with nothing to resume.
- A piece that runs out of `--retries` stops its file and leaves it resumable: nothing is deleted, and rerunning the same command carries on.

The mechanics, the journal, resume and the custody record are on the [download engine](../download-engine.md) page.
