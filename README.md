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

| Tool | Purpose |
|---|---|
| `devices` | List ADB devices. |
| `detect_package` | Resolve the default package and report its source. |
| `list_databases` | Discover SQLite files in private and external app storage, including tenant directories. |
| `backend_status` | Report runner availability, mode, and selected backend. |
| `deploy_runner` | Install the built jar and run a smoke query. |
| `schema` | Inspect tables, columns, foreign keys, indexes, counts, and optional column statistics. |
| `query` | Execute read-only SQL with a result limit. |
| `sample` | Return a small table sample. |
| `execute` | Execute a write against the live device database. |
| `snapshot` | Copy a database to the host and report its consistency guarantee. |
| `pools` | Map osapiens pool names to tables, row counts, and promoted-field definitions. |
| `entries` | Read decoded business objects by pool name, keys, or SQL filter. |
| `decode` | Decode a serialized BLOB column directly. |
| `search` | Search a local FTS5 index, ranked with BM25. |
| `semantic_index` | Build/update text documents and their local embeddings. |
| `semantic_search` | Retrieve documents by cosine similarity. |

Tools also work without an MCP client:

```bash
export ANDROID_PACKAGE=com.your.app
python android_db_mcp.py call backend_status
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
python android_db_mcp.py call semantic_search pool=CodesGroup chunk=Codes q='roof missing'
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
| `ANDROID_DB_CACHE` | `~/.cache/android-db-mcp` | Snapshots, discovery caches, and indexes. Use separate directories for different devices with the same package. |
| `ANDROID_DB_DEBUG` | Unset | Log ADB timing/fallback diagnostics to stderr. |
| `ADB` | `adb` | ADB executable. |
| `DB_LIST_TTL` | `600` | Database/pool discovery cache lifetime in seconds. |
| `INDEX_TTL` | `120` | Plain-table index lifetime in seconds. |
| `LOCALE` | `en` | Display/search translation language. |
| `EMBED_MODEL` | `BAAI/bge-small-en-v1.5` | fastembed model name. |

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
