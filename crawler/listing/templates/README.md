# Writing a crawl template

A template is a small TOML file that teaches the crawler one target's *logic*: how to
recognise it, where its rows are, and how a child directory is addressed. Everything
else — circuits, retries, deduplication, the report — is shared and already written.

This is the tutorial. The reference for each key is at the bottom.

---

## When you need one (and when you do not)

You usually do not. The crawler's default reading takes every `<a href>` on a page and
keeps what resolves strictly below the directory being listed. That handles Apache,
nginx, lighttpd, Caddy, IIS, `http.server` and the hand-rolled autoindex templates onion
operators actually run, without knowing anything about any of them.

Write a template when the target does something the *links* cannot express:

| The target… | Template gives you |
|---|---|
| keeps the path in a query parameter (`?p=dumps/raw`) | `[navigate] kind = "query"` |
| folds the path into one escaped segment (`/fm/TOK/dumps%2Fraw`) | `kind = "encoded"` |
| serves a JavaScript shell and a JSON API | `kind = "api"` + `[extract] strategy = "json"` |
| answers a listing only to `POST`/`PROPFIND` | `[navigate] method = …` |
| serves file bytes from a different URL than the row | `[download] url = …` |
| paginates a single directory | `[paginate]` |
| publishes a `tree`/`find` dump of everything | `strategy = "manifest"` |
| is a known server you want *recognised* | `[match]` alone — see "recognition-only" below |

The last row is worth spelling out. A directory holding a single file scores below the
index-confidence guard and is written off as a leaf; a page a template matched skips that
guard entirely. So a template with nothing but `[match]` rules and the default strategy
is a real fix for a real failure, and half the built-ins are exactly that.

---

## Start from the target, not from the file

```bash
# 1. What is it? One request, and it tells you what it found and what to pass.
python3 OnionAccelerator.py --mode crawl --detect --url http://target.onion/

# 2. What already exists?
python3 OnionAccelerator.py --list-profiles
```

`--detect` prints every profile that matched, how many entries each one *actually read*,
and a sample child URL. If something already works, you are finished. If the table shows
matches that read nothing, that is your answer: the software is recognised, the listing
is somewhere the reader is not looking.

Keep your own templates in a directory and pass `--templates`:

```bash
mkdir -p ~/profiles
python3 OnionAccelerator.py --mode crawl --detect --templates ~/profiles \
    --url http://target.onion/
```

A template in `--templates` **replaces** a built-in of the same name, which is how you fix
a shipped profile mid-engagement without touching the repository — and how a profile for
one client's target stays out of it.

---

## Worked example 1 — the path is in a query parameter

The page looks like a listing and reads as empty, because every row links back to the
page itself with a different query. Structurally that is a sort control, not an entry.

```html
<table id="main-table"><tbody>
  <tr><td><i class="fa fa-folder-o"></i><a href="?p=files/archive" class="link">archive</a></td>
      <td>&mdash;</td><td>2023-11-02 14:31</td></tr>
  <tr><td><i class="fa fa-file-o"></i><a href="?p=files&view=dump.sql.gz" class="link">dump.sql.gz</a></td>
      <td>512M</td><td>2023-11-02 14:29</td></tr>
</tbody></table>
```

```toml
name     = "my-file-manager"
title    = "Somebody's PHP file manager (path in ?p=)"
priority = 70

[match]
content_type = ["text/html"]
body_regex   = "tinyfilemanager"     # something only this software emits

[extract]
strategy = "rows"
row      = "table#main-table tbody tr"
link     = "td a.link"
dir_when = "i.fa-folder-o"           # what marks a directory *within a row*

[navigate]
kind  = "query"
param = "p"

[download]
url = "{root}?p={path}&dl={name}"    # bytes live somewhere the row does not point
```

Note what is *not* there: no size or date selectors. The shared row-text reader finds
`512M` and `2023-11-02 14:29` wherever they sit, and column positions move between
releases of the same software. Only pin a column when the shared reader gets it wrong.

`dir_when` matters more than it looks. In a manager like this both a directory and a file
are `?p=` links; the folder icon is the only thing separating them. **If you name
`dir_when`, its absence means "file"** — so pick a marker every directory row has.

---

## Worked example 2 — a JavaScript shell over a JSON API

Nothing in the HTML is a listing. Open the browser's network tab, do one click, and copy
the request.

```
POST /api/fs/list
Content-Type: application/json

{"path": "/dumps", "page": 1, "per_page": 0}
```
```json
{"code": 200, "data": {"content": [
  {"name": "raw", "is_dir": true,  "size": 0,        "modified": "2024-01-17T03:02:55Z"},
  {"name": "part.bin", "is_dir": false, "size": 277504, "modified": "2023-11-02T14:29:41Z"}
]}}
```

```toml
name     = "my-alist"
title    = "AList-like API"
priority = 75

[match]
body_regex = "window\\.ALIST"        # the shell, so --detect can spot it passively

[probe]                              # what --detect sends to confirm it
method = "POST"
path   = "/api/fs/list"
body   = '{"path": "/", "page": 1, "per_page": 0}'
expect_regex = "\"content\""

[probe.headers]
Content-Type = "application/json"

[extract]
strategy  = "json"
rows      = "data.content"           # dotted path to the array
dir_field = "is_dir"
date      = "modified"

[navigate]
kind   = "api"
method = "POST"
url    = "{origin}/api/fs/list"
body   = '{"path": "/{path}", "page": 1, "per_page": 0}'

[navigate.headers]
Content-Type = "application/json"

[download]
url = "{origin}/d/{path}"
```

Then crawl it. An API target **needs `--profile`**: the seed itself has to be requested
in the target's scheme, and until a template is chosen there is no scheme to request it
in.

```bash
python3 OnionAccelerator.py --mode crawl --templates ~/profiles \
    --profile my-alist --url http://target.onion/dumps/
```

`--url http://target.onion/dumps/` lists `dumps`, not the root: the seed's path is read
out of the URL you typed and substituted into `{path}`.

**Placeholders are substituted by name, never through `str.format`** — an API body is
JSON and JSON is made of braces. `{path}` is percent-encoded into a URL and JSON-escaped
into a body, so a directory called `we"ird` cannot break out of the request.

---

## Worked example 3 — the target publishes its own index

Some sites ship a `tree` or `find` dump of everything they hold. Reading it is one
request where crawling the same tree is thousands, and it is the only sound way to
measure what a crawl missed.

```bash
python3 OnionAccelerator.py --mode crawl \
    --profile tree-dump --url http://target.onion/List_of_leaked_files.txt
```

`tree-dump`, `ls-lr-dump`, `find-dump` and `sitemap-xml` are built in; `format = "auto"`
sniffs which of the four a file is. Paths resolve against the dump's own directory, so a
dump served beside the tree it describes needs no configuration. One served elsewhere
needs `strip_prefix`.

A manifest's directories are **recorded and never fetched** — the dump already listed
what is under them.

---

## Testing it

Loading is strict: an unknown key, a bad regex, a strategy that does not exist or an
option that strategy does not take is an error naming your file. That is deliberate — a
silently ignored typo is a rule that stopped applying, and the crawl it produces looks
exactly like a target with nothing in it.

```bash
# Does it load, and does it read the page?
python3 OnionAccelerator.py --mode crawl --detect --templates ~/profiles \
    --url http://target.onion/

# Does it walk? Fifty directories is enough to know.
python3 OnionAccelerator.py --mode crawl --templates ~/profiles \
    --profile my-file-manager --url http://target.onion/ --max-pages 50
```

Read the first few lines of `crawls/<job_id>/dirs.jsonl`. Each carries the `profile` that
read it, `n_dirs`, `n_files` and `confidence`. Directories with zero of both, page after
page, mean the rows are not where the template says.

To contribute a template back, save a page of the target as a fixture in
`tests/fixtures/listings/`, add it to `VERIFIED_FIXTURES` in `tests/test_profiles.py`,
and mark the template `status = "verified"`. A template with no fixture must stay
`unverified` — the word is shown to operators deciding whether to trust it, and
`tests/test_profiles.py` fails if one lies.

---

## Reference

### Top level

| Key | Meaning |
|---|---|
| `name` | **Required.** Unique; what `--profile` takes. |
| `title` | One line, shown by `--list-profiles` and `--detect`. |
| `priority` | Tie-break among equally-matching profiles. Built-ins: manifests 84–90, APIs and machine formats 70–82, managers 65–75, autoindexes 50–60, fallback 0. |
| `status` | `verified` (a fixture pins it) or `unverified` (written from docs). Default `unverified`. |
| `notes` | Anything an operator needs to know that the fields cannot say. |

### `[match]` — is this that target?

Every rule present must hold; the score is how many held, so a profile naming four
signals outranks one naming a single weak one. All regexes are case-insensitive and
multi-line. **No rules at all means "always matches"** — that is the fallback's trick,
and at priority 0 it loses every tie.

| Key | Tested against |
|---|---|
| `content_type` | String or list; prefix match on the response content type. |
| `status` | Integer or list of HTTP status codes. |
| `title_regex` | The page's `<h1>`, else its `<title>`. |
| `generator_regex` | The `<address>` footer an autoindex leaves behind. |
| `body_regex` | The first 512 KiB of the body. |
| `url_regex` | The page's URL. |
| `[match.header]` | A table of `Header = "regex"`. |

### `[probe]` — an active check for `--detect` only

Never sent during a crawl. `method`, `path` (joined onto the seed), `[probe.headers]`,
`body`, and `expect_regex` — a cheap check that the answer is the shape you expected
before it is parsed.

### `[extract]` — where are the rows?

`strategy` is required. Options are validated against that strategy's own set.

**`anchors`** — the default structural reader. `junk_text` (extra link text to ignore),
`min_confidence`.

**`rows`** — CSS selectors. `row`, `link`, `name`, `size`, `date`, `dir_when`,
`file_when`, `skip`, `href_attr`. Omit `size`/`date` unless the shared row reader gets
them wrong.

**`json`** — `rows` (dotted path to the array; empty means the document root), `name`,
`size`, `date`, `path`, `href`, `dir_field`, `dir_value`, `type_field`, `cursor_field`,
`more_field`. With no options at all it reads nginx's and Caddy's JSON autoindex.

**`xml`** — `rows`, `name`, `href`, `size`, `date`, `dir_when`, `dir_rows`, `dir_name`,
`path_is_name`, `cursor_field`, `more_field`. Namespace prefixes are matched loosely, so
`D:response`, `d:response` and `response` are the same element.

**`manifest`** — `format` (`auto`, `tree`, `find`, `ls`, `sitemap`), `strip_prefix`,
`max_entries`.

### `[navigate]` — how is a child addressed?

| `kind` | Child URL | Other keys |
|---|---|---|
| `href` (default) | the link, as published | — |
| `query` | the page's URL with `param` set to the child's path | `param`, `join` |
| `encoded` | `prefix` + the whole path as one escaped segment | `prefix`, `join` |
| `api` | whatever `url` renders to, sent with `method`/`body`/`headers` | `url` (required), `method`, `body`, `headers`, `prefix` |

Placeholders: `{origin}`, `{root}`, `{base}`, `{path}`, `{path_raw}`, `{name}`, `{href}`,
`{cursor}`.

### `[paginate]` — the rest of *this* directory

`kind` is `none` (default), `query` (`param`, `start`, `max_pages`) or `cursor`, which
follows whatever the strategy read into `cursor_field`. Extra pages are queued at the
page's own depth, not one below.

### `[download]` — where the bytes are

`url`, a template over the same placeholders. Set it only when a file's bytes are not at
the URL its row points to. What lands in `urls.txt` — and what `--download` fetches — is
this URL when it is set.
