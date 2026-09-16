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

One SQLite file per source under `~/.cache/android-db-mcp/<package>/index_<hash>.sqlite`:
a `docs` table (`doc_id`, `src_key`, `last_change`, `text`, `meta`, `vec`), an FTS5 table over the
same text, and `meta` (build time, watermark). Keyword and vector search share that file.

Four decisions:

1. **Live documents.** Built through the same runner path `query` uses, never a stale snapshot.
   Every result reports `age_s` and `stale_rows`.
2. **Chunk per embedded entity.** `chunk="Codes"` → one doc per element with the parent's title,
   `doc_id` = `key@thingId#Codes[i]`.
3. **Translations resolved before indexing.** `[{Language:"EN",Text:…},{Language:"DE",…}]` → one
   string in `LOCALE` (default `en`; `locale="de"` per call). Missing language falls back to first.
4. **Incremental on `#lastChange`.** Watermark stored; `refresh="auto"` re-reads only moved rows
   and drops deleted ones. Verified: `refreshed (+2 changed, -1 deleted)`.

Semantic: fastembed `BAAI/bge-small-en-v1.5` (384 dims, CPU), swappable via `EMBED_MODEL`.
Verified over MCP stdio: `"roof missing"` → **No Roof** under *Insufficient Protection* (no keyword
overlap), ~0.17 s/query after a 4 s build over 95 docs. Keyword search needs no extra install.

## 5. Performance

Measured on the emulator, inside one server process:

| Operation | Before | After |
|---|---|---|
| `query`, single statement | 0.8–3 s | 0.03 s |
| `entries`, decoded objects | 10–31 s | 0.08 s |
| `schema`, one pool, stats | 8 s | 0.21 s |
| `schema`, 119 tables, stats | > 60 s | 0.84 s |
| `pools`, 114 pools, cold | 7 m 50 s | 1.8 s |
| `list_databases` | ~30 s | 1.7 s |
| `search`, chunked rebuild | ~30 s | 0.05 s |
| `search`, index current | ~30 s | 0.09 s |

Causes and fixes:

- **ART start-up per call (~2 s)** → `--serve` runner, one process per identity. Biggest win.
- **Per-table round trips** (`PRAGMA table_info` + FK pragma + `COUNT(*)` × 119 tables) → one
  `pragma_table_info` join for all columns, one for FKs, `UNION ALL` counts 60 tables at a time,
  fill/distinct stats batched by SQL length.
- **Brute-force file scan** (`head -c 15` over the whole `files/**` tree) → scan only `databases/`
  dirs plus `*.db` / `*.sqlite*`, cached on disk for `DB_LIST_TTL` (600 s).

`ANDROID_DB_DEBUG=1` prints a timing line per adb call — that is how each was found.

## 6. Where everything lives

All code in `~/projects/android-db-mcp`. Nothing added to the `field-service-os` repo; nothing committed.

| Path | What |
|---|---|
| `android_db_mcp.py` | MCP server, 16 tools, `main()` entry point (md5 `6b3515a6…`) |
| `bo_codec.py` | osapiens codec + locale/flatten/chunk helpers |
| `runner/src/dbq/Main.java` | on-device runner (one-shot, `--serve`, base64 blobs) |
| `runner/build.sh`, `runner/deploy.sh` | javac + d8 · push into sandbox + smoke query |
| `runner/build/dbq.jar` | built dex jar, currently deployed (gitignored) |
| `pyproject.toml`, `MANIFEST.in`, `.gitignore` | installable package `android-db-mcp` 0.1.0 |
| `tests/test_regressions.py` | 421-line regression suite (see below) |
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

**Two lines of work diverged — reconcile before building anything new.**

The Mac version (canonical) gained packaging, the regression suite, and runner isolation with
locking, reaping and `atexit` shutdown. None of the items below are in it.

A second version exists only in the cloud sandbox from the same afternoon: the generic core split
from the osapiens layer behind plugin hooks (`DOC_SOURCES`, `SCHEMA_ANNOTATORS`, `TABLE_ALIASES`,
`DEVICE_CHANGED`, loading `plugins/*.py`), plus hybrid search, cross-pool indexes, redaction,
`dry_run` writes, backup/restore and `set_device` — with `plugins/osapiens.py` never written, so it
does not run. It also predates the packaging and the runner-isolation work, so **it must not be
pasted over the Mac file**.

The sane order: port those features onto the current `android_db_mcp.py` one at a time, each with a
test in `tests/test_regressions.py`, rather than finishing the old split wholesale. `Main.java`
already has the `dryrun` mode compiled and tested, so the write guard is the cheapest one to land
first.

| Item | Adds | Status |
|---|---|---|
| Plugin split | generic core reusable on any app | in progress |
| Hybrid search | bm25 + vectors fused by reciprocal rank | written, untested |
| Cross-pool index | `pools=[…]` / `"*"`, hits carry `meta.pool` | written, untested |
| PII redaction | `REDACT_FIELDS` before index or reply | written, untested |
| Write guard + `dry_run` | rolled-back txn reports rows; table allow-list | written, untested |
| Backup / restore | snapshot tenant DBs, restore after a test | written, untested |
| `set_device` | switch emulator mid-session, reset runners/caches | written, untested |
| Multilingual embeddings | better German recall; `EMBED_MODEL` only | not started |
| Reference expansion | ids → titles at index time | not started |
| `changes_since` diff | what the app wrote since a watermark | not started |
| `explain(key)` | one object, decoded, refs resolved one level | not started |
| SQL over decoded JSON | `json_each` views per pool | not started |
| Eval set | 10 questions with known answers | not started |

If only one thing gets built next: **`changes_since`** — it reuses everything that exists, and
"what changed in the DB since I tapped Save?" is the question an offline-first sync app raises most.

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
