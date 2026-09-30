# android-db-mcp

An MCP server that gives an assistant access to an Android app's SQLite databases,
including osapiens business objects stored as serialized BLOBs. It supports schema
inspection, read-only SQL, explicit writes, decoded objects, keyword search, and
local semantic search.

## How it works

```text
MCP client → Python server → ADB → dbq Java runner → Android SQLite
                              ↘ host snapshot (read fallback)
                  decoded records → local FTS5 / embedding index
```

The preferred backend executes SQL on the device using Android's SQLite through
`app_process`. Private databases use the app's uid through `run-as`; external
app-specific databases use the ADB shell identity; root mode is also supported.
The jar is separate from the app and does not require an app code change.

Persistent runner processes are isolated by ADB executable, device serial, package,
and identity. Each process handles one request/response at a time. Failed runners
are closed and reaped; server shutdown and runner deployment close cached runners.
Set `ANDROID_DB_PERSIST=0` to launch a fresh process per call.

When the jar is unavailable, reads fall back to a host snapshot. Search indexes use
the same selected read backend, so a deployed jar lets indexing fetch records
without copying an entire database.

## Install

Requires Python 3.10+, ADB, and a connected emulator or device. Building the runner
also requires a JDK and an Android SDK with a platform and build-tools installation.

From this source checkout:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
# Optional local semantic search:
python -m pip install -e '.[semantic]'
```

The installed `android-db-mcp` command starts the stdio MCP server. Direct execution
with `python android_db_mcp.py` continues to work. Python wheels contain the server
and codec; build the Android jar from a source checkout or source distribution and
set `ANDROID_DB_JAR` when using an installed wheel.

### Build and deploy the runner

```bash
cd runner
./build.sh
./deploy.sh com.your.app
```

`build.sh` picks the newest SDK platform and build-tools under `ANDROID_HOME`
(or `ANDROID_SDK_ROOT`, then `~/Library/Android/sdk`). The default minimum API is 26;
set `MIN_API` to override it. The output is `runner/build/dbq.jar`.

`deploy.sh` pushes the jar to `/data/local/tmp/dbq.jar`, streams it into the app's
`code_cache/` for `run-as`, and runs a smoke query. Reinstalling or clearing app data
removes the private jar; deploy it again or call `deploy_runner`.

For an emulator that supports `adb root`:

```bash
ANDROID_DB_RUNNER=root ./deploy.sh com.your.app
```

Private data normally requires a debuggable app for `run-as`. Root mode requires a
root-capable device/emulator. External-storage access depends on the device's
permissions. This runner uses ordinary Android SQLite, not a SQLCipher integration.

## Connect an MCP client

Use the absolute paths to your Python environment, server, and built jar:

```json
{
  "mcpServers": {
    "android-db": {
      "command": "/path/to/android-db-mcp/.venv/bin/python",
      "args": ["/path/to/android-db-mcp/android_db_mcp.py"],
      "env": {
        "ANDROID_PACKAGE": "com.your.app",
        "ANDROID_SERIAL": "emulator-5554",
        "ANDROID_DB_JAR": "/path/to/android-db-mcp/runner/build/dbq.jar"
      }
    }
  }
}
```

The default package comes from `ANDROID_PACKAGE`, a literal `applicationId` in the
project's Gradle file, or the foreground app. Use an explicit package and serial
for predictable targeting. Gradle variants and computed application IDs may require
an explicit override.

## Tools

Start with `app_context` — one call that describes the device, package, tenant databases, the
pools that hold data and the indexes already built.

Twenty tools, deliberately. Each one costs context in every session, so anything that was a
shorthand for `query`, a duplicate of `app_context`, or a second way to do one thing was removed.

| Tool | Purpose |
|---|---|
| `app_context` | **Start here.** Device, package, backend, databases by tenant, populated pools, existing indexes. |
| `devices` | List ADB devices. |
| `list_databases` | Discover SQLite files in private and external app storage, including tenant directories. |
| `deploy_runner` | Install the built jar and run a smoke query. |
| `schema` | Inspect tables, columns, foreign keys, indexes, counts, and optional column statistics. |
| `query` | Execute read-only SQL with a result limit. |
| `execute` | Execute a write against the live device database — `dry_run=True` rehearses it and rolls back. |
| `related` | The neighbourhood of one object: what it points at, what points at it, two hops out. |
| `set_device` | Point the session at another emulator or phone, resetting runners and caches. |
| `snapshot` | Copy a database to the host and report its consistency guarantee. |
| `pools` | Map osapiens pool names to tables, row counts, and promoted-field definitions. |
| `entries` | Read decoded business objects by pool name, keys, or SQL filter; project `fields` and apply `filters` over nested values. |
| `decode` | Decode a serialized BLOB column directly. |
| `refs` | Verified reference map: which fields point at which pool, with hit rates. |
| `explain_object` | One object, decoded, labelled, with its references resolved to titles. |
| `fields` | Field paths inside a pool's decoded objects, with fill rates and examples. |
| `query_decoded` | Real SQL (`json_each` / `json_extract`) over the decoded objects. |
| `mark` | Record current object state before an app action. |
| `marks` | List recorded marks. |
| `changes_since` | What was added, removed or changed since a mark, with field-level before/after. |
| `search` | Search decoded objects or table columns — `mode=keyword|semantic|hybrid`, with filters and a per-hit explanation of why each result matched. |

Tools also work without an MCP client:

```bash
export ANDROID_PACKAGE=com.your.app
python android_db_mcp.py call app_context
python android_db_mcp.py call list_databases
python android_db_mcp.py call schema db=app.db stats=false
python android_db_mcp.py call query db=app.db sql='SELECT count(*) FROM orders'
```

`key=value` arguments are parsed as JSON when possible. Quote arrays and SQL for
your shell, for example `keys='["object-123"]'` and `columns='["note"]'`.

## Business objects and pagination

osapiens entry tables are named `BusinessObjectEntry#<applicationId>#<poolId>`.
The `#value` column contains the serialized object. Promoted scalar columns may
be empty even when the corresponding nested data exists; `entries` and `decode`
read the actual BLOB content.

```bash
python android_db_mcp.py call pools
python android_db_mcp.py call entries pool=CodesGroup locale=de limit=10
python android_db_mcp.py call entries pool=WorkOrder keys='["object-123"]'
```

`entries` and `decode` return `rowid`, `truncated`, and `next_after_rowid`. When
`truncated` is true, repeat the same call with `after_rowid=<next_after_rowid>`.
Keep the database, pool, filters, and locale the same. Results are ordered by rowid
and limits must be positive. These tools require rowid tables; pages are separate
live reads, so concurrent edits or rowid reuse can change the dataset during traversal.

Translations are resolved when selecting display titles and generating searchable
text; the decoded `value` retains the original object. The codec supports common
SerializationUtil types, but not every possible type (for example Thrift and multipart
payloads). Unsupported values are reported as undecoded rather than guessed.

## Keyword and semantic search

```bash
python android_db_mcp.py call search pool=CodesGroup chunk=Codes q=roof
python android_db_mcp.py call search pool=CodesGroup chunk=Codes q='roof missing' mode=semantic
python android_db_mcp.py call search db=app.db table=orders columns='["note"]' q=refund
```

`chunk=Codes` indexes each element of the decoded `Codes` collection independently,
retaining its parent's title. Without a chunk path, each object becomes one document.
Plain-table search requires `db`, `table`, and `columns`; it uses SQLite rowids.
Keyword queries use FTS5 MATCH syntax. Semantic search computes embeddings locally
with fastembed/ONNX; its first use may download the selected model. The default model
is `BAAI/bge-small-en-v1.5`; choose `EMBED_MODEL` explicitly for other languages/models.

Refresh modes:

- `auto`: for pools, compare each record's `#lastChange` against stored source state,
  fetch new or changed records, and remove deleted records and obsolete chunks.
  Null change markers are re-read on every check. For plain tables, rebuild after
  `INDEX_TTL` seconds.
- `full`: rebuild documents and, for semantic tools, embeddings.
- `never`: retain stored documents (build if absent). Pool replies report changed
  and deleted source counts. Semantic tools still repair missing vectors or replace
  vectors from an incompatible model. An incompatible index format requires a rebuild.

Keyword refreshes invalidate embeddings for changed documents. The next semantic
call embeds only missing vectors. Model identity and vector dimensions are stored;
changing `EMBED_MODEL` regenerates vectors without rebuilding the source documents.
Old index formats rebuild automatically with `auto` or `full`.

Replies include document count, content age, last check time, missing vector count,
and model information. `stale_rows=null` means no exact post-refresh comparison was
performed; it is not a claim of zero changes. Source reads are not one transaction
with app writes. Changes that retain the same non-null `#lastChange` on an existing
record require `full`. Semantic ranking currently scans all vectors in memory.

### Which search mode

`search` runs one index in up to two ways and fuses the rankings:

| mode | what runs |
|---|---|
| `auto` | keyword, plus vectors when `EMBEDDINGS=on` — the default |
| `keyword` | bm25 only |
| `semantic` | embeddings only |
| `hybrid` | both, fused with weighted reciprocal rank |

**Embeddings are off by default, and that is a measured decision.** On this data, scored with
`eval.py` over 136 known-item queries:

| retriever | descriptive | exact | partial | phrase | ALL (MRR) |
|---|---|---|---|---|---|
| keyword | 0.57 | **0.87** | **0.82** | 0.59 | **0.71** |
| vector | 0.35 | 0.84 | 0.79 | 0.46 | 0.61 |
| hybrid | 0.55 | 0.86 | **0.82** | 0.58 | 0.70 |

Vectors cost a model download and seconds per index build and, on short coded titles like
`Failure Group PFSV0J Code 2`, buy nothing. Turn them on with `EMBEDDINGS=on` when the content
becomes prose, or when a reranker beats these numbers — `eval.py` will say.

One detail that matters more than the model choice: FTS5 joins bare words with `AND`, so
"cause of failure" used to match nothing unless one document contained all three words. When the
strict reading finds nothing, the terms are retried joined with `OR`. On the phrase family that
took keyword search from **MRR 0.04 to 0.59** — a bigger gain than any embedding model produced,
and the reason vectors are no longer needed to cover that case.


## Following references

Business objects point at each other by id. `refs` derives that graph from the data rather than
from configuration: it decodes a sample of objects, guesses a target pool from each id-shaped
field name, and keeps the guess only when a sample of those ids actually resolves to `#key`
values in that pool.

```bash
python android_db_mcp.py call refs pool=WorkOrder
# path=PriorityId -> pool=Priority, hit_rate=1.0, probed=20
```

`explain_object` uses the map to describe a single object the way a person would read it:

```bash
python android_db_mcp.py call explain_object key=0ceede98-e6e3-4619-8d63-6788a9f92185
```

It returns the title, the resolved references (`Priority: High` rather than a uuid), the non-empty
fields and the promoted columns. Omit `pool` and the key is located across every pool first.
`depth=2` inlines each referenced object; `incoming=True` also looks for objects pointing back at
this one, and says which pools it could and could not search.

The same resolution runs while indexing, so a document says `Asset: WTG 07` instead of listing
identifiers — which is what makes search results readable and embeddings meaningful.

## What did that action change?

```bash
python android_db_mcp.py call mark label="before save"
#   … tap Save in the app, or trigger a sync …
python android_db_mcp.py call changes_since
```

`mark` records identity (`#key`, `#thingId`, `#lastChange`) for every populated pool — one batched
query — and decoded values for pools under `MARK_DECODE_MAX` rows. `changes_since` re-reads and
reports added, removed and changed objects; where values were recorded it includes a field-level
before/after. Pools that were only covered at identity level are named explicitly, so a diff never
implies more than it checked.

`changes_since(since=<#lastChange>)` needs no mark: it lists objects touched after a watermark,
without before/after.

## SQL over decoded objects

Each index keeps every object's decoded `#value` as JSON, so SQLite's JSON functions work over
nested fields that exist nowhere in the table's columns:

```bash
python android_db_mcp.py call query_decoded pool=CodesGroup \
  sql="SELECT d.title AS grp, COUNT(*) AS codes
       FROM decoded d, json_each(d.json, '\$.Codes') c
       GROUP BY 1 ORDER BY 2 DESC LIMIT 10"
```

Tables available: `decoded(src_key, key, thing, last_change, title, json)`,
`paths(path, kind, n, n_filled, example)`, `docs(doc_id, src_key, text, meta)` and
`sources(src_key, last_change)`. Queries are read-only and run against the index snapshot, whose
age and staleness come back with the result.

`fields(pool=...)` lists the paths that exist and how often they are filled — worth a look before
writing a query, because a path present in 2 of 80 objects will quietly return almost nothing.

For filtering without SQL, `entries` takes the same paths:

```bash
python android_db_mcp.py call entries pool=WorkOrder \
  fields='["Title","Codes[*].Title"]' \
  filters='{"Status":"OPEN","Title":{"op":"contains","value":"pump"}}' scan=500
```

Nested values live inside the BLOB, so these filters are applied after decoding: the reply states
how many rows were scanned, how many matched, and whether the scan was complete. Narrow with
`where` (SQL over promoted columns, executed on the device) first when a promoted column can do it.

## Evaluating retrieval

`eval.py` scores keyword, vector and hybrid search against known-item queries generated from the
indexed documents themselves, so changing a model or a fusion weight is measured, not guessed.

```bash
python eval.py build --pool CodesGroup --chunk Codes --n 40
python eval.py run   --pool CodesGroup --chunk Codes --sweep
python eval.py run   --pool CodesGroup --chunk Codes --models BAAI/bge-small-en-v1.5,<other>
```

Three query families are generated per document: the exact title, a two-or-three word fragment,
and text from a different field (the case embeddings are supposed to win). Hand-written cases in
`evals/manual.json` are merged in. Reported: recall@1, recall@5, MRR@10 and median latency per
retriever.

## Benchmarks

```bash
python bench.py run --label after --reps 7
python bench.py ab --before-src /path/to/baseline/checkout --rounds 3
python profile_tools.py --reps 3            # per-function, device wait vs host CPU
```

`profile_tools.py` is the one to reach for when something feels slow: it runs each tool under
cProfile and separates time spent waiting for the device from host CPU, then ranks the host
functions. Optimising anything else is guesswork — about 75% of wall time here is the device.

`ab` alternates two checkouts round by round and keeps the best time per scenario. An emulator
drifts enough that running all of A and then all of B invents regressions that disappear when the
runs are interleaved.

## Dashboard

```bash
python3 explorer.py                      # http://127.0.0.1:8765
python3 explorer.py --port 9000 --locale de
python3 explorer.py --dump snap.json     # capture the tenant
python3 explorer.py --fixtures snap.json --no-device   # browse that capture with no emulator
```

A local database dashboard for data that has no usable schema of its own. There are no foreign
keys in these files — an `AssetId` lives inside a serialized BLOB — so every relationship shown
here was derived by decoding the objects and verified against real ids. Point any ordinary SQLite
tool at the same file and it shows tables with no relationships at all.

| View | What it gives you |
|---|---|
| **Overview** | objects, references and resolve rate; every database with size and table count; largest pools; **unresolved references** (ids pointing at nothing — usually a sync gap); recently changed objects; retrieval indexes built by the MCP tools |
| **Pools** | a real data grid: sortable, paged, resizable columns, decoded nested paths as columns (`Codes[*].Title`), a column picker showing each path's fill rate, structured filters over nested values, text filter, CSV/JSON export, row peek without leaving the grid |
| **Object** | decoded fields, references resolved to titles, what references it, everything two hops away grouped by pool, raw decoded JSON |
| **Relationships** | the ERD: pools sized by object count, arrows for verified references, dashed amber where some ids don't resolve, focus mode for one pool's neighbourhood, SVG export |
| **Databases / tables** | table list per database, raw table browsing, and a structure view — columns with types, fill rates, distinct counts, primary keys, indexes, foreign keys, DDL, and a count of columns that are always empty |
| **SQL console** | read-only SQL on the device, or `json_each` / `json_extract` over the decoded objects; query history, saved queries, EXPLAIN QUERY PLAN, copy as JSON/CSV |
| **Changes** | mark the current state, act in the app, refresh — added / removed / changed with a field-level before/after, and optional auto-refresh |

Keyboard: `⌘K` / `Ctrl+K` command palette, `/` search, `?` shortcuts, `⌘↵` runs a query.

**Secrets are masked.** Columns and paths that look like credentials (`password`, `token`,
`secret`, `api_key`, `session`, …) render as dots — your `MobileUser` table holds a real session
token — and clicking one reveals just that value. The eye button in the header unmasks everything.

**Performance.** The whole tenant is decoded into memory once at startup (a few seconds), so
browsing, search, the graph and the object views cost no device round trips — in-memory endpoints
answer in 2–3 ms. Only raw-table paging, schema and the SQL console go back to the device, at
40–70 ms. **Reload** re-reads. Pools beyond `--max-objects` (default 20000) are skipped and named
at startup.

**Offline mode.** `--dump` writes a snapshot including every decoded object and each database's
schema; `--fixtures snap.json --no-device` then serves the whole dashboard from it, including SQL
over an in-memory copy of the decoded mirror. Useful for working on a plane, and it is how the UI
is developed and tested.

The server binds to `127.0.0.1` only and never writes to the device.

## Writes, rehearsed

`execute` is the only tool that can damage the app's data, so it can rehearse first:

```bash
python android_db_mcp.py call execute db=app.db sql="UPDATE MobileUser SET remember=99" dry_run=true
# {"changes": 1, "dry_run": true, "targets": ["MobileUser"],
#  "note": "Rolled back: nothing was written. `changes` is what the statement would have affected."}
```

The statement runs inside a transaction on the device that is never marked successful, so SQLite
rolls it back — you learn how many rows a `WHERE` clause really matches without touching them.
Requires the jar runner; the `sqlite3` fallback cannot roll back and refuses rather than committing.

Two switches harden this further: `ANDROID_DB_WRITE=off` refuses writes outright, `=dry-run` turns
every write into a rehearsal, and `ANDROID_DB_WRITE_ALLOW=Table1,Table2` limits which tables may be
written (a statement whose target cannot be read is refused rather than guessed at).

## Credentials

These databases hold real secrets — `MobileUser` keeps a live session token in the clear — and
everything these tools return is pasted into a model's context. Fields and columns whose *name*
says credential (`password`, `token`, `apiKey`, `client_secret`, `sessionId`, …) come back as
`«redacted»`, and the reply lists what was hidden, so a redacted answer is never mistaken for
missing data. Documents are redacted before they are indexed, so nothing leaks into FTS or an
embedding either.

Names are matched as whole words, not substrings: `Passes`, `Bypass`, `CompassBearing` and
`PassengerCount` are left alone. `REDACT=off` disables it; `REDACT_FIELDS=<regex>` replaces the
rules with your own.


## Writes and snapshot guarantees

`query` uses a read-only Android connection, or `PRAGMA query_only` on a host copy.
`execute` explicitly opens the live database for writes. If runner startup fails
before SQL dispatch, a one-shot fallback is allowed. If a persistent write loses its
response, the server reports **write outcome unknown** and does not retry it. Read
back the affected data before issuing another write. A successful write does not
force the app to refresh any in-memory caches.

`snapshot` returns `consistent=true` only when it uses on-device SQLite `.backup`.
If that path is unavailable, it copies the database, WAL, and SHM separately and
returns `consistent=false` with a warning: concurrent app writes can make this copy
inconsistent. Snapshot-backed `query` replies include the snapshot metadata.

```bash
python android_db_mcp.py call snapshot db=app.db require_consistent=true
```

This refuses raw copying. The current consistent-backup path requires on-device
`sqlite3` and a private database. Snapshots and search indexes contain app data and
are stored under `ANDROID_DB_CACHE`.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `ANDROID_PACKAGE` | Detected | Default application package. |
| `ANDROID_SERIAL` | ADB selection | Device serial; set when multiple devices are connected. |
| `ANDROID_PROJECT_DIR` / `CLAUDE_PROJECT_DIR` | Current directory | Project roots for Gradle package detection. |
| `ANDROID_DB_BACKEND` | `auto` | `auto`, `jar`, or `snapshot`. |
| `ANDROID_DB_RUNNER` | `run-as` | `run-as` or `root`. |
| `ANDROID_DB_JAR` | `runner/build/dbq.jar` beside server | Built runner jar. |
| `ANDROID_DB_PERSIST` | `1` | Set `0` for one-shot runners. |
| `ANDROID_DB_CACHE` | `~/.cache/android-db-mcp` | Snapshots, discovery caches, and indexes. Entries are stored per device serial and package, so two emulators running the same app never share them. |
| `ANDROID_DB_DEBUG` | Unset | Log ADB timing/fallback diagnostics to stderr. |
| `ADB` | `adb` | ADB executable. |
| `DB_LIST_TTL` | `600` | Database/pool discovery cache lifetime in seconds. |
| `INDEX_TTL` | `120` | Plain-table index lifetime in seconds. |
| `LOCALE` | `en` | Display/search translation language. |
| `EMBEDDINGS` | `off` | `on` lets `search(mode="auto")` use vectors. Off by default: measured on this data they scored no better than keyword search. |
| `EMBED_MODEL` | `BAAI/bge-small-en-v1.5` | fastembed model name. An unsupported name reports the alternatives this build has. |
| `INDEX_REFS` | `1` | Resolve references to titles when building documents; `0` disables. |
| `REF_SAMPLE` / `REF_PROBE` / `REF_MIN_HITS` | `40` / `20` / `0.5` | Objects sampled, ids probed, and the hit rate a reference guess must reach. |
| `REF_TTL` | `86400` | Reference-map cache lifetime in seconds. |
| `CONTEXT_TTL` | `60` | `app_context` cache lifetime in seconds. |
| `MARK_DECODE_MAX` / `MARK_DECODE_TOTAL` | `2000` / `20000` | Per-pool and overall object budget for storing decoded values in a mark. |
| `PATHS_MAX` | `20000` | Above this many objects the field/fill-rate table is skipped. |
| `HYBRID_W_BM25` / `HYBRID_W_VECTOR` | `1.0` / `0.5` | Fusion weights; measure changes with `eval.py`. |
| `ANDROID_DB_WRITE` | `on` | `off` refuses every write; `dry-run` turns every write into a rehearsal. |
| `ANDROID_DB_WRITE_ALLOW` | Unset | Comma-separated tables that `execute` may write; everything else is refused. |
| `REDACT` | `on` | `off` disables credential redaction in tool replies and indexes. |
| `REDACT_FIELDS` | Unset | A regex that replaces the built-in word list, when you want different rules. |
| `SNAPSHOT_TTL` | `5` | Seconds a pulled database copy is reused by the reads of one operation (snapshot backend). |
| `TITLE_TTL` | `300` | Seconds a resolved reference title is cached. |
| `EXPORT_MAX_ROWS` | `200000` | Row cap on a dashboard CSV/JSON export. |

Use `list_databases(refresh=true)` or `pools(refresh=true)` after changing tenants.
Pool mappings are also cached in memory for the server session until explicitly refreshed.

## Development and validation

```bash
python -m pip install -e '.[test]'
python -m unittest discover -s tests -v
python -m pip wheel --no-deps --wheel-dir dist .
```

Tests use temporary SQLite databases, mocked ADB, deterministic embeddings, and a
local child process speaking the runner protocol. They do not require a device or
model download. Coverage includes package/device isolation, write retry behavior,
concurrent request framing and process cleanup, index changes and model migration,
pagination, and snapshot consistency metadata.

`first-test.sh` is a separate manual emulator smoke test that builds/deploys the jar
and inspects the configured app. It defaults to the osapiens service app; set `PKG`
for another app. It can boot an emulator and launch the app, so run it deliberately.
Saved logs show earlier successful Android and MCP checks; they do not replace
running the regression suite or rechecking the current build on a device.

The optional `agent-loop.sh` development helper executes shell scripts placed in
`.agent/queue`; it is not needed to run the MCP server.
