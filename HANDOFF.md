# android-db-mcp — project memory

Working notes for picking this up later. Setup and tool reference live in `README.md`;
this file is the *why*, the state, and what's unfinished.
Living version (editable, commentable): https://claude.ai/code/artifact/df15c0f9-87ae-4484-be91-aa031fd88d33

Last updated: 2026-09-16. The canonical code is on the Mac in `~/projects/android-db-mcp`
(`android_db_mcp.py` md5 `6b3515a6…`, 1323 lines) — Lukas packaged and hardened it after the
first build; see §6 and §8 before changing anything.

---

## 1. What this is

`android-db-mcp` gives Claude Code live read/write access to the SQLite data of an Android app
running on an emulator, so it can answer questions about real app state instead of guessing from
source code. Built and verified against **field-service-os** (`com.osapiens.operations.serviceos`).

The starting question was "how do we do RAG for Android?". The answer is two things:

1. an **access path** onto the device, and
2. a **data layer** that turns the app's binary blobs into text worth retrieving.

The access path is the dex-jar idea, done the `run-as` way: a small Java class compiled to a dex
jar, copied into the app's `code_cache/`, launched with `app_process`. It runs with the app's own
uid and Android's own SQLite, so WAL, locks and permissions behave exactly as they do for the app.
Only result rows leave the device.

Rejected alternatives: plain file pulls (stale; WAL makes them lie), Android Studio's Database
Inspector approach (native JVMTI agent — more capable, far more work, APIs internal to the IDE),
an in-app `debugImplementation` sidecar (needs touching every project).

This is **agent-driven retrieval**, not a fixed retrieve-then-generate pipeline: Claude decides
when to search and can verify any fuzzy hit against the exact row with SQL.

## 2. How it works

```
Claude Code --MCP stdio--> android_db_mcp.py --adb shell -T--> dbq --serve (app uid | shell uid | root)
                                            \--fallback: run-as cat--> host snapshot (+ -wal/-shm)
```

- Runner: `runner/src/dbq/Main.java` → `build.sh` (javac + d8) → `dbq.jar` → `deploy.sh`.
- Requests are **base64 JSON**, so SQL survives the adb → sh → run-as quoting layers.
- Deploy trick: `/data/local/tmp` is `shell:shell 0771`, so the app uid can't read it. The jar is
  streamed across the uid boundary: `cat … | run-as pkg sh -c 'cat > code_cache/dbq.jar'`.

Identity is chosen per database:

| Identity | Reaches | Jar location | When |
|---|---|---|---|
| `run-as <pkg>` | private `databases/`, `files/` | `code_cache/dbq.jar` | default, debuggable build |
| adb `shell` | `/storage/emulated/0/Android/data/<pkg>/files/**` | `/data/local/tmp/dbq.jar` | FUSE denies `run-as` there |
| `root` | everything, incl. non-debuggable | `/data/local/tmp/dbq.jar` | `ANDROID_DB_RUNNER=root` (emulator) |

Each `app_process` launch costs ~2 s of ART start-up, so the runner has a `--serve` mode: one
process per identity kept alive over `adb shell -T`, one base64 request line in, one JSON line out.
A `ping` confirms support; a dead process restarts transparently; `ANDROID_DB_PERSIST=0` or an old
jar falls back to one-shot.

Snapshot fallback (no jar deployed): `run-as cat` of `.db` + `-wal` + `-shm` (or on-device
`sqlite3 .backup` where that binary exists), queried read-only on the host. Copying only the `.db`
is the classic mistake — WAL keeps recent rows in `-wal`.

## 3. The osapiens data layer

The app's real content is **not in columns** — it is in a binary `#value` BLOB, and the columns
beside it are mostly empty. That discovery is what made retrieval possible.

Business objects live in `BusinessObjectEntry#<applicationId>#<poolId>`. `#value` is osapiens'
`SerializationUtil` format: one type byte then a big-endian `DataOutput` payload (10 map, 11 list,
14 byte[], 27 string in Java modified UTF-8, 31 large string, 33 TWKB geometry, 20–26 scalars).
`bo_codec.py` is a Python port plus retrieval helpers.

Reference sources, all under
`node_modules/@osapiens/mobile-client-android/android/app/src/main/java/com/osapiens/operations/`:

| File | Defines |
|---|---|
| `utils/SerializationUtil.java` | the `#value` wire format, type tags, read/write |
| `dataaccess/sqlite/tables/SQLiteTables.java` | entry-table naming and DDL, the `#`-columns |
| `objects/BusinessObjectNamedField.java` | promoted-column spec: name, path, dataType in sequence |
| `objects/ValueType.java` | dataType enum 1–9 (INT, FLOAT, STRING, BOOLEAN, ANY, BLOB_BASE64, GEOSHAPE, …) |

Row columns: `#key` (object id), `#thingId`, `#value`, promoted columns, `#immutable`,
`#lastChange` (epoch millis). Pool names: `BusinessObjectPool(poolId, poolName, applicationId)`.
Promoted-column specs: `BusinessObjectNamedField.data` → list of byte arrays, each three values
written back to back.

**The decisive detail:** in a named-field path, `default` means "each element of this list".
`CodesGroup.Title` has path `Title.default.Text` and is filled **0/56**, while `Type` (path `Type`)
is filled **56/56**. Every human-readable label exists only inside the BLOB. Hence `schema` reports
a fill rate per column — an always-NULL column is a signal, not missing data.

Tools: `pools()` (names → entry tables, row counts, named-field paths), `entries(pool=…)` (decoded
objects with locale-resolved `title`; `keys=[…]` follows references), `decode(db, table, column)`
(any other serialized BLOB).

QA tenant scale: 114 pools, 436 rows — `CodesGroup` 56, `SmartForm` 80, `Address` 35,
`SmartFormQuestion` 21; `ApplicationData.db` separately has 25 tables, `BundleFileMetadata` 6006 rows.

## 4. Retrieval

One SQLite file per source under `~/.cache/android-db-mcp/<device-serial>/<package>/index_<hash>.sqlite`:

| Table | What |
|---|---|
| `docs` | `doc_id`, `src_key`, `last_change`, `text`, `meta`, `vec` — one row per retrieval unit |
| `fts` | FTS5 over the same text (bm25) |
| `decoded` | `src_key`, `key`, `thing`, `last_change`, `title`, `json` — the decoded mirror |
| `paths` | `path`, `kind`, `n`, `n_filled`, `example` — which fields exist and how often they are filled |
| `sources` | `src_key`, `last_change` — incremental refresh bookkeeping |
| `meta` | build time, watermark, embedding model and dimensions |

Everything lives in one file, so keyword hits, vectors and decoded JSON can be joined in a single
query. Index version 4.

Six decisions:

1. **Live documents.** Built through the same runner path `query` uses, never a stale snapshot.
   Every result reports `age_s` and `stale_rows`.
2. **Chunk per embedded entity.** `chunk="Codes"` → one doc per element with the parent's title,
   `doc_id` = `key@thingId#Codes[i]`.
3. **Translations resolved before indexing.** `[{Language:"EN",Text:…},{Language:"DE",…}]` → one
   string in `LOCALE` (default `en`; `locale="de"` per call). Missing language falls back to first.
4. **Incremental on `#lastChange`.** Watermark stored; `refresh="auto"` re-reads only moved rows
   and drops deleted ones. Verified: `refreshed (+2 changed, -1 deleted)`.
5. **References resolved at index time.** Ids are replaced with titles before the text is written
   (`Priority: High`, not a uuid). Titles for a whole build are fetched in one pass per target pool,
   so this costs a couple of queries no matter how many objects are indexed. A chunked document only
   claims the references that sit inside its own element (`Codes[3].AssetId`), not its siblings'.
6. **The decoded mirror is written during the same pass** that builds the documents — the objects
   are already decoded, so `json_each`/`json_extract` SQL costs nothing extra to enable.

Semantic: fastembed `BAAI/bge-small-en-v1.5` (384 dims, CPU), swappable via `EMBED_MODEL`.
`search(mode=hybrid)` fuses bm25 and vectors with weighted RRF (`1.0` / `0.5`) and explains every hit.
Embeddings are off by default — see §8.

### What the evaluation actually says

`eval.py` scores known-item queries generated from the indexed documents (exact title / partial
title / text from another field). On `CodesGroup` chunked by `Codes`, 108 cases:

| Retriever | descriptive | exact | partial | ALL (MRR@10) |
|---|---|---|---|---|
| bm25 | 0.65 | 0.87 | 0.82 | **0.78** |
| vector | 0.20 | 0.81 | 0.79 | 0.57 |
| hybrid | 0.66 | 0.86 | 0.82 | **0.78** |

Conclusions, which are worth keeping because they cut against the usual assumption:

- **Keyword search is the strong retriever on this data.** The content is short coded titles
  ("Failure Group PFSV0J Code 2"); there is little semantics for embeddings to exploit.
- **Hybrid is worth it for the descriptive case** (r@1 0.65 vs 0.62) and costs nothing elsewhere.
  Weight sweep: 1.0/0.0 … 1.0/1.0 all land on 0.77–0.78; only 0.5/1.0 (vector-led) drops to 0.73.
  Default 1.0/0.5.
- **The multilingual model is worse here.** `paraphrase-multilingual-MiniLM-L12-v2` scores vector
  MRR 0.37 against bge-small's 0.54. The tenant's "German" is English strings with " DE" appended,
  so there is nothing for it to win. Re-run the comparison when real German content exists —
  the harness is in place.

## 5. Performance

Two rounds of work. The first (earlier sessions) removed per-call ART start-up and per-table round
trips. The second (this session) removed a hidden `adb` subprocess from the per-query path.

### Round 2, measured with `bench.py ab` — three interleaved rounds, best of five reps each

| Scenario | Before | After | Change |
|---|---|---|---|
| `query`, single statement | 0.018 s | 0.003 s | −86% |
| `sample`, 5 rows | 0.020 s | 0.003 s | −83% |
| `entries`, limit 5 | 0.020 s | 0.004 s | −78% |
| `entries`, limit 50 | 0.026 s | 0.011 s | −59% |
| `schema`, no stats | 0.111 s | 0.030 s | −73% |
| `schema`, stats | 0.285 s | 0.080 s | −72% |
| `pools`, warm | 0.094 s | 0.032 s | −66% |
| `pools`, cold | 0.155 s | 0.045 s | −71% |
| `search`, warm index | 0.021 s | 0.005 s | −78% |
| `search(mode=semantic)` | 0.028 s | 0.008 s | −73% |
| index rebuild (full) | 0.021 s | 0.007 s | −68% |
| `list_databases`, cold | 0.107 s | 0.106 s | — (pure adb, unchanged) |

New tools, for reference: `app_context` cold 0.21 s / warm ~0 s, `refs` cold 0.007 s / cached ~0 s,
`search(mode=hybrid)` 0.008 s, `fields` 0.004 s, `query_decoded` 0.004 s, `mark` (436 objects, 57 pools)
0.081 s, `changes_since` 0.072 s.

Causes and fixes, round 2:

- **`adb get-serialno` ran on every device query** to build the runner cache key — a ~16 ms process
  spawn per statement, which is most of what a warm query cost. Now resolved once per process
  (`_serial()`), and an explicit `ANDROID_SERIAL` bypasses it entirely.
- **Runner start-up was serialised.** Reaching a private database and an external one starts two
  runners (`run-as` and `shell`), ~1.2 s of ART boot each, previously one after the other under a
  global lock. Now a per-identity lock plus `_prewarm()` starts them in parallel:
  `app_context` cold went 3.2 s → 1.0 s, and to 0.21 s once discovery caches exist.
- **`_pool_index` was computed even when an object had no reference-shaped fields** — short-circuited.

### Round 3 — profiling by function (this session)

`profile_tools.py` runs every tool under cProfile and splits wall time into **device wait** (the
runner pipe, an adb subprocess) and **host CPU**, because only the second is worth optimising.
First measurement: 1,253 ms wall = 829 ms device + 424 ms host — and **59% of the host CPU was in
the BLOB decoder's byte primitives.**

Three fixes, each measured:

| Fix | Evidence |
|---|---|
| `_modified_utf8` scanned every character of every string with `any(0xD800 <= ord(ch) ...)` | That genexpr plus `any` plus `ord` was 100 ms — a quarter of all host CPU. Now: `bytes.isascii()` fast path, and a surrogate is detected by searching the *bytes* for `0xED` (a surrogate always encodes as ED A0 80 … ED BF BF), so the character walk runs only for the rare string that needs it. |
| `_Reader` sliced a fresh `bytes` per primitive and called `struct.unpack` each time | Cached buffer length, compiled `struct.Struct` objects read with `unpack_from`, and `u8` indexes instead of slicing. |
| The type dispatch was an if-chain in tag order | Measured over 30,440 real values: **STRING is 65% of them and was the sixteenth comparison.** Reordered by frequency (STRING, LIST, MAP, INT, BYTES, BOOL, NULL, LONG, …). |

Measured in isolation, no device involved, over the 436 real objects of this tenant:

| | time | throughput |
|---|---|---|
| original | 35.1 ms | 6.4 MB/s |
| optimised | **15.6 ms** | **14.3 MB/s** |

**2.25× faster, 54% less CPU — and byte-identical output on all 436 objects**, checked against the
original implementation from git rather than assumed.

Two more, away from the codec:

- **`related()` read one object per round trip.** Now one query per *pool* per hop: depth 2 went
  from ~20 device queries to 7, and its host CPU from 13.1 ms to 2.7 ms.
- **Cache databases were paying for durability they do not need.** The index files and
  `watch.sqlite` under `ANDROID_DB_CACHE` are derived — if one is lost the next refresh rebuilds
  it — so `synchronous=OFF` (plus WAL for indexes, MEMORY for the watch store).
- `_is_secret` is memoised on the field name, which repeats across every object of a pool. The
  cache key includes the redaction settings, so changing them is never served from the cache.

Host CPU per tool, before → after:

| tool | before | after |
|---|---|---|
| `pools` | 60.6 ms | **19.6 ms** |
| `entries` (50 rows) | 20.1 ms | **8.3 ms** |
| `entries` (fields+filters) | 11.6 ms | **4.0 ms** |
| `related` (depth 2) | 13.1 ms | **2.7 ms** |
| `explain_object` | 1.7 ms | **0.6 ms** |
| index rebuild (80 objects) | 59.3 ms | **43.0 ms** |
| `mark` (436 objects) | 123.6 ms | **98.4 ms** |

**What is left is not worth chasing.** After these fixes ~75% of wall time is the device answering,
and the largest remaining host cost is `_read` itself — the recursive dispatcher, which is the work.
`mark`/`changes_since` are the heaviest tools (≈300 ms) and both are dominated by reading every
object of every pool off the device; the host share is decode plus one `json.dumps` per object,
already near the floor.


### Measurement notes (read before trusting a number)

The emulator drifts: the same call measured 0.47 s and 1.36 s twenty seconds apart, and a naive
before/after run produced two "regressions" (`list_databases` +70%, index build +256%) that both
vanished under interleaving. Use `bench.py ab --before-src <baseline checkout> --rounds 3`, which
alternates the two builds and keeps the best time per scenario. `bench.py run` alone is fine for
spotting orders of magnitude, not for ±30%.

Round 1 results, kept for history:

| Operation | Before | After |
|---|---|---|
| `query`, single statement | 0.8–3 s | 0.03 s |
| `entries`, decoded objects | 10–31 s | 0.08 s |
| `schema`, 119 tables, stats | > 60 s | 0.84 s |
| `pools`, 114 pools, cold | 7 m 50 s | 1.8 s |
| `list_databases` | ~30 s | 1.7 s |

- **ART start-up per call (~2 s)** → `--serve` runner, one process per identity. Biggest win.
- **Per-table round trips** → one `pragma_table_info` join for all columns, one for FKs,
  `UNION ALL` counts 60 tables at a time, fill/distinct stats batched by SQL length.
- **Brute-force file scan** → scan only `databases/` dirs plus `*.db` / `*.sqlite*`, cached for
  `DB_LIST_TTL`.

`ANDROID_DB_DEBUG=1` prints a timing line per adb call — that is how each was found.

## 6. Where everything lives

All code in `~/projects/android-db-mcp`. Nothing added to the `field-service-os` repo; nothing committed.

| Path | What |
|---|---|
| `android_db_mcp.py` | MCP server, 20 tools, `main()` entry point |
| `bo_codec.py` | osapiens codec + locale/flatten/chunk helpers |
| `runner/src/dbq/Main.java` | on-device runner (one-shot, `--serve`, base64 blobs) |
| `runner/build.sh`, `runner/deploy.sh` | javac + d8 · push into sandbox + smoke query |
| `runner/build/dbq.jar` | built dex jar, currently deployed (gitignored) |
| `pyproject.toml`, `MANIFEST.in`, `.gitignore` | installable package `android-db-mcp` 0.1.0 |
| `tests/test_regressions.py` | regression suite, 71 tests (see below) |
| `bench.py` | scenario timings; `ab` mode interleaves two checkouts to cancel emulator drift |
| `eval.py` | retrieval evaluation: known-item query sets, model and fusion-weight comparison |
| `profile_tools.py` | per-function profiling; splits wall time into device wait and host CPU |
| `explorer.py` | dashboard server on `127.0.0.1:8765`; loads the tenant into memory, `--dump`/`--fixtures` for offline work |
| `web/index.html`, `web/app.css`, `web/app.js` | the dashboard itself — overview, data grids, object views, ERD, schema browser, SQL console, changes |
| `evals/`, `.bench/` | generated query sets and timing results (gitignored) |
| `README.md` | setup, tools, env vars, manual test checklist |
| `first-test.sh` | 7-step bring-up check → `first-test.log` |
| `agent-loop.sh`, `.agent/` | test scaffolding (Claude drops scripts in `queue/`, reads `done/`) |

**It is a Python package now.** `pip install -e .` in a venv; console script
`android-db-mcp` → `android_db_mcp:main`; deps `mcp>=1,<3`, extras `semantic`
(fastembed + numpy) and `test` (numpy). An MCP entry can therefore call the installed
script instead of `python3 /path/android_db_mcp.py`.

`tests/test_regressions.py` covers databases, runner behaviour, snapshots and an MCP
stdio round trip, with a fake embedder so semantic paths run without fastembed —
run it before and after any change here.

Runner processes are cached by **(adb path, serial, package, identity)** under a lock,
failed ones are closed and reaped, and `atexit` closes them on shutdown — so several
devices or packages in one session cannot cross-talk.

In `~/field-service-os` (both private): `.mcp.json` has an `android-db` entry beside azure-devops,
context7, deepwiki, playwright — the file is untracked and **also holds a Context7 API key in plain
text**; `CLAUDE.md` is gitignored by the repo's own rules and tells Claude where tenant databases
are and that promoted columns lie.

The server entry runs via `bash -c` (pyenv shims + platform-tools on `PATH`), sets `ANDROID_DB_JAR`
and `ADB`, and deliberately does **not** set `ANDROID_PACKAGE` — it's auto-detected from
`android/app/build.gradle` `applicationId` (handles the `hasProperty(…) ? … : "…"` ternary), else
the foreground app. `detect_package` shows which source won.

Host: Python 3.10.4 (pyenv), `mcp` 2.x (hence the `FastMCP`/`MCPServer` compat import),
`fastembed` 0.8.0, SDK at `~/Library/Android/sdk` (build-tools 37), emulator `emulator-5554`,
Android 14 / SDK 34, user build, SELinux enforcing, **no `sqlite3` binary on the image**.

## 7. Verified facts (keep these)

- `run-as` + `app_process` + dex in `code_cache/` **works** on Android 14 user build, SELinux
  enforcing, no root.
- Its `avc: denied` lines (`userdebug_or_eng_prop`, `odsign_prop`, `apex-info-list.xml`, `boot.art`
  lock, `idmap2`, FUSE `ioctl`) are ART probing — harmless, JSON still returns.
- FUSE refuses the `run-as` domain inside `Android/data/<pkg>` but admits adb `shell`.
- The image has no `sqlite3` binary at all.
- End-to-end: catalog `Cat-2` → `Groups.PART` → `entries(keys=…)` → "Part 01" with codes
  "Code 01/02"; `search(pool="CodesGroup", chunk="Codes", q="protection")` → **No Roof** under
  *Insufficient Protection*; `entries(locale="de")` → German titles.

Bugs fixed (in case they recur): redeploy failing over a `chmod 444` jar (now `rm -f` first);
`#` in tenant filenames breaking a `file:` URI; `mode=ro` failing on a WAL copy with no `-wal`;
`mcp` 2.x renaming `FastMCP` → `MCPServer`; `claude mcp add` taking `-e` variadically (server name
first); named-field specs being a *sequence*, not a map; fastembed's download logging needing to be
kept off stdout (it carries the MCP protocol).

Two test harnesses: a **simulated device** in the cloud sandbox (fake `adb` + fake `app_process`
honouring the same JSON contract, fixtures for WAL, external storage, FUSE denial, `#` names,
rolled-back writes), and the **agent loop** on the Mac (`agent-loop.sh` runs `.agent/queue/*.sh`,
writes `.agent/done/*.out` for Claude to read).

## 8. Open work

Everything below the line in the previous plan has landed. What remains is listed after it.

### Built in this session

| Item | Adds | Where |
|---|---|---|
| Per-device cache keys | two emulators running the same app/tenant no longer share discovery caches, snapshots or indexes | `_serial`, `_device_tag`, `_pkg_cache` |
| `app_context` | one call: device, package, backend, databases by tenant, populated pools, existing indexes | cached `CONTEXT_TTL` (60 s) |
| Reference map | verified `XxxId` → pool, derived by sampling + probing, never hand-configured | `refs`, `_ref_map` |
| Reference expansion | ids resolved to titles inside indexed documents and `explain_object` | `_docs_from_pool`, `_expand_refs` |
| `explain_object` | one object decoded, labelled, references resolved, optional incoming refs | locates the key across pools when no pool given |
| `mark` / `marks` / `changes_since` | before/after over an app action, with field-level diffs | `watch.sqlite` per device+package |
| Decoded mirror | every object's decoded `#value` as JSON in the index file | `decoded` table |
| `fields` | field paths with fill rates and examples | `paths` table |
| `query_decoded` | real SQL with `json_each` / `json_extract` over decoded objects | read-only, SELECT/WITH only |
| `entries(fields=…, filters=…)` | projection and nested-value filters with honest scan accounting | reports `scanned` / `matched` / `complete` |
| `search` (one tool, four modes) | bm25 + vectors fused (weighted RRF), filters, per-hit `matched_by` and `why` | weights measured, not guessed |
| Vector cache | the vector matrix is stacked and L2-normalised once per index file | `_vectors`, keyed on mtime+size |
| `eval.py` | known-item eval set, three query families, model and weight comparison | `evals/<pool>-<chunk>-<locale>.json` |
| `bench.py` | scenario timings plus interleaved `ab` mode that survives emulator drift | `.bench/*.json` |
| Dashboard | overview, data grids with decoded columns and filters, object views, ERD with focus mode, schema browser, SQL console, change watch, secret masking | `explorer.py` + `web/` |
| Nested titles | `display_title` falls back one level down, so `Text.Title` is found — SmartForm went from ~10/80 titled to 80/80, which improves search documents too | `bo_codec.py` |

25 tools; 71 offline regression tests (was 16 tools, 30 tests).

### Review pass (this session)

Two parallel reviews of the whole tree found ~30 real defects; the ones worth remembering:

| Defect | Why it mattered |
|---|---|
| `mark()` stored only pool *names* | `changes_since` re-resolved them and raised "Ambiguous pool" whenever two tenants or two applicationIds shared a name — the headline feature was unusable on a multi-tenant install. Marks now store db + table. |
| `_title_cache` cached failures forever | One adb hiccup wrote "(no X object)" into every document until the server restarted, and a renamed object kept its old label for good. Now: failures are retried, entries expire after `TITLE_TTL`. |
| `entries(filters=…)` pagination dead-end | A scan window with no matches returned `truncated: true` with a null cursor, so the rest of the pool was unreachable. The cursor is now the last row *scanned*. |
| `changes_since(since=…)` silent loss | Per-pool row caps were applied and never reported. It now names the pools that hit the cap. |
| Every internal read re-pulled the database | On the snapshot backend one index refresh could copy gigabytes over adb, and each batch came from a different point in time. `SNAPSHOT_TTL` coalesces the reads of one operation. |
| `_shutdown_runners()` cleared the startup locks | `deploy_runner` racing a query started two runners for one key and leaked the loser. |
| Dashboard published a half-built index | Readers had no lock; Reload during another request could throw "dictionary changed size", show empty pools, or mix a new object set with an old graph. Rebuilds now swap an immutable `View` in one assignment. |
| Dashboard auto-refresh reloaded the device every 5 s | Handler threads piled up behind the store lock. Polling no longer reloads; only the explicit Refresh does. |
| Pool grid sorting never worked | The state stored `lastChange` while the column was `#lastChange`, so direction never toggled and no arrow ever drew. Any column sorts now, including decoded paths. |
| No `Host` check | Binding to 127.0.0.1 does not stop DNS rebinding; a web page could read the app's data and run SQL. Now refused unless addressed as localhost. |
| `_static` prefix match, CRLF in export filenames, `%` wedging the router, pan/zoom listener leak, duplicate column names, `related()` unbounded depth | Each fixed and covered by a test where testable. |

Redaction caught a bug in itself: matching `pass` as a substring hid `Passes` and `PassengerCount`.
It matches whole words now.

### The cut (this session)

30 tools measured at ~3,764 tokens of schema in every session. Removed as redundant or unused:
`sample` (it is `query` with LIMIT), `detect_package` and `backend_status` (both in `app_context`),
`query_plan` (added for the wrong reason — these queries run in 3 ms), `drop_index`
(`refresh="full"` already rebuilds), `marks` (folded into `changes_since.recent_marks`),
`indexes` (in `app_context`), `semantic_index` (every search builds on demand).

`search`, `semantic_search` and `hybrid_search` were one operation with different weights, so they
are now `search(mode=auto|keyword|semantic|hybrid)`. **20 tools, ~2,963 tokens — 21% less context
per session, no capability lost.**

Embeddings default to off (`EMBEDDINGS=on` to enable). The eval says they match keyword search
overall and lose on every family except none; the case they used to win — natural phrasing — was
fixed instead by retrying an empty FTS `AND` query as `OR`, which moved keyword MRR on that family
from 0.04 to 0.59. Keep measuring before re-enabling them.

### Still open

| Item | Adds | Status |
|---|---|---|
| Plugin split | generic core reusable on any app | deferred until a second app needs it |
| Cross-pool index | `pools=[…]` / `"*"` so "where is X mentioned?" needs no pool guess | not started — check first whether dropping the pool filter is enough, since `meta` already carries the pool |
| PII redaction | `REDACT_FIELDS` before index or reply | sandbox draft only |
| Write guard + `dry_run` | rolled-back txn reports rows; table allow-list | `Main.java` already has `dryrun` compiled; the Python side is not wired |
| Backup / restore | snapshot tenant DBs, restore after a test | sandbox draft only |
| `set_device` | switch emulator mid-session, reset runners/caches | sandbox draft only; `_serial()` now honours an explicit `ANDROID_SERIAL` change, which is the hard part |
| Multilingual embeddings | better German recall | **evaluated and rejected for now** — see §4; revisit when the tenant has real German content |
| Incremental `paths` | avoid re-walking every object when one changes | only matters above ~20k objects, where the table is currently skipped (`PATHS_MAX`) |

The sandbox branch (plugin hooks, redaction, backup/restore, `set_device`) still predates the
packaging and runner-isolation work, so it must be ported item by item with a test — never pasted
over `android_db_mcp.py`.

If only one thing gets built next: **wire the write guard**, because `execute()` is the only tool
that can damage the app's data and the runner already supports the rolled-back dry run.

## 9. Operating notes

- **After reinstall / clear-data** the jar in `code_cache/` is gone; tools say so, `deploy_runner`
  restores it (Claude can call it). After editing `Main.java`: `runner/build.sh` then
  `runner/deploy.sh <package>`.
- **After switching tenant/app**: `list_databases(refresh=true)`, `pools(refresh=true)` — discovery
  is cached 10 min. Full reset: delete `~/.cache/android-db-mcp/<package>/`.
- **If `app_process` is denied under `run-as`** (empty reply + `avc: denied` naming `runas_app`):
  `ANDROID_DB_RUNNER=root`. Also covers non-debuggable builds on an emulator.
- **Debugging**: `ANDROID_DB_DEBUG=1` (per-call timings), `ANDROID_DB_PERSIST=0` (one-shot),
  `python3 android_db_mcp.py call <tool> k=v` (any tool from the terminal, no MCP client).
- **`execute` writes live app data.** Prefer `dry_run` once it lands; keep the CLAUDE.md line
  "only when I ask"; `restore` only helps if `backup` ran first.
- **Data leaves the device when queried** — it enters Claude's context. `User`, `Address`,
  `BusinessPartner` hold real names/emails even on QA. Keep this on QA tenants; redaction is
  written but unverified.
- **Scope**: `run-as` only works on debuggable builds — this cannot reach a release app on a phone.
