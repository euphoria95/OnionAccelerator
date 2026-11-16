# Multi-download mode (`--mode multi`)

- Downloads many files at once over a shared set of **lanes** — one lane is one isolated Tor circuit with its own SOCKS credential and its own User-Agent, pinned for the lane's life. Concurrency is the lane count (endpoints × `--circuits-per-endpoint`), not a fixed twenty.
- Cuts every file larger than `--piece-size` (4 MiB by default) into pieces that any free lane takes next, so a fast circuit does more of the work and a stalled one is abandoned (see [The download engine](../download-engine.md)).
- Aggregates download progress via a single progress bar in the terminal.

The mechanics, the journal, resume and the custody record are on the [download engine](../download-engine.md) page.
