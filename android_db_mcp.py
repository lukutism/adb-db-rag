#!/usr/bin/env python3
"""
android-db-mcp — an MCP server that lets Claude Code read (and optionally write) the SQLite
databases of an Android app running on an emulator/device.

Two backends, picked per package:

* jar (preferred when deployed): the `dbq` runner (runner/build/dbq.jar) is executed on the device
  with app_process — as the app uid via `run-as` for the private data dir, as the adb `shell` user
  for external app-specific storage (/storage/emulated/0/Android/data/<pkg>/files, where FUSE
  refuses run-as), or as root. SQL runs with Android's own SQLite; only result rows leave the device.
* snapshot (fallback): the database (+ -wal/-shm) is copied to the host and queried locally,
  read-only. Retrieval indexes use the selected read backend, decoding only needed records.

Config (env vars):
  ADB                 path to adb (default "adb")
  ANDROID_SERIAL      device id when several are attached
  ANDROID_PACKAGE     default package (optional: else detected from android/app/build.gradle
                      applicationId under $CLAUDE_PROJECT_DIR / cwd, else the foreground app)
  ANDROID_DB_BACKEND  auto | jar | snapshot   (default auto: jar if deployed, else snapshot)
  ANDROID_DB_RUNNER   run-as | root           (root = `adb root` emulator, works for any package)
  ANDROID_DB_JAR      path to built dbq.jar for the deploy_runner tool (default runner/build/dbq.jar)
  ANDROID_DB_CACHE    snapshot/index directory (default ~/.cache/android-db-mcp)

Run:  python3 android_db_mcp.py     (stdio transport, what Claude Code expects)
"""
from __future__ import annotations

import atexit
import base64
import functools
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import threading
import time
from functools import wraps
from pathlib import Path
from typing import Any

try:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _MCPServer
    from mcp.server.fastmcp.exceptions import ToolError as _ToolError
except ModuleNotFoundError:  # mcp 2.x renamed FastMCP -> MCPServer
    from mcp.server.mcpserver import MCPServer as _MCPServer
    from mcp.server.mcpserver.exceptions import ToolError as _ToolError

ADB = os.environ.get("ADB", "adb")
SERIAL = os.environ.get("ANDROID_SERIAL")
DEFAULT_PKG = os.environ.get("ANDROID_PACKAGE")
BACKEND = os.environ.get("ANDROID_DB_BACKEND", "auto")
RUNNER = os.environ.get("ANDROID_DB_RUNNER", "run-as")
JAR = Path(os.environ.get("ANDROID_DB_JAR", Path(__file__).parent / "runner" / "build" / "dbq.jar"))
CACHE = Path(os.environ.get("ANDROID_DB_CACHE", Path.home() / ".cache" / "android-db-mcp"))
CACHE.mkdir(parents=True, exist_ok=True)

mcp = _MCPServer("android-db")
_jar_state: dict[str, bool] = {}


def _forget_jar(package: str) -> None:
    """Drop every cached jar probe for this package (one entry per jar copy)."""
    for k in [k for k in _jar_state if k.split(":", 1)[0] == package]:
        _jar_state.pop(k, None)
_TOOLS: dict[str, Any] = {}  # plain registry so the `call` CLI works on any mcp version


def tool():
    def deco(fn):
        _TOOLS[fn.__name__] = fn
        @wraps(fn)
        def exposed(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except (ValueError, RuntimeError, OSError, sqlite3.Error, subprocess.SubprocessError) as e:
                # MCP 2 hides unexpected exceptions. Keep actionable failure messages,
                # particularly uncertain write outcomes, visible to the caller.
                raise _ToolError(str(e)) from e
        mcp.tool()(exposed)
        return fn
    return deco

# --------------------------------------------------------------------------- adb helpers


def _adb(*args: str, binary: bool = False, check: bool = True) -> bytes | str:
    cmd = [ADB]
    if SERIAL:
        cmd += ["-s", SERIAL]
    cmd += list(args)
    t0 = time.time()
    res = subprocess.run(cmd, capture_output=True, timeout=120)
    if os.environ.get("ANDROID_DB_DEBUG"):
        print(f"[adb {time.time() - t0:5.2f}s] {' '.join(args)[:160]}", file=sys.stderr)
    if check and res.returncode != 0:
        raise RuntimeError(f"adb {' '.join(args)!r} failed: {res.stderr.decode(errors='replace').strip()}")
    return res.stdout if binary else res.stdout.decode(errors="replace")


_device_ident: dict[str, str] = {}


def _serial() -> str:
    """The serial of the device we talk to, resolved once per process.

    Also keeps `adb get-serialno` out of the per-query path: _device_sql used to spawn adb for
    every single statement just to look up the runner cache key."""
    if SERIAL:
        return SERIAL          # explicit choice wins and is never cached, so it can change
    if "serial" not in _device_ident:
        try:
            s = str(_adb("get-serialno", check=False)).strip()
        except Exception:
            s = ""
        s = "" if s == "unknown" else s
        if not s:
            # The device is booting, offline or unauthorised. Caching that would pin every later
            # query to the one-shot runner and dump every cache into a shared directory, so the
            # answer is used once and asked again next time.
            return "unknown-device"
        _device_ident["serial"] = s
    return _device_ident["serial"]


def _device_tag() -> str:
    """Filesystem-safe device id. Caches and indexes are per device: two emulators running the
    same app and tenant have different data and must never share discovery or index files."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", _serial())


def _pkg_cache(package: str) -> Path:
    """Cache directory for this device + package."""
    d = CACHE / _device_tag() / package
    d.mkdir(parents=True, exist_ok=True)
    return d


_detected: dict[str, str] = {}


def _package_from_project() -> str | None:
    """applicationId from android/app/build.gradle(.kts) under the project dir (or cwd)."""
    roots = [os.environ.get("CLAUDE_PROJECT_DIR"), os.environ.get("ANDROID_PROJECT_DIR"), os.getcwd()]
    for root in filter(None, roots):
        for rel in ("android/app/build.gradle", "android/app/build.gradle.kts", "app/build.gradle", "app/build.gradle.kts"):
            f = Path(root) / rel
            if f.is_file():
                for line in f.read_text(errors="replace").splitlines():
                    if "applicationId" in line and "Suffix" not in line:
                        lits = re.findall(r'["\']([A-Za-z][A-Za-z0-9_.]+)["\']', line)
                        if lits:
                            _detected["source"] = str(f)
                            return lits[-1]     # last literal = the default in `hasProperty(...) ? ... : "..."`
    return None


def _package_from_device() -> str | None:
    """The app currently in the foreground on the device (Android 10+ dumpsys formats)."""
    out = str(_adb("shell", "dumpsys activity activities 2>/dev/null | grep -E 'topResumedActivity|mResumedActivity|ResumedActivity' | head -3", check=False))
    m = re.search(r"\s([A-Za-z][A-Za-z0-9_.]+)/[A-Za-z0-9_.$]+", out)
    if not m:
        out = str(_adb("shell", "dumpsys window 2>/dev/null | grep -E 'mCurrentFocus|mFocusedApp' | head -2", check=False))
        m = re.search(r"\s([A-Za-z][A-Za-z0-9_.]+)/[A-Za-z0-9_.$]+", out)
    if m:
        _detected["source"] = "foreground app on device"
        return m.group(1)
    return None


def _pkg(package: str | None) -> str:
    p = package or DEFAULT_PKG or _detected.get("package")
    if not p:
        p = _package_from_project() or _package_from_device()
        if p:
            _detected["package"] = p
    if not p:
        raise ValueError("No package given: set ANDROID_PACKAGE, run inside a project with "
                         "android/app/build.gradle, or bring the app to the foreground on the device")
    if not re.fullmatch(r"[A-Za-z0-9_.]+", p):
        raise ValueError(f"Suspicious package name: {p!r}")
    return p


def _in_sandbox(package: str, shell_cmd: str, binary: bool = False, check: bool = True) -> bytes | str:
    """Run a shell command with cwd = /data/data/<pkg>, as the app uid (run-as) or as root."""
    if RUNNER == "root":
        full = f"cd /data/data/{package} && {shell_cmd}"
        return _adb("exec-out", f"sh -c {shlex.quote(full)}", binary=binary, check=check)
    # exec-out gives raw stdout (no CRLF mangling), which matters for binary files.
    return _adb("exec-out", f"run-as {package} sh -c {shlex.quote(shell_cmd)}", binary=binary, check=check)


def _device_has_sqlite3(package: str) -> bool:
    out = _in_sandbox(package, "command -v sqlite3 >/dev/null 2>&1 && echo yes || echo no", check=False)
    return "yes" in str(out)


EXTERNAL_FILES = "/storage/emulated/0/Android/data/{pkg}/files"   # getExternalFilesDir()
EXTERNAL_FILES_ROOT = "/data/media/0/Android/data/{pkg}/files"      # same dir as seen by root


def _safe_db_name(db: str) -> str:
    """Accept a bare name (looked up in databases/), a path relative to the app data dir, or an
    absolute path (e.g. external app-specific storage). Reject anything with '..' or shell noise."""
    if not re.fullmatch(r"[A-Za-z0-9_.@+#\-/]+", db) or ".." in db or db.startswith("."):
        raise ValueError(f"Suspicious database path: {db!r}")
    return db


EXTERNAL_PREFIXES = ("/storage/emulated/0/", "/sdcard/")


def _is_external(db: str) -> bool:
    return db.startswith(EXTERNAL_PREFIXES)


def _as_shell(shell_cmd: str, binary: bool = False, check: bool = True) -> bytes | str:
    """Run as the adb `shell` user. On Android 11+ FUSE lets shell into Android/data/<pkg>/ while
    it refuses the run-as (runas_app) domain — so external app-specific storage is read this way."""
    return _adb("exec-out", f"sh -c {shlex.quote(shell_cmd)}", binary=binary, check=check)


def _exec_for(package: str, db: str, shell_cmd: str, binary: bool = False, check: bool = True) -> bytes | str:
    """Pick the identity that can reach this database: root, shell (external storage) or run-as."""
    if RUNNER != "root" and _is_external(db):
        return _as_shell(shell_cmd, binary=binary, check=check)
    return _in_sandbox(package, shell_cmd, binary=binary, check=check)


def _db_path(package: str, db: str) -> str:
    """Resolve what the user called `db` to a path usable inside the sandbox (cwd = app data dir)."""
    db = _safe_db_name(db)
    if "/" not in db:
        return f"databases/{db}"
    if RUNNER == "root" and db.startswith("/storage/emulated/0/"):
        return "/data/media/0/" + db[len("/storage/emulated/0/"):]
    return db


def _cache_name(db: str) -> str:
    return _safe_db_name(db).strip("/").replace("/", "__")


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


# --------------------------------------------------------------------------- jar backend


def _jar_path(package: str, db: str | None = None) -> str:
    if RUNNER == "root" or (db is not None and _is_external(db)):
        return "/data/local/tmp/dbq.jar"       # readable by root and by shell
    return "code_cache/dbq.jar"                 # inside the sandbox, for run-as


def _jar_available(package: str, db: str | None = None) -> bool:
    """Can the runner serve this database? Which copy of the jar is needed depends on the identity
    that can reach the db — /data/local/tmp/dbq.jar as `shell` for external storage, the sandbox
    copy under run-as for the private dir — so availability is checked per copy, the same split
    _jar_path() already makes. Requiring both regardless of db sent every read to the host snapshot
    (a full pull of the database, and stale) whenever either copy was missing."""
    if BACKEND == "snapshot":
        return False
    path = _jar_path(package, db)
    key = f"{package}:{path}"
    if key not in _jar_state:
        probe = f"test -f {path} && echo yes || echo no"
        out = (_as_shell(probe, check=False) if path.startswith("/data/local/tmp")
               else _in_sandbox(package, probe, check=False))
        _jar_state[key] = "yes" in str(out)
        if BACKEND == "jar" and not _jar_state[key]:
            raise RuntimeError(f"ANDROID_DB_BACKEND=jar but {path} is not deployed for "
                               f"{package}; call deploy_runner or run runner/deploy.sh")
    return _jar_state[key]


PERSIST = os.environ.get("ANDROID_DB_PERSIST", "1") != "0"
_servers: dict[tuple[str, str, str, str], "_Server"] = {}
_servers_lock = threading.RLock()          # guards the dict only
_server_locks: dict[tuple[str, str, str, str], threading.RLock] = {}   # guards one runner's startup


class _Server:
    """One long-lived `app_process … dbq.Main --serve` per device, package and identity,
    driven over `adb shell -T` with one base64 request line in and one JSON line out."""

    def __init__(self, identity: str, package: str, serial: str):
        self.identity = identity
        self._lock = threading.RLock()
        self._closed = False
        base = [ADB, "-s", serial, "shell", "-T"]
        if identity == "run-as":
            inner = "CLASSPATH=code_cache/dbq.jar exec app_process / dbq.Main --serve 2>/dev/null"
            cmd = base + [f"run-as {package} sh -c {shlex.quote(inner)}"]
        elif identity == "root":
            inner = f"cd /data/data/{package} && CLASSPATH=/data/local/tmp/dbq.jar exec app_process / dbq.Main --serve 2>/dev/null"
            cmd = base + [f"sh -c {shlex.quote(inner)}"]
        else:
            inner = "CLASSPATH=/data/local/tmp/dbq.jar exec app_process / dbq.Main --serve 2>/dev/null"
            cmd = base + [f"sh -c {shlex.quote(inner)}"]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            pong = self.request({"mode": "ping"}, timeout=60)
            if not pong.get("serve"):
                raise RuntimeError(f"runner does not support --serve: {pong}")
        except Exception:
            self.close()
            raise

    def request(self, req: dict[str, Any], timeout: float = 120) -> dict[str, Any]:
        # A request owns the stream until its complete response has been consumed.
        with self._lock:
            if self._closed:
                raise RuntimeError("persistent runner is closed")
            try:
                return self._request(req, timeout)
            except Exception:
                self.close()
                raise

    def _request(self, req: dict[str, Any], timeout: float) -> dict[str, Any]:
        import select
        line = base64.b64encode(json.dumps(req).encode()) + b"\n"
        try:
            self.proc.stdin.write(line)
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise RuntimeError(f"persistent runner ({self.identity}) is gone: {e}")
        buf = bytearray()
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.close()
                raise RuntimeError(f"persistent runner ({self.identity}) timed out")
            ready, _, _ = select.select([self.proc.stdout], [], [], min(remaining, 5))
            if not ready:
                if self.proc.poll() is not None:
                    raise RuntimeError(f"persistent runner ({self.identity}) exited")
                continue
            chunk = os.read(self.proc.stdout.fileno(), 1 << 20)
            if not chunk:
                self.close()
                raise RuntimeError(f"persistent runner ({self.identity}) closed its output")
            buf += chunk
            if buf.endswith(b"\n") or b"\n" in buf:
                response = json.loads(buf.decode("utf-8"))
                if not isinstance(response, dict):
                    raise RuntimeError("runner returned a non-object response")
                return response

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                if self.proc.poll() is None:
                    self.proc.kill()
                self.proc.wait(timeout=5)
            finally:
                for stream in (self.proc.stdin, self.proc.stdout):
                    if stream:
                        stream.close()


def _get_server(ident: str, package: str, serial: str) -> tuple["_Server", tuple[str, str, str, str]]:
    """The live runner for this identity, starting it if needed.

    Startup costs about a second of ART boot, so it happens under a per-identity lock rather than
    the global one: reaching a private database and an external one can start both in parallel."""
    key = (ADB, serial, package, ident)
    with _servers_lock:
        srv = _servers.get(key)
        if srv is not None and srv.proc.poll() is None:
            return srv, key
        lock = _server_locks.setdefault(key, threading.RLock())
    with lock:
        with _servers_lock:
            srv = _servers.get(key)
            if srv is not None and srv.proc.poll() is None:
                return srv, key
            if srv is not None:
                _servers.pop(key, None)
        if srv is not None:
            try:
                srv.close()
            except (OSError, subprocess.TimeoutExpired):
                pass
        srv = _Server(ident, package, serial)          # slow: deliberately outside _servers_lock
        with _servers_lock:
            _servers[key] = srv
        return srv, key


def _prewarm(package: str) -> None:
    """Start the runners this package needs in the background. Fire and forget: whoever queries
    first either finds one ready or waits on the same per-identity lock, never starting a second."""
    if not PERSIST:
        return
    serial = _serial()
    if serial == "unknown-device":
        return
    idents = ("root",) if RUNNER == "root" else ("run-as", "shell")

    def start(ident: str) -> None:
        try:
            _get_server(ident, package, serial)
        except Exception as e:
            if os.environ.get("ANDROID_DB_DEBUG"):
                print(f"[prewarm {ident}] {e}", file=sys.stderr)

    for i in idents:
        threading.Thread(target=start, args=(i,), daemon=True).start()


def _shutdown_runners() -> None:
    with _servers_lock:
        servers = list(_servers.values())
        _servers.clear()
        # _server_locks is deliberately NOT cleared: _get_server relies on every thread receiving
        # the same lock object per key. Dropping them mid-flight lets two threads start two
        # runners for one key, and the loser is never closed.
    for srv in servers:
        try:
            srv.close()
        except (OSError, subprocess.TimeoutExpired):
            pass


atexit.register(_shutdown_runners)


def _identity(package: str, db: str) -> str:
    if RUNNER == "root":
        return "root"
    return "shell" if _is_external(db) else "run-as"


def _device_sql(package: str, db: str, sql: str, mode: str = "read", limit: int = 200,
                blobs: str = "summary") -> dict[str, Any]:
    """Execute SQL on the device through the dbq runner: via a persistent --serve process when
    possible (one ART start-up per session instead of per query), else one app_process per call.
    blobs="base64" returns BLOB cells as {"$b64": ...}."""
    req = {"db": _db_path(package, db), "sql": sql, "mode": mode, "limit": limit, "blobs": blobs}
    ident = _identity(package, db)
    res: dict[str, Any] | None = None
    if PERSIST:
        srv = None
        try:
            serial = _serial()
            if serial == "unknown-device":
                raise RuntimeError("No Android device selected")
            srv, key = _get_server(ident, package, serial)
        except Exception as e:  # Startup/ping failed: no SQL has been sent, so fallback is safe.
            srv = None
            if os.environ.get("ANDROID_DB_DEBUG"):
                print(f"[persist startup fallback] {e}", file=sys.stderr)
        if srv is not None:
            try:
                res = srv.request(req)
            except Exception as e:
                with _servers_lock:
                    if _servers.get(key) is srv:
                        _servers.pop(key, None)
                try:
                    srv.close()
                except (OSError, subprocess.TimeoutExpired):
                    pass
                if mode == "write":
                    raise RuntimeError("Write outcome is unknown: the runner lost its response. "
                                       "The statement was not retried; query the database to verify "
                                       "its effect before issuing another write.") from e
                if os.environ.get("ANDROID_DB_DEBUG"):
                    print(f"[persist read fallback] {e}", file=sys.stderr)
    if res is None:
        b64 = base64.b64encode(json.dumps(req).encode()).decode()
        cmd = f"CLASSPATH={_jar_path(package, db)} app_process / dbq.Main {b64} 2>/dev/null"
        out = str(_exec_for(package, db, cmd, check=False))
        start, end = out.find("{"), out.rfind("}")
        if start < 0 or end < 0:
            raise RuntimeError(f"dbq runner produced no JSON (is the jar deployed? SELinux denial?): {out[:400]!r}")
        res = json.loads(out[start:end + 1])
    if "error" in res:
        raise RuntimeError(f"device SQL error: {res['error']}")
    res["backend"] = f"jar ({ident})"
    return res


# --------------------------------------------------------------------------- snapshot backend


_SNAPSHOT_TTL = float(os.environ.get("SNAPSHOT_TTL", "5"))


def _snapshot(package: str, db: str, refresh: bool = True, require_consistent: bool = False,
              force: bool = False) -> dict[str, Any]:
    package, db = _pkg(package), _safe_db_name(db)
    src = _db_path(package, db)
    local_dir = _pkg_cache(package)
    local = local_dir / _cache_name(db)
    meta = local_dir / f"{_cache_name(db)}.meta.json"

    if refresh and not force and local.exists() and meta.exists():
        # One logical operation issues many reads — _has_table per database, _pool_counts per 60
        # tables, title lookups per 200 keys. On the snapshot backend each of those used to re-pull
        # the entire database over adb (gigabytes for one index refresh) and, worse, read each
        # batch from a different point in time. Within SNAPSHOT_TTL they now share one pull.
        age = time.time() - local.stat().st_mtime
        if age < _SNAPSHOT_TTL:
            try:
                info = json.loads(meta.read_text())
            except (OSError, ValueError):
                info = None
            if info and (not require_consistent or info.get("consistent")):
                return {**info, "reused": True, "age_s": round(age, 2)}
    if not refresh and local.exists():
        info = json.loads(meta.read_text()) if meta.exists() else {"path": str(local)}
        info.setdefault("consistent", info.get("method") == "on-device sqlite3 .backup")
        if not info["consistent"]:
            info.setdefault("warning", "Cached snapshot has no consistency guarantee; use require_consistent=True for SQLite .backup.")
        if not require_consistent or info["consistent"]:
            return info

    use_backup = _device_has_sqlite3(package) and not _is_external(db)
    if require_consistent and not use_backup:
        raise RuntimeError("A consistent snapshot requires on-device sqlite3 .backup for a private "
                           "database; raw database/WAL copies cannot guarantee consistency.")

    # A failed replacement must never inherit the previous copy's consistency metadata.
    meta.unlink(missing_ok=True)
    for stale in (local, local.with_name(local.name + "-wal"), local.with_name(local.name + "-shm")):
        if stale.exists():
            stale.unlink()

    info: dict[str, Any] = {"package": package, "db": db, "path": str(local), "taken_at": time.time()}

    if use_backup:
        # Consistent snapshot via the online backup API, written inside the app sandbox.
        tmp = f"cache/__mcp_snapshot_{_cache_name(db)}"
        _in_sandbox(package, f"rm -f {tmp}; sqlite3 {shlex.quote(src)} '.backup {tmp}'")
        local.write_bytes(_in_sandbox(package, f"cat {tmp} && rm -f {tmp}", binary=True))
        info["method"] = "on-device sqlite3 .backup"
        info["consistent"] = True
    else:
        # Fallback: raw copy of db + WAL + shm. SQLite replays the WAL on open.
        local.write_bytes(_exec_for(package, db, f"cat {shlex.quote(src)}", binary=True))
        for suffix in ("-wal", "-shm"):
            raw = _exec_for(package, db, f"cat {shlex.quote(src + suffix)} 2>/dev/null", binary=True, check=False)
            if raw:
                local.with_name(local.name + suffix).write_bytes(raw)
        info["method"] = "raw copy of db + -wal/-shm"
        info["consistent"] = False
        info["warning"] = ("Best-effort snapshot: database and WAL files were copied separately. "
                           "Concurrent app writes can produce an inconsistent copy.")

    info["bytes"] = local.stat().st_size
    meta.write_text(json.dumps(info))
    return info


def _connect(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path)  # plain open: mode=ro fails on a WAL copy without -wal; query_only guards writes
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only = 1")
    return con


def _json_safe(v: Any, blobs: str = "summary") -> Any:
    if isinstance(v, bytes):
        return {"$b64": base64.b64encode(v).decode()} if blobs == "base64" else f"<blob {len(v)} bytes>"
    return v


def _cell_bytes(v: Any) -> bytes | None:
    """Undo the {"$b64": ...} wrapping of a BLOB cell (either backend)."""
    if isinstance(v, dict) and "$b64" in v:
        return base64.b64decode(v["$b64"])
    if isinstance(v, (bytes, bytearray)):
        return bytes(v)
    return None


def _rows(cur: sqlite3.Cursor, limit: int, blobs: str = "summary") -> dict[str, Any]:
    cols = [d[0] for d in cur.description] if cur.description else []
    rows = cur.fetchmany(limit + 1)
    truncated = len(rows) > limit
    rows = rows[:limit]
    return {"columns": cols, "rows": [[_json_safe(v, blobs) for v in r] for r in rows],
            "row_count": len(rows), "truncated": truncated}


def _snapshot_sql(package: str, db: str, sql: str, limit: int, refresh: bool, blobs: str = "summary") -> dict[str, Any]:
    info = _snapshot(package, db, refresh)
    con = _connect(info["path"])
    try:
        res = _rows(con.execute(sql), limit, blobs)
    finally:
        con.close()
    res["backend"] = f"snapshot ({info.get('method')})"
    res["snapshot"] = info
    return res


# --------------------------------------------------------------------------- unified read


def _read(package: str | None, db: str, sql: str, limit: int = 200, refresh: bool = True,
          blobs: str = "summary") -> dict[str, Any]:
    _positive_limit(limit)
    package = _pkg(package)
    if _jar_available(package, db):
        return _device_sql(package, db, sql, "read", limit, blobs)
    return _snapshot_sql(package, db, sql, limit, refresh, blobs)


def _positive_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")
    return limit


# --------------------------------------------------------------------------- tools


@tool()
def devices() -> str:
    """List connected Android devices/emulators (adb devices -l)."""
    return _adb("devices", "-l")


_LIST_TTL = int(os.environ.get("DB_LIST_TTL", "600"))


def _disk_cache(package: str, name: str) -> Path:
    return _pkg_cache(package) / name


def _cached_json(path: Path, ttl: int) -> Any:
    try:
        if time.time() - path.stat().st_mtime < ttl:
            return json.loads(path.read_text())
    except (OSError, ValueError):
        pass
    return None


@tool()
def list_databases(package: str | None = None, refresh: bool = False) -> list[str]:
    """List every SQLite database the app has: in its private databases/ and files/ dirs and in
    its external app-specific storage (/storage/emulated/0/Android/data/<pkg>/files/**), e.g.
    per-tenant folders. Candidates are files inside any databases/ dir plus *.db / *.sqlite*
    elsewhere, confirmed by the 'SQLite format 3' header. Cached for DB_LIST_TTL seconds
    (default 600) — pass refresh=True after logging into another tenant.
    Returned values can be passed as `db` to the other tools as-is."""
    package = _pkg(package)
    cache = _disk_cache(package, "databases.json")
    if not refresh:
        hit = _cached_json(cache, _LIST_TTL)
        if hit is not None:
            return hit
    scan = ("find {dirs} -type f -size +0 \\( -path '*/databases/*' -o -name '*.db' -o -name '*.sqlite' "
            "-o -name '*.sqlite3' -o -name '*.sqlite-*' \\) 2>/dev/null | while read -r f; do "
            "case \"$f\" in *-journal|*-wal|*-shm|*-ver) continue;; esac; "
            "[ \"$(head -c 15 \"$f\" 2>/dev/null)\" = 'SQLite format 3' ] && echo \"$f\"; done")
    out = str(_in_sandbox(package, scan.format(dirs="./databases ./files"), check=False))
    if RUNNER == "root":
        out += str(_in_sandbox(package, scan.format(dirs=shlex.quote(EXTERNAL_FILES_ROOT.format(pkg=package))), check=False))
    else:
        out += str(_as_shell(scan.format(dirs=shlex.quote(EXTERNAL_FILES.format(pkg=package))), check=False))
    found = []
    for line in out.splitlines():
        f = line.strip()
        if not f:
            continue
        if f.startswith("./"):
            f = f[2:]
        if f.startswith("databases/"):
            f = f[len("databases/"):]                       # bare name form
        elif RUNNER == "root" and f.startswith("/data/media/0/"):
            f = "/storage/emulated/0/" + f[len("/data/media/0/"):]   # canonical external form
        found.append(f)
    cache.write_text(json.dumps(found))
    return found




@tool()
def set_device(serial: str | None = None, package: str | None = None) -> dict:
    """Point this session at a different emulator or phone. Closes the runners, forgets every
    in-process cache, and reports what the new device looks like. Pass serial=None to go back to
    automatic selection. Caches on disk are already per device, so nothing is thrown away."""
    global SERIAL
    if serial and not re.fullmatch(r"[A-Za-z0-9_.:-]+", serial):
        raise ValueError(f"Suspicious serial: {serial!r}")
    attached = str(_adb("devices", check=False))
    if serial and serial not in attached:
        raise ValueError(f"{serial} is not attached. adb devices says:\n{attached.strip()}")
    _shutdown_runners()
    SERIAL = serial or None
    _device_ident.clear()
    _pool_cache.clear()
    _ref_cache.clear()
    _title_cache.clear()
    _VEC_CACHE.clear()
    _jar_state.clear()
    _detected.pop("package", None)
    return {"serial": _serial(), "package": _pkg(package) if (package or DEFAULT_PKG) else None,
            "cache_dir": str(CACHE / _device_tag()), "attached": attached.strip().splitlines()[1:]}




@tool()
def deploy_runner(package: str | None = None) -> dict:
    """Push the dbq.jar runner (built with runner/build.sh) into the app sandbox so SQL can run
    on the device via app_process. Safe to call again after reinstalling the app."""
    package = _pkg(package)
    if not JAR.exists():
        raise RuntimeError(f"{JAR} not found — build it with runner/build.sh (needs the Android SDK)")
    _shutdown_runners()
    _adb("push", str(JAR), "/data/local/tmp/dbq.jar")
    if RUNNER == "root":
        _adb("root", check=False)
    else:
        # /data/local/tmp is shell:shell 0771, so the app uid can't read it: stream it across.
        _adb("shell", f"cat /data/local/tmp/dbq.jar | run-as {package} sh -c "
                      f"'mkdir -p code_cache && rm -f code_cache/dbq.jar; cat > code_cache/dbq.jar && chmod 444 code_cache/dbq.jar'")
    _forget_jar(package)
    ok = _jar_available(package)
    dbs = list_databases(package) if ok else []
    smoke = _device_sql(package, dbs[0], "SELECT sqlite_version()", limit=1) if dbs else None
    return {"deployed": ok, "runner": RUNNER, "smoke_test": smoke}


@tool()
def snapshot(db: str, package: str | None = None, require_consistent: bool = False) -> dict:
    """Pull a fresh database copy. Reports consistent=True for SQLite .backup, otherwise a
    best-effort raw DB/WAL copy with a warning. require_consistent=True refuses raw copying.
    Always pulls, ignoring the SNAPSHOT_TTL coalescing window that internal reads use."""
    return _snapshot(package, db, refresh=True, require_consistent=require_consistent, force=True)


@tool()
def schema(db: str, package: str | None = None, refresh: bool = True, stats: bool = True,
           tables: list[str] | None = None) -> dict:
    """Full schema of a database: tables with CREATE statements, columns (with fill rate and
    distinct count per column when stats=True, so always-NULL shadow columns are obvious),
    foreign keys, indexes, row counts. osapiens pool tables (BusinessObjectEntry#app#pool) are
    annotated with their pool name; their #value BLOB is decodable with entries()/decode().
    `tables` restricts the output (table names or pool names) — use it on big databases."""
    package = _pkg(package)
    rd = lambda sql, lim=200000: _read(package, db, sql, limit=lim, refresh=False)["rows"]
    master = _read(package, db, "SELECT type, name, tbl_name, sql FROM sqlite_master "
                                "WHERE sql IS NOT NULL ORDER BY type, name", limit=5000, refresh=refresh)
    out: dict[str, Any] = {"database": db, "backend": master["backend"], "tables": [], "views": [], "indexes": []}
    pool_names = _pool_map(package, db) if any(r[1].startswith(ENTRY_PREFIX) for r in master["rows"]) else {}
    want: set[str] | None = None
    if tables:
        by_pool = {v.lower(): f"{ENTRY_PREFIX}{k[0]}#{k[1]}" for k, v in pool_names.items()}
        want = {by_pool.get(t.lower(), t) for t in tables}
    names = [r[1] for r in master["rows"] if r[0] == "table" and (want is None or r[1] in want)]
    if not names:
        return out
    in_list = "(" + ", ".join(_sq(n) for n in names) + ")"
    # 1 query: every column of every table (pragma_table_info as a table-valued function)
    cols_by: dict[str, list[dict[str, Any]]] = {n: [] for n in names}
    for tname, cid, cname, ctype, notnull, dflt, pk in rd(
            "SELECT m.name, p.cid, p.name, p.type, p.\"notnull\", p.dflt_value, p.pk FROM sqlite_master m "
            f"JOIN pragma_table_info(m.name) p WHERE m.type='table' AND m.name IN {in_list} ORDER BY m.name, p.cid"):
        cols_by[tname].append({"name": cname, "type": ctype, "notnull": bool(notnull), "pk": bool(pk), "default": dflt})
    # 1 query: foreign keys
    fks_by: dict[str, list[dict[str, Any]]] = {n: [] for n in names}
    for tname, to_table, frm, to in rd(
            "SELECT m.name, f.\"table\", f.\"from\", f.\"to\" FROM sqlite_master m "
            f"JOIN pragma_foreign_key_list(m.name) f WHERE m.type='table' AND m.name IN {in_list}"):
        fks_by[tname].append({"from": frm, "to_table": to_table, "to": to})
    # row counts, batched UNION ALL (100 tables per query)
    counts: dict[str, int] = {}
    for i in range(0, len(names), 60):
        q = " UNION ALL ".join(f"SELECT {_sq(n)}, COUNT(*) FROM {_q(n)}" for n in names[i:i + 60])
        counts.update({r[0]: r[1] for r in rd(q)})
    # fill/distinct per column for non-empty tables, batched (~300 selects per query)
    fill: dict[tuple[str, str], tuple[int, int]] = {}
    if stats:
        selects = [f"SELECT {_sq(n)}, {_sq(c['name'])}, COUNT({_q(c['name'])}), COUNT(DISTINCT {_q(c['name'])}) FROM {_q(n)}"
                   for n in names if counts.get(n) for c in cols_by[n]]
        batch: list[str] = []
        size = 0
        for sel in selects + [None]:
            if batch and (sel is None or size + len(sel) > 24000):
                for t, c, filled, distinct in rd(" UNION ALL ".join(batch)):
                    fill[(t, c)] = (filled, distinct)
                batch, size = [], 0
            if sel is not None:
                batch.append(sel)
                size += len(sel) + 11
    for typ, name, tbl, sql in master["rows"]:
        if typ == "table" and name in cols_by:
            count = counts.get(name, 0)
            colinfo = cols_by[name]
            for c in colinfo:
                if (name, c["name"]) in fill:
                    c["filled"] = f"{fill[(name, c['name'])][0]}/{count}"
                    c["distinct"] = fill[(name, c["name"])][1]
            entry = {"name": name, "sql": sql, "row_count": count, "columns": colinfo, "foreign_keys": fks_by[name]}
            if name.startswith(ENTRY_PREFIX):
                app_id, pool_id = _parse_entry_table(name)
                entry["pool"] = pool_names.get((app_id, pool_id))
                entry["applicationId"], entry["poolId"] = app_id, pool_id
                entry["note"] = ("#value is an osapiens-serialized object; named columns are promoted scalars "
                                 "and may be empty. Use entries(pool=...) / decode() to read the real content.")
            out["tables"].append(entry)
        elif typ == "view" and want is None:
            out["views"].append({"name": name, "sql": sql})
        elif typ == "index" and (want is None or tbl in want):
            out["indexes"].append({"name": name, "table": tbl, "sql": sql})
    return out


@tool()
def query(sql: str, db: str, package: str | None = None, limit: int = 200, refresh: bool = True) -> dict:
    """Run a read-only SQL query (SELECT / PRAGMA / EXPLAIN) against the app database — on the
    device via the jar runner when deployed, otherwise on a fresh host snapshot.
    Results are JSON rows; large results are truncated to `limit`. Columns whose name looks like a
    credential come back as «redacted» (see REDACT_FIELDS); the reply lists them."""
    res = _read(package, db, sql, limit, refresh)
    res["rows"], hidden = _redact_rows(res.get("columns", []), res.get("rows", []))
    if hidden:
        res["redacted_columns"] = hidden
    return res






WRITE_MODE = os.environ.get("ANDROID_DB_WRITE", "on").lower()     # on | dry-run | off
WRITE_ALLOW = [t.strip() for t in os.environ.get("ANDROID_DB_WRITE_ALLOW", "").split(",") if t.strip()]
_WRITE_TARGET = re.compile(r"(?is)\b(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|UPDATE(?:\s+OR\s+\w+)?|DELETE\s+FROM)\s+"
                           r"[\"'`\[]?([A-Za-z0-9_#@.$-]+)")


def _write_targets(sql: str) -> list[str]:
    return list(dict.fromkeys(_WRITE_TARGET.findall(sql or "")))


def _check_write(sql: str, dry_run: bool) -> bool:
    """Decide whether this statement may actually commit. Returns the effective dry_run flag."""
    if WRITE_MODE == "off":
        raise RuntimeError("Writes are disabled (ANDROID_DB_WRITE=off). Unset it, or use "
                           "dry_run=True to see what the statement would change.")
    if WRITE_ALLOW:
        targets = _write_targets(sql)
        if not targets:
            raise RuntimeError("ANDROID_DB_WRITE_ALLOW is set but no target table could be read "
                               "from this statement; refusing rather than guessing.")
        blocked = [t for t in targets if t not in WRITE_ALLOW]
        if blocked:
            raise RuntimeError(f"{', '.join(blocked)} is not in ANDROID_DB_WRITE_ALLOW "
                               f"({', '.join(WRITE_ALLOW)}).")
    return dry_run or WRITE_MODE == "dry-run"


@tool()
def execute(sql: str, db: str, package: str | None = None, dry_run: bool = False) -> dict:
    """Run a WRITE statement (INSERT/UPDATE/DELETE/DDL) on the live device database, so the
    running app sees the change. Use with care — this mutates live app data.

    dry_run=True runs the statement inside a transaction that is rolled back and reports how many
    rows it *would* have changed: the safe way to check a WHERE clause before committing.
    ANDROID_DB_WRITE=off refuses writes entirely, =dry-run forces every write to be a rehearsal,
    and ANDROID_DB_WRITE_ALLOW=table1,table2 limits which tables may be written."""
    package, db = _pkg(package), _safe_db_name(db)
    effective_dry = _check_write(sql, dry_run)
    if _jar_available(package, db):
        res = _device_sql(package, db, sql, "dryrun" if effective_dry else "write")
        res["dry_run"] = effective_dry
        res["targets"] = _write_targets(sql)
        if effective_dry:
            res["note"] = ("Rolled back: nothing was written. `changes` is what the statement "
                           "would have affected.")
        return res
    if effective_dry:
        raise RuntimeError("dry_run needs the jar runner (the sqlite3 fallback cannot roll back "
                           "for us); run deploy_runner first.")
    if not _device_has_sqlite3(package):
        raise RuntimeError("No jar runner deployed and no sqlite3 binary on the device; "
                           "run deploy_runner (or use an emulator/userdebug build).")
    out = _in_sandbox(package, f"sqlite3 {shlex.quote(_db_path(package, db))} {shlex.quote(sql)}")
    return {"ok": True, "backend": "sqlite3", "output": str(out)}


# ------------------------------------------------------------------ osapiens business objects
#
# Entry tables are named BusinessObjectEntry#<applicationId>#<poolId>; each row has "#key",
# "#thingId", "#value" (the object, in SerializationUtil's binary format), promoted scalar
# columns declared in BusinessObjectNamedField, "#immutable", "#lastChange". Pool names live in
# BusinessObjectPool(poolId, poolName, applicationId) of the same database.

ENTRY_PREFIX = "BusinessObjectEntry#"
LOCALE = os.environ.get("LOCALE", "en")
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import bo_codec  # noqa: E402
except ImportError:  # pragma: no cover
    bo_codec = None

_pool_cache: dict[tuple[str, str], list[dict[str, Any]]] = {}


def _need_codec() -> None:
    if bo_codec is None:
        raise RuntimeError("bo_codec.py must sit next to android_db_mcp.py")


def _parse_entry_table(name: str) -> tuple[int | None, int | None]:
    m = re.fullmatch(re.escape(ENTRY_PREFIX) + r"(\d+)#(\d+)", name)
    return (int(m.group(1)), int(m.group(2))) if m else (None, None)


def _has_table(package: str, db: str, table: str) -> bool:
    r = _read(package, db, f"SELECT 1 FROM sqlite_master WHERE type='table' AND name={_sq(table)}", limit=1)
    return bool(r["rows"])


def _sq(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _pools_in(package: str, db: str, refresh: bool = False) -> list[dict[str, Any]]:
    key = (package, db)
    if not refresh:
        hit = _pool_cache.get(key)      # get, not "in" + index: another thread may clear between them
        if hit is not None:
            return hit
    import hashlib
    cache = _disk_cache(package, f"pools_{hashlib.sha1(db.encode()).hexdigest()[:10]}.json")
    if not refresh:
        hit = _cached_json(cache, _LIST_TTL)
        if hit is not None:
            _pool_cache[key] = hit
            return hit
    out: list[dict[str, Any]] = []
    if _has_table(package, db, "BusinessObjectPool"):
        tables = {r[0] for r in _read(package, db, "SELECT name FROM sqlite_master WHERE type='table'", limit=100000)["rows"]}
        for pool_id, name, app_id in _read(package, db, "SELECT poolId, poolName, applicationId FROM BusinessObjectPool", limit=100000)["rows"]:
            table = f"{ENTRY_PREFIX}{app_id}#{pool_id}"
            out.append({"name": name, "applicationId": app_id, "poolId": pool_id, "table": table,
                        "db": db, "exists": table in tables})
    _pool_cache[key] = out
    cache.write_text(json.dumps(out))
    return out


def _pool_counts(package: str, db: str, tables: list[str]) -> dict[str, int]:
    """Row count per entry table, batched 60 tables to a query (one device round-trip each)."""
    counts: dict[str, int] = {}
    for i in range(0, len(tables), 60):
        q = " UNION ALL ".join(f"SELECT {_sq(t)}, COUNT(*) FROM {_q(t)}" for t in tables[i:i + 60])
        counts.update({r[0]: r[1] for r in _read(package, db, q, limit=100000)["rows"]})
    return counts


def _pool_map(package: str, db: str) -> dict[tuple[int, int], str]:
    return {(p["applicationId"], p["poolId"]): p["name"] for p in _pools_in(package, db)}


def _bo_databases(package: str, refresh: bool = False) -> list[str]:
    cache = _disk_cache(package, "bo_databases.json")
    if not refresh:
        hit = _cached_json(cache, _LIST_TTL)
        if hit is not None:
            return hit
    out = [d for d in list_databases(package, refresh=refresh)
           if _has_table(package, d, "BusinessObjectPool")]
    cache.write_text(json.dumps(out))
    return out


def _resolve_pool(package: str, pool: str, db: str | None = None, app: int | None = None) -> dict[str, Any]:
    """Pool name (case-insensitive) or entry table name → {db, table, name, applicationId, poolId}."""
    if not pool:
        raise ValueError("A pool name is required (a pool with no name in BusinessObjectPool "
                         "can only be addressed by its BusinessObjectEntry#app#pool table name)")
    if pool.startswith(ENTRY_PREFIX):
        app_id, pool_id = _parse_entry_table(pool)
        if db is None:
            raise ValueError("db is required when addressing a pool by table name")
        return {"db": db, "table": pool, "name": _pool_map(package, db).get((app_id, pool_id)),
                "applicationId": app_id, "poolId": pool_id}
    dbs = [db] if db else _bo_databases(package)
    hits = [p for d in dbs for p in _pools_in(package, d)
            if (p["name"] or "").lower() == pool.lower() and (app is None or p["applicationId"] == app)]
    if not hits:
        known = sorted({p["name"] for d in dbs for p in _pools_in(package, d) if p["name"]})
        raise ValueError(f"No pool named {pool!r}. Known pools: {', '.join(known) or '(none)'}")
    if len(hits) > 1:
        raise ValueError("Ambiguous pool — pass app= or db=: " + "; ".join(
            f"{h['name']} app={h['applicationId']} pool={h['poolId']} in {h['db']}" for h in hits))
    return hits[0]


def _parse_named_fields(raw: bytes | None) -> list[Any]:
    if not raw:
        return []
    try:
        specs = bo_codec.decode(raw, nested=False) or []   # list of byte[] (each: name, path, dataType)
        return [bo_codec.named_field(base64.b64decode(item["$b64"])) if isinstance(item, dict) and "$b64" in item
                else {"$unparsed": item} for item in specs]
    except Exception as e:  # keep schema usable even if the spec format drifts
        return [{"$undecoded": str(e)}]


def _all_named_fields(package: str, db: str) -> dict[tuple[int, int], list[Any]]:
    """(applicationId, poolId) → decoded named-field specs, one query for the whole database."""
    if not _has_table(package, db, "BusinessObjectNamedField"):
        return {}
    r = _read(package, db, "SELECT applicationId, poolId, data FROM BusinessObjectNamedField", limit=100000, blobs="base64")
    return {(app, pool): _parse_named_fields(_cell_bytes(data)) for app, pool, data in r["rows"]}


def _named_fields(package: str, db: str, app_id: int, pool_id: int) -> list[Any]:
    return _all_named_fields(package, db).get((app_id, pool_id), [])


@tool()
def pools(db: str | None = None, package: str | None = None, refresh: bool = False,
          name: str | None = None, fields: bool = False) -> list[dict]:
    """osapiens business-object pools: pool name → entry table (BusinessObjectEntry#app#pool),
    applicationId, poolId and row count. Scans every database with a BusinessObjectPool table
    unless `db` is given. Use the `name` with entries() and search(pool=...).

    `name` keeps only the pools whose name contains it, case-insensitively — pass it whenever you
    are after one pool, because a full tenant listing is hundreds of pools and the whole reply is
    pasted into the caller's context.

    `fields=True` adds `named_fields`, each pool's promoted columns with their JSON paths. It is
    off by default because it is the bulk of the reply. In a field's `path`, 'default' means "each
    element of this list" (Codes.default.Title.default.Text = Codes[*].Title[*].Text); such
    list-derived columns are usually left empty by the app, so read the decoded #value instead."""
    package = _pkg(package)
    out = []
    if refresh:
        list_databases(package, refresh=True)
        _disk_cache(package, "bo_databases.json").unlink(missing_ok=True)
    needle = name.lower() if name else None
    for d in ([db] if db else _bo_databases(package)):
        ps = [dict(p) for p in _pools_in(package, d, refresh)
              if needle is None or needle in (p["name"] or "").lower()]
        counts = _pool_counts(package, d, [p["table"] for p in ps if p["exists"]])
        named = _all_named_fields(package, d) if fields else {}
        for p in ps:
            p["rows"] = counts.get(p["table"], 0)
            if fields:
                p["named_fields"] = named.get((p["applicationId"], p["poolId"]), [])
            out.append(p)
    return out


# ------------------------------------------------------------------ redaction
#
# Everything these tools return is pasted into a model's context, and these databases hold real
# credentials — MobileUser keeps a live session token in the clear. Fields whose *name* says
# "secret" are replaced by default; the reply always says what was hidden, so a redacted answer is
# never mistaken for missing data. Set REDACT_FIELDS="" to turn it off.

REDACT_FIELDS = os.environ.get("REDACT_FIELDS")          # optional regex override
_REDACT = re.compile(REDACT_FIELDS) if REDACT_FIELDS else None
REDACTION_ON = os.environ.get("REDACT", "on").lower() != "off"
REDACTED = "\u00abredacted\u00bb"

# Matching on a substring redacts far too much — "Passes", "Bypass" and "CompassBearing" all
# contain "pass". Field names are split into words (camelCase, snake_case, digits) and each word is
# compared whole, with adjacent pairs joined so api_key and apiKey are caught too.
_NAME_WORDS = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
SECRET_WORDS = {"password", "passwd", "passphrase", "secret", "token", "apikey", "credential",
                "credentials", "session", "sessionid", "jwt", "bearer", "signature", "privatekey",
                "auth", "accesskey", "refreshtoken", "clientsecret"}


def _redact_config() -> tuple:
    """What the cache key must include, so patching the settings is picked up immediately."""
    return (_REDACT.pattern if _REDACT is not None else None, REDACTION_ON)


@functools.lru_cache(maxsize=8192)
def _is_secret_cached(name: str, cfg: tuple) -> bool:
    """Memoised: field names repeat across every object of a pool, and this walks the name.
    `cfg` is part of the key so a changed REDACT setting is never served from the cache."""
    pattern, on = cfg
    if pattern is not None:
        return bool(re.search(pattern, name))
    if not on:
        return False
    words = [w.lower() for w in _NAME_WORDS.findall(name)]
    if any(w in SECRET_WORDS for w in words):
        return True
    return any(words[i] + words[i + 1] in SECRET_WORDS for i in range(len(words) - 1))


def _is_secret(name: Any) -> bool:
    if not isinstance(name, str) or not name:
        return False
    return _is_secret_cached(name, _redact_config())


def _redacting() -> bool:
    return _REDACT is not None or REDACTION_ON


def _redact(obj: Any, hits: set[str] | None = None, path: str = "") -> Any:
    """Copy a decoded object with secret-looking fields replaced. Structure is preserved so a
    caller still sees that the field exists."""
    if not _redacting():
        return obj
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            here = f"{path}.{k}" if path else str(k)
            if _is_secret(k) and not isinstance(v, (dict, list)):
                out[k] = REDACTED
                if hits is not None:
                    hits.add(here)
            else:
                out[k] = _redact(v, hits, here)
        return out
    if isinstance(obj, list):
        return [_redact(v, hits, f"{path}[{i}]") for i, v in enumerate(obj)]
    return obj


def _redact_rows(columns: list[str], rows: list[list[Any]]) -> tuple[list[list[Any]], list[str]]:
    if not _redacting():
        return rows, []
    secret = [i for i, c in enumerate(columns) if _is_secret(c)]
    if not secret:
        return rows, []
    out = []
    for r in rows:
        r = list(r)
        for i in secret:
            if r[i] not in (None, ""):
                r[i] = REDACTED
        out.append(r)
    return out, [columns[i] for i in secret]


def _decode_cell(v: Any) -> Any:
    raw = _cell_bytes(v)
    if raw is None:
        return v
    try:
        return bo_codec.decode(raw)
    except Exception as e:
        return {"$undecoded": f"{e}", "$bytes": len(raw)}


_PATH_SEG = re.compile(r"([^.\[\]]+)((?:\[(?:\*|\d+)\])*)")
_PATH_IDX = re.compile(r"\[(\*|\d+)\]")


def _path_values(obj: Any, path: str) -> list[Any]:
    """Values at a field path inside a decoded object. '[*]' walks every element of a list, so
    'Codes[*].Title' collects the title of each code."""
    cur = [obj]
    for name, idx in _PATH_SEG.findall(path):
        cur = [c[name] for c in cur if isinstance(c, dict) and name in c]
        for token in _PATH_IDX.findall(idx):
            step = []
            for c in cur:
                if isinstance(c, list):
                    if token == "*":
                        step.extend(c)
                    elif int(token) < len(c):
                        step.append(c[int(token)])
            cur = step
    return cur


def _readable(v: Any, locale: str) -> Any:
    t = bo_codec.resolve_locale(v, locale)
    return t if t is not None else v


def _project(value: Any, fields: list[str], locale: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for f in fields:
        vals = [_readable(v, locale) for v in _path_values(value, f)]
        out[f] = vals[0] if len(vals) == 1 else (vals or None)
    return out


def _cmp_num(v: Any, want: Any, op: str) -> bool:
    try:
        a, b = float(v), float(want)
    except (TypeError, ValueError):
        a, b = str(v), str(want)
    return {"gt": a > b, "lt": a < b, "ge": a >= b, "le": a <= b}[op]


def _match_filters(value: Any, filters: dict[str, Any], locale: str) -> bool:
    """Filters run on the decoded object, which is the only place most values exist. Supported:
    a bare value (equals), or {"op": eq|ne|contains|in|gt|lt|ge|le|exists|missing, "value": ...}."""
    for path, spec in filters.items():
        vals = [_readable(v, locale) for v in _path_values(value, path)]
        op, want = ("eq", spec)
        if isinstance(spec, dict) and "op" in spec:
            op, want = spec["op"], spec.get("value")
        if op == "exists":
            ok = any(v is not None and str(v) != "" for v in vals)
        elif op == "missing":
            ok = not any(v is not None and str(v) != "" for v in vals)
        elif not vals:
            ok = False
        elif op == "eq":
            ok = any(str(v) == str(want) for v in vals)
        elif op == "ne":
            ok = all(str(v) != str(want) for v in vals)
        elif op == "contains":
            ok = any(str(want).lower() in str(v).lower() for v in vals)
        elif op == "in":
            wanted = {str(x) for x in (want or [])}
            ok = any(str(v) in wanted for v in vals)
        elif op in ("gt", "lt", "ge", "le"):
            ok = any(_cmp_num(v, want, op) for v in vals)
        else:
            raise ValueError(f"Unknown filter op {op!r} on {path!r}")
        if not ok:
            return False
    return True


@tool()
def entries(pool: str, db: str | None = None, app: int | None = None, keys: list[str] | None = None,
            where: str | None = None, limit: int = 50, package: str | None = None,
            locale: str | None = None, raw: bool = False, after_rowid: int | None = None,
            fields: list[str] | None = None, filters: dict[str, Any] | None = None,
            scan: int = 500) -> dict:
    """Read business objects from a pool by NAME (e.g. 'CodesGroup'), with the #value BLOB decoded.

    `keys` selects exact #key values and `where` is SQL over the promoted columns / #lastChange —
    both run on the device. `fields=["Title", "Codes[*].Title"]` returns just those paths instead of
    the whole object, and `filters={"Status": "OPEN", "Title": {"op": "contains", "value": "pump"}}`
    keeps only matching objects. Nested values exist only inside the BLOB, so field filters run
    after decoding: at most `scan` rows are examined and the reply states how many were scanned and
    whether matches may remain. Narrow with `where` first when a promoted column can do the work.
    Ordered by rowid; pass next_after_rowid as after_rowid for the next page."""
    _need_codec()
    _positive_limit(limit)
    package = _pkg(package)
    loc = locale or LOCALE
    p = _resolve_pool(package, pool, db, app)
    post = bool(fields or filters)
    fetch = _positive_limit(scan) if post else limit
    conds = []
    if keys:
        conds.append('"#key" IN (' + ", ".join(_sq(str(k)) for k in keys) + ")")
    if where:
        conds.append(f"({where})")
    if after_rowid is not None:
        conds.append(f"rowid > {int(after_rowid)}")
    sql = (f'SELECT rowid, * FROM {_q(p["table"])}' + (" WHERE " + " AND ".join(conds) if conds else "")
           + f" ORDER BY rowid LIMIT {fetch + 1}")
    r = _read(package, p["db"], sql, limit=fetch, blobs="base64")
    cols = r["columns"]
    rows, scanned, stopped_early, last_rowid = [], 0, False, None
    redacted: set[str] = set()
    for row in r["rows"]:
        rec = dict(zip(cols, row))
        value = _decode_cell(rec.get("#value"))
        scanned += 1
        last_rowid = rec["rowid"]
        value = _redact(value, redacted)
        if filters and not _match_filters(value, filters, loc):
            continue
        item: dict[str, Any] = {"rowid": rec["rowid"], "key": rec.get("#key"), "thingId": rec.get("#thingId"),
                                "lastChange": rec.get("#lastChange"),
                                "columns": {k: v for k, v in rec.items() if not k.startswith("#") and k != "rowid"},
                                "title": bo_codec.display_title(value, loc)}
        if fields:
            item["fields"] = _project(value, fields, loc)
        if raw or not fields:
            item["value"] = value
        if raw:
            item["raw_b64"] = rec.get("#value", {}).get("$b64") if isinstance(rec.get("#value"), dict) else None
        rows.append(item)
        if len(rows) >= limit:
            stopped_early = scanned < len(r["rows"])
            break
    out = {"pool": p["name"], "table": p["table"], "db": p["db"], "applicationId": p["applicationId"],
           "poolId": p["poolId"], "backend": r.get("backend"), "row_count": len(rows),
           "truncated": r.get("truncated") or stopped_early,
           # the cursor is the last row *examined*, not the last one that matched — otherwise a
           # scan window with no matches has no cursor at all and the rest of the pool is
           # unreachable, and a window with matches re-scans rows already seen.
           "next_after_rowid": last_rowid if (r.get("truncated") or stopped_early) else None,
           "rows": rows}
    if redacted:
        out["redacted_fields"] = sorted(redacted)
    if post:
        out.update({"scanned": scanned, "scan_limit": fetch, "matched": len(rows),
                    "complete": not r.get("truncated") and not stopped_early,
                    "note": "fields/filters are evaluated after decoding each object; "
                            "`complete` is false when rows beyond the scan limit were not examined."})
    return out


@tool()
def decode(db: str, table: str, where: str | None = None, column: str = "#value", limit: int = 20,
           package: str | None = None, after_rowid: int | None = None) -> dict:
    """Decode any osapiens-serialized BLOB column (default "#value") of any table into JSON —
    e.g. Bundle.configs, BusinessObjectNamedField.data, or an entry table addressed directly.
    Returns rowid + decoded value per row. Pass next_after_rowid as after_rowid to continue in
    rowid order. Requires a rowid table; pages are separate live reads."""
    _need_codec()
    _positive_limit(limit)
    package = _pkg(package)
    cols = [r[0] for r in _read(package, db, f"SELECT name FROM pragma_table_info({_sq(table)})", limit=1000)["rows"]]
    if column not in cols:
        raise ValueError(f"{table} has no column {column!r}; columns: {cols}")
    conds = [f"({where})"] if where else []
    if after_rowid is not None:
        conds.append(f"rowid > {int(after_rowid)}")
    sql = (f"SELECT rowid, {_q(column)} FROM {_q(table)}" + (" WHERE " + " AND ".join(conds) if conds else "")
           + f" ORDER BY rowid LIMIT {limit + 1}")
    r = _read(package, db, sql, limit=limit, blobs="base64")
    hidden: set[str] = set()
    return {"table": table, "column": column, "backend": r.get("backend"), "truncated": r.get("truncated"),
            "next_after_rowid": r["rows"][-1][0] if r.get("truncated") and r["rows"] else None,
            "rows": [{"rowid": row[0], "value": _redact(_decode_cell(row[1]), hidden)} for row in r["rows"]],
            **({"redacted_fields": sorted(hidden)} if hidden else {})}


@tool()
def patch_object(pool: str, key: str, patches: dict, db: str | None = None, app: int | None = None,
                 package: str | None = None, dry_run: bool = False) -> dict:
    """Edit fields of ONE business object in place, on the live device — the running app sees it.

    `patches` maps a dotted path to its new value: {"Items": [], "Title": "demo",
    "Items[0].DamageDescription": "x", "ChangeControl.Source": "MOB"}. Paths are the same shape
    fields() prints, with [i] for list elements; the path must already exist in the object.

    Only the named values' bytes are rewritten — everything else stays byte-identical, and a
    fixed-width number keeps its original type (a LONG stays a LONG, which a decode/re-encode
    round trip would not). Replacing a whole list or map re-encodes that subtree with default
    integer widths. "#lastChange" is NOT bumped; the app may cache the object in memory, so
    restart it (or pull-to-refresh) to see the change.

    dry_run=True reports the before/after values without committing. Respects ANDROID_DB_WRITE."""
    _need_codec()
    package = _pkg(package)
    p = _resolve_pool(package, pool, db, app)
    table, pdb = p["table"], p["db"]
    if not isinstance(patches, dict) or not patches:
        raise ValueError("patches must be a non-empty {path: value} object")
    if not _jar_available(package, pdb):
        raise RuntimeError("patch_object needs the jar runner: the read and the write must hit the "
                           "same database, and without it reads come from a host snapshot while "
                           "writes go to the device. Run deploy_runner first.")
    rows = _device_sql(package, pdb, f'SELECT rowid, "#value" FROM {_q(table)} WHERE "#key" = {_sq(key)}',
                       "read", 2, "base64")["rows"]
    if not rows:
        raise ValueError(f"No object with key {key!r} in pool {p['name'] or table}")
    blob = base64.b64decode(rows[0][1]["$b64"] if isinstance(rows[0][1], dict) else rows[0][1])
    before = bo_codec.decode(blob)
    for path, value in patches.items():
        blob = bo_codec.splice(blob, path, value)
    after = bo_codec.decode(blob)
    sql = (f'UPDATE {_q(table)} SET "#value" = X\'{blob.hex().upper()}\' '
           f'WHERE "#key" = {_sq(key)}')
    effective_dry = _check_write(sql, dry_run)
    res = _device_sql(package, pdb, sql, "dryrun" if effective_dry else "write")
    return {"pool": p["name"], "table": table, "db": pdb, "key": key, "rowid": rows[0][0],
            "changes": res.get("changes"), "dry_run": effective_dry,
            "bytes": len(blob),
            "patched": {path: {"before": bo_codec.get_path(before, path),
                               "after": bo_codec.get_path(after, path)} for path in patches},
            **({"note": "Rolled back: nothing was written."} if effective_dry else
               {"note": "Written. Restart the app to clear its in-memory copy."})}


# ------------------------------------------------------------------ before/after over app actions
#
# The question this answers is "what did that button actually write?". mark() records the state of
# the business objects; changes_since() re-reads and reports added / removed / changed objects with
# a field-level diff. Identity (key, thingId, #lastChange) is recorded for every pool because that
# is one batched query; decoded values — needed for a real before/after — are recorded for pools
# under MARK_DECODE_MAX rows, and the reply says which pools were left at identity level.

_MARK_DECODE_MAX = int(os.environ.get("MARK_DECODE_MAX", "2000"))
_MARK_DECODE_TOTAL = int(os.environ.get("MARK_DECODE_TOTAL", "20000"))


def _obj_ident(tbl: Any, key: Any, thing: Any) -> tuple[str, str, str]:
    """Canonical identity of an object across a store/read round trip.

    #thingId arrives from the device as an integer but lands in a TEXT column, so it comes back as
    '0'. Comparing the raw tuples made every object look both added and removed."""
    return (str(tbl), str(key), "" if thing is None else str(thing))


def _watch_con(package: str) -> sqlite3.Connection:
    con = sqlite3.connect(_pkg_cache(package) / "watch.sqlite", timeout=60)
    con.execute("PRAGMA synchronous = OFF")      # scratch: a lost mark is re-taken, not recovered
    con.execute("PRAGMA journal_mode = MEMORY")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS marks(mark TEXT PRIMARY KEY, created REAL, label TEXT,
                                         pools TEXT, decoded_pools TEXT, objects INTEGER);
        CREATE TABLE IF NOT EXISTS obj(mark TEXT, pool TEXT, db TEXT, tbl TEXT, key TEXT, thing TEXT,
                                       last_change INTEGER, title TEXT, json TEXT,
                                       PRIMARY KEY(mark, tbl, key, thing));
    """)
    return con


def _target_pools(package: str, pools_arg: list[str] | str | None, db: str | None) -> list[dict[str, Any]]:
    """The pools a watch covers: those named, or every pool that currently holds objects."""
    if isinstance(pools_arg, str):
        pools_arg = [pools_arg]
    if pools_arg:
        out = [dict(_resolve_pool(package, p, db)) for p in pools_arg]
        by_db: dict[str, list[dict[str, Any]]] = {}
        for p in out:
            by_db.setdefault(p["db"], []).append(p)
        for d, ps in by_db.items():          # without counts the decode budget sees rows=0 and
            counts = _pool_counts(package, d, [p["table"] for p in ps])   # decodes everything
            for p in ps:
                p["rows"] = counts.get(p["table"], 0)
        return out
    out = []
    for d in ([db] if db else _bo_databases(package)):
        ps = [p for p in _pools_in(package, d) if p["exists"]]
        counts = _pool_counts(package, d, [p["table"] for p in ps])
        for p in ps:
            if counts.get(p["table"]):
                out.append({**p, "rows": counts[p["table"]]})
    return out


def _read_state(package: str, targets: list[dict[str, Any]], decode_tables: set[str],
                locale: str) -> list[dict[str, Any]]:
    """Current identity (+ decoded value where asked) of every object in the target pools.
    One query per 40 tables for identity, one per 12 tables where values are wanted."""
    by_db: dict[str, list[dict[str, Any]]] = {}
    for p in targets:
        by_db.setdefault(p["db"], []).append(p)
    rows: list[dict[str, Any]] = []
    for d, ps in by_db.items():
        plain = [p for p in ps if p["table"] not in decode_tables]
        rich = [p for p in ps if p["table"] in decode_tables]
        name_of = {p["table"]: p["name"] for p in ps}
        for group, size, cols, want_value in ((plain, 40, '"#key", "#thingId", "#lastChange"', False),
                                              (rich, 12, '"#key", "#thingId", "#lastChange", "#value"', True)):
            for i in range(0, len(group), size):
                batch = group[i:i + size]
                q = " UNION ALL ".join(
                    f'SELECT {_sq(p["table"])}, {cols} FROM {_q(p["table"])}' for p in batch)
                r = _read(package, d, q, limit=1_000_000, blobs="base64" if want_value else "summary")
                if r.get("truncated"):
                    raise RuntimeError("Too many objects to mark in one pass; narrow with pools=[...]")
                for row in r["rows"]:
                    tbl, key, thing, change = row[0], row[1], row[2], row[3]
                    rec = {"pool": name_of.get(tbl), "db": d, "tbl": tbl, "key": key, "thing": thing,
                           "last_change": change, "title": None, "json": None}
                    if want_value:
                        value = _decode_cell(row[4])
                        rec["title"] = bo_codec.display_title(value, locale)
                        rec["json"] = json.dumps(value, default=str, sort_keys=True)
                    rows.append(rec)
    return rows


def _decode_plan(targets: list[dict[str, Any]], decode: str | bool) -> set[str]:
    if decode is False or decode == "never":
        return set()
    if decode is True or decode == "always":
        return {p["table"] for p in targets}
    chosen, total = set(), 0
    for p in sorted(targets, key=lambda p: p.get("rows") or 0):
        n = p.get("rows") or 0
        if n <= _MARK_DECODE_MAX and total + n <= _MARK_DECODE_TOTAL:
            chosen.add(p["table"])
            total += n
    return chosen


@tool()
def mark(pools: list[str] | None = None, db: str | None = None, label: str | None = None,
         decode: str = "auto", locale: str | None = None, package: str | None = None) -> dict:
    """Record the current state of the app's business objects so changes_since() can tell you what
    an action changed. Typical use: call mark(), tap Save / trigger a sync in the app, then call
    changes_since(). With no arguments every pool that holds objects is covered. decode='auto'
    stores decoded values for pools under MARK_DECODE_MAX rows (those get field-level before/after);
    'always' forces it, 'never' keeps it to identity + #lastChange."""
    _need_codec()
    package = _pkg(package)
    loc = locale or LOCALE
    targets = _target_pools(package, pools, db)
    if not targets:
        raise ValueError("No pools with data to mark")
    decode_tables = _decode_plan(targets, decode)
    rows = _read_state(package, targets, decode_tables, loc)
    mark_id = f"m{int(time.time() * 1000)}"
    con = _watch_con(package)
    try:
        con.execute("INSERT INTO marks VALUES (?,?,?,?,?,?)",
                    # the full pool record, not just its name: re-resolving a name later fails
                    # outright when two tenants (or two applicationIds) share a pool name
                    (mark_id, time.time(), label,
                     json.dumps([{k: p.get(k) for k in ("name", "db", "table", "applicationId", "poolId", "rows")}
                                 for p in targets]),
                     json.dumps(sorted(decode_tables)), len(rows)))
        con.executemany("INSERT OR REPLACE INTO obj VALUES (?,?,?,?,?,?,?,?,?)",
                        [(mark_id, r["pool"], r["db"], r["tbl"], r["key"], r["thing"],
                          r["last_change"], r["title"], r["json"]) for r in rows])
        con.execute("DELETE FROM obj WHERE mark NOT IN (SELECT mark FROM marks ORDER BY created DESC LIMIT 10)")
        con.execute("DELETE FROM marks WHERE mark NOT IN (SELECT mark FROM marks ORDER BY created DESC LIMIT 10)")
        con.commit()
    finally:
        con.close()
    return {"mark": mark_id, "label": label, "objects": len(rows), "pools": len(targets),
            "pools_with_values": len(decode_tables),
            "pools_identity_only": sorted({p["name"] for p in targets if p["table"] not in decode_tables}),
            "note": "Do the thing in the app, then call changes_since()."}




def _diff_values(before: Any, after: Any, locale: str) -> dict[str, Any]:
    b = dict(bo_codec.flatten(before, locale))
    a = dict(bo_codec.flatten(after, locale))
    return {"changed": [{"path": k, "before": b[k], "after": a[k]} for k in sorted(b.keys() & a.keys()) if b[k] != a[k]],
            "added": [{"path": k, "after": a[k]} for k in sorted(a.keys() - b.keys())],
            "removed": [{"path": k, "before": b[k]} for k in sorted(b.keys() - a.keys())]}


@tool()
def changes_since(mark: str | None = None, since: int | None = None, pools: list[str] | None = None,
                  db: str | None = None, limit: int = 50, locale: str | None = None,
                  package: str | None = None) -> dict:
    """What changed in the app's data. With a mark (default: the most recent one) this compares
    against that recorded state and reports added / removed / changed objects, with a field-level
    before/after for pools whose values were stored. With since=<#lastChange> instead, it simply
    lists objects whose #lastChange is newer — no before/after, but no mark needed.
    This is the fastest way to answer 'what did that sync/tap actually write?'."""
    _need_codec()
    package = _pkg(package)
    loc = locale or LOCALE

    if since is not None:
        targets = _target_pools(package, pools, db)
        out, scanned, capped = [], 0, []
        for p in targets:
            r = _read(package, p["db"],
                      f'SELECT "#key", "#thingId", "#lastChange", "#value" FROM {_q(p["table"])} '
                      f'WHERE "#lastChange" > {int(since)} ORDER BY "#lastChange" DESC',
                      limit=limit, blobs="base64")
            scanned += 1
            if r.get("truncated"):
                capped.append(p["name"])
            for key, thing, change, blob in r["rows"]:
                value = _decode_cell(blob)
                out.append({"change": "touched", "pool": p["name"], "key": key, "thingId": thing,
                            "lastChange": change, "title": bo_codec.display_title(value, loc)})
        out.sort(key=lambda r: -(r["lastChange"] or 0))
        return {"mode": f"since #lastChange > {since}", "pools_scanned": scanned,
                "count": len(out), "changes": out[:limit],
                "truncated": len(out) > limit or bool(capped),
                "pools_hitting_the_row_cap": capped,
                "note": "No before/after in this mode — use mark() first for field-level diffs."
                        + (f" Raise limit: {len(capped)} pool(s) had more changes than it allowed."
                           if capped else "")}

    con = _watch_con(package)
    try:
        recent = [{"mark": r[0], "at": time.strftime("%H:%M:%S", time.localtime(r[1])), "label": r[2],
                   "age_s": round(time.time() - r[1], 1)}
                  for r in con.execute("SELECT mark, created, label FROM marks ORDER BY created DESC LIMIT 10")]
        if mark is None:
            if not recent:
                raise ValueError("No mark recorded yet — call mark() first (or pass since=...)")
            mark = recent[0]["mark"]
        info = con.execute("SELECT created, label, pools, decoded_pools FROM marks WHERE mark=?", (mark,)).fetchone()
        if not info:
            raise ValueError(f"Unknown mark {mark!r}")
        created, label, stored_pools, decoded_tables = info[0], info[1], json.loads(info[2]), set(json.loads(info[3]))
        stored = {_obj_ident(r[0], r[1], r[2]): r for r in con.execute(
            "SELECT tbl, key, thing, last_change, title, json, pool FROM obj WHERE mark=?", (mark,))}
    finally:
        con.close()

    if stored_pools and isinstance(stored_pools[0], dict):
        want = {str(x) for x in pools} if pools else None
        targets = [t for t in stored_pools if want is None or t["name"] in want]
    else:                                   # a mark written before pool records were stored
        targets = _target_pools(package, pools or stored_pools, db)
    if not targets:
        raise ValueError("None of the marked pools match `pools`")
    now = _read_state(package, targets, decoded_tables, loc)
    current = {_obj_ident(r["tbl"], r["key"], r["thing"]): r for r in now}

    added, removed, changed = [], [], []
    for ident, r in current.items():
        old = stored.get(ident)
        if old is None:
            added.append({"change": "added", "pool": r["pool"], "key": r["key"], "thingId": r["thing"],
                          "title": r["title"], "lastChange": r["last_change"]})
            continue
        same_change = old[3] == r["last_change"]
        same_value = (old[5] or None) == (r["json"] or None)
        if same_change and (same_value or r["json"] is None):
            continue
        item: dict[str, Any] = {"change": "changed", "pool": r["pool"], "key": r["key"], "thingId": r["thing"],
                                "title": r["title"], "lastChange": {"before": old[3], "after": r["last_change"]}}
        if old[5] and r["json"]:
            item["diff"] = _diff_values(json.loads(old[5]), json.loads(r["json"]), loc)
            if not any(item["diff"].values()) and same_change:
                continue
        else:
            item["diff"] = None
            item["note"] = "values were not recorded for this pool (identity-level only)"
        changed.append(item)
    for ident, old in stored.items():
        if ident not in current:
            removed.append({"change": "removed", "pool": old[6], "key": old[1], "thingId": old[2],
                            "title": old[4], "lastChange": old[3]})

    everything = changed + added + removed
    return {"mode": "mark diff", "mark": mark, "label": label, "recent_marks": recent,
            "marked_at": time.strftime("%H:%M:%S", time.localtime(created)),
            "elapsed_s": round(time.time() - created, 1),
            "summary": {"added": len(added), "removed": len(removed), "changed": len(changed),
                        "unchanged": len(current) - len(added) - len(changed)},
            "pools_without_values": sorted({p["name"] for p in targets if p["table"] not in decoded_tables}),
            "truncated": len(everything) > limit,
            "changes": everything[:limit]}


# ------------------------------------------------------------------ object references
#
# Business objects point at each other by id: a WorkOrder carries PriorityId, AssetId,
# OrderTypeId … Those ids are useless on their own, both to a reader and to a retrieval index.
# The map below is *derived and verified*, never hand-configured: candidate reference fields are
# found by decoding a sample of real objects, a target pool is guessed from the field name, and
# the guess is kept only if a sample of its values actually resolves to #keys in that pool.

_REF_TTL = int(os.environ.get("REF_TTL", "86400"))          # the shape of the data, not the data
_REF_SAMPLE = int(os.environ.get("REF_SAMPLE", "40"))       # objects decoded to find candidates
_REF_PROBE = int(os.environ.get("REF_PROBE", "20"))         # distinct ids used to verify one guess
_REF_MIN_HITS = float(os.environ.get("REF_MIN_HITS", "0.5"))
_REF_SUFFIX = re.compile(r"^(?P<base>.+?)(Id|Ids|ID|IDs|Uuid|UUID|Key|Keys|Ref|Refs)$")
_ID_VALUE = re.compile(r"[A-Za-z0-9_.:@+-]{3,64}")
_ref_cache: dict[tuple[str, str, str], tuple[float, list[dict[str, Any]]]] = {}
_title_cache: dict[tuple[str, str, str, str], dict[str, Any]] = {}


def _ref_base(name: str) -> str | None:
    """'AssetId' -> 'Asset'. Returns None for a bare 'Id'/'Key' (the object's own identity)."""
    m = _REF_SUFFIX.fullmatch(name)
    if not m:
        return None
    base = m.group("base")
    return base if len(base) >= 2 else None


def _looks_like_id(v: Any) -> bool:
    return isinstance(v, str) and bool(_ID_VALUE.fullmatch(v))


def _walk_ids(obj: Any, generic: str = "", concrete: str = "",
              out: list[tuple[str, str, str]] | None = None, depth: int = 8) -> list[tuple[str, str, str]]:
    """Every reference-looking value in a decoded object as (generic_path, concrete_path, id).

    The generic path collapses list positions to [*] and describes the shape of the pool
    ('Codes[*].AssetId'); the concrete path keeps the index ('Codes[3].AssetId') so a chunked
    document can claim only its own references."""
    if out is None:
        out = []
    if depth < 0:
        return out
    if isinstance(obj, dict):
        if "$b64" in obj or "$bytes" in obj or "$undecoded" in obj:
            return out
        for k, v in obj.items():
            g = f"{generic}.{k}" if generic else str(k)
            c = f"{concrete}.{k}" if concrete else str(k)
            if isinstance(k, str) and _ref_base(k):
                if _looks_like_id(v):
                    out.append((g, c, v))
                    continue
                if isinstance(v, list) and v and all(_looks_like_id(x) for x in v):
                    for i, x in enumerate(v):
                        out.append((g + "[*]", f"{c}[{i}]", x))
                    continue
            _walk_ids(v, g, c, out, depth - 1)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _walk_ids(v, generic + "[*]", f"{concrete}[{i}]", out, depth - 1)
    return out


def _ids_by_path(obj: Any) -> dict[str, list[str]]:
    d: dict[str, list[str]] = {}
    for g, _c, v in _walk_ids(obj):
        d.setdefault(g, []).append(v)
    return d


def _pool_index(package: str) -> dict[str, list[dict[str, Any]]]:
    """lower-case pool name -> pool records across every business-object database."""
    idx: dict[str, list[dict[str, Any]]] = {}
    for d in _bo_databases(package):
        for p in _pools_in(package, d):
            if p["exists"]:
                idx.setdefault(p["name"].lower(), []).append(p)
    return idx


def _target_guesses(path: str, pool_idx: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Pools a reference field might point at, from the field name alone."""
    field = path.rstrip("[*]").rsplit(".", 1)[-1]
    base = _ref_base(field)
    if not base:
        return []
    names = [base, base + "s", base.rstrip("s")]
    if base.endswith("y"):
        names.append(base[:-1] + "ies")
    seen, out = set(), []
    for n in names:
        for p in pool_idx.get(n.lower(), []):
            k = (p["db"], p["table"])
            if k not in seen:
                seen.add(k)
                out.append(p)
    return out


def _sample_values(package: str, p: dict[str, Any], n: int) -> list[Any]:
    """Decode up to n objects of a pool (newest first — they are the ones with populated fields)."""
    _need_codec()
    sql = (f'SELECT "#value" FROM {_q(p["table"])} WHERE "#value" IS NOT NULL '
           f'ORDER BY "#lastChange" DESC LIMIT {int(n)}')
    rows = _read(package, p["db"], sql, limit=n, blobs="base64")["rows"]
    return [_decode_cell(r[0]) for r in rows]


def _verify_refs(package: str, cands: list[dict[str, Any]]) -> None:
    """Confirm each candidate by looking its sample ids up in the guessed pool. One query per
    database covers every candidate pointing into it."""
    by_db: dict[str, list[dict[str, Any]]] = {}
    for i, c in enumerate(cands):
        c["_i"] = i
        by_db.setdefault(c["target_db"], []).append(c)
    for db, group in by_db.items():
        for i in range(0, len(group), 40):
            batch = group[i:i + 40]
            parts = []
            for c in batch:
                ids = c["samples"][:_REF_PROBE]
                in_list = ", ".join(_sq(str(v)) for v in ids)
                parts.append(f'SELECT {c["_i"]}, COUNT(DISTINCT "#key") FROM {_q(c["target_table"])} '
                             f'WHERE "#key" IN ({in_list})')
            try:
                rows = _read(package, db, " UNION ALL ".join(parts), limit=10000)["rows"]
            except Exception:
                continue
            found = {int(r[0]): int(r[1]) for r in rows}
            for c in batch:
                probed = len(c["samples"][:_REF_PROBE])
                c["hits"] = found.get(c["_i"], 0)
                c["probed"] = probed
                c["hit_rate"] = round(c["hits"] / probed, 3) if probed else 0.0


def _ref_map(package: str, pool: str, db: str | None = None, app: int | None = None,
             refresh: bool = False) -> list[dict[str, Any]]:
    """Verified reference fields of one pool. Cached on disk for REF_TTL (default a day)."""
    p = _resolve_pool(package, pool, db, app)
    ck = (package, p["db"], p["table"])
    if not refresh:
        hit = _ref_cache.get(ck)
        if hit is not None and time.time() - hit[0] < _REF_TTL:
            return hit[1]
    import hashlib
    h = hashlib.sha1(f'{p["db"]}#{p["table"]}'.encode()).hexdigest()[:10]
    cache = _disk_cache(package, f"refmap_{h}.json")
    if not refresh:
        hit = _cached_json(cache, _REF_TTL)
        if hit is not None:
            _ref_cache[ck] = (time.time(), hit)
            return hit

    values = _sample_values(package, p, _REF_SAMPLE)
    seen: dict[str, list[str]] = {}
    for v in values:
        for path, ids in _ids_by_path(v).items():
            bucket = seen.setdefault(path, [])
            for i in ids:
                if i not in bucket:
                    bucket.append(i)

    if not seen:                       # nothing that looks like a reference: no pools to look up
        _ref_cache[ck] = (time.time(), [])
        cache.write_text("[]")
        return []
    pool_idx = _pool_index(package)
    cands: list[dict[str, Any]] = []
    for path, ids in seen.items():
        for target in _target_guesses(path, pool_idx):
            if target["table"] == p["table"] and target["db"] == p["db"]:
                continue                      # a self-reference adds nothing to a document
            cands.append({"path": path, "field": path.rstrip("[*]").rsplit(".", 1)[-1],
                          "target_pool": target["name"], "target_db": target["db"],
                          "target_table": target["table"], "samples": ids[:_REF_PROBE],
                          "seen": len(ids)})
    if cands:
        _verify_refs(package, cands)
    out = []
    for c in cands:
        if c.get("hit_rate", 0) >= _REF_MIN_HITS:
            out.append({k: c[k] for k in ("path", "field", "target_pool", "target_db", "target_table",
                                          "hit_rate", "hits", "probed", "seen")})
    out.sort(key=lambda c: (-c["hit_rate"], c["path"]))
    # keep the best target per path: a field points at one pool
    best: dict[str, dict[str, Any]] = {}
    for c in out:
        best.setdefault(c["path"], c)
    out = list(best.values())
    _ref_cache[ck] = (time.time(), out)
    cache.write_text(json.dumps(out))
    return out


_MISSING = object()          # the referenced object does not exist, as opposed to having no title


_TITLE_TTL = int(os.environ.get("TITLE_TTL", "300"))


def _title_bucket(ck: tuple) -> dict[str, Any]:
    """Per (package, db, table, locale) title map, expired as a whole after TITLE_TTL.

    Without expiry a renamed object keeps its old label in every document built afterwards, and a
    single failed read used to pin 200 keys to 'missing' for the life of the process."""
    now = time.time()
    b = _title_cache.get(ck)
    if b is None or now - b["at"] > _TITLE_TTL:
        b = _title_cache[ck] = {"at": now, "map": {}}
    if len(_title_cache) > 64:
        for k in sorted(_title_cache, key=lambda k: _title_cache[k]["at"])[:16]:
            _title_cache.pop(k, None)
    return b["map"]


def _resolve_titles(package: str, target_db: str, target_table: str, keys: list[str],
                    locale: str) -> dict[str, Any]:
    """#key -> display title for a set of referenced objects, batched and memoised.

    Distinguishes three outcomes, because they mean different things in a document: a title, an
    object that exists but carries no label (None), and an id that resolves to nothing (_MISSING)."""
    ck = (package, target_db, target_table, locale)
    known = _title_bucket(ck)
    missing = [k for k in dict.fromkeys(keys) if k not in known]
    for i in range(0, len(missing), 200):
        batch = missing[i:i + 200]
        in_list = ", ".join(_sq(str(k)) for k in batch)
        sql = f'SELECT "#key", "#value" FROM {_q(target_table)} WHERE "#key" IN ({in_list})'
        try:
            rows = _read(package, target_db, sql, limit=len(batch) + 1, blobs="base64")["rows"]
        except Exception as e:
            # A failed read says nothing about whether these objects exist. Caching "missing" here
            # used to write "(no X object)" into every document until the server restarted.
            if os.environ.get("ANDROID_DB_DEBUG"):
                print(f"[titles unresolved, will retry] {e}", file=sys.stderr)
            continue
        for key, blob in rows:
            known[str(key)] = bo_codec.display_title(_decode_cell(blob), locale)
        for k in batch:
            known.setdefault(k, _MISSING)
    return {k: known.get(k, _MISSING) for k in keys}


def _expand_refs(package: str, value: Any, refmap: list[dict[str, Any]], locale: str,
                 titles: dict[tuple[str, str, str], str | None] | None = None,
                 scope: str | None = None) -> list[dict[str, Any]]:
    """Reference hits inside one decoded object, with a resolved title when one is known."""
    if not refmap:
        return []
    rules = {r["path"]: r for r in refmap}
    out = []
    for g, c, rid in _walk_ids(value):
        rule = rules.get(g)
        if rule is None:
            continue
        title = (titles or {}).get((rule["target_db"], rule["target_table"], rid), _MISSING)
        if scope is not None and not (c == scope or c.startswith(scope + ".") or c.startswith(scope + "[")):
            continue
        out.append({"path": g, "at": c, "field": rule["field"], "id": rid,
                    "pool": rule["target_pool"],
                    "title": title if title is not _MISSING else None,
                    "resolved": title is not _MISSING})
    return out


def _collect_ref_keys(package: str, values: list[Any], refmap: list[dict[str, Any]]
                      ) -> dict[tuple[str, str], list[str]]:
    """(db, table) -> every referenced id across a batch, so titles resolve in one pass."""
    need: dict[tuple[str, str], list[str]] = {}
    rules = {r["path"]: r for r in refmap}
    for v in values:
        for g, _c, rid in _walk_ids(v):
            rule = rules.get(g)
            if rule is not None:
                need.setdefault((rule["target_db"], rule["target_table"]), []).append(rid)
    return {k: list(dict.fromkeys(v)) for k, v in need.items()}


def _titles_for_batch(package: str, need: dict[tuple[str, str], list[str]], locale: str
                      ) -> dict[tuple[str, str, str], str | None]:
    out: dict[tuple[str, str, str], str | None] = {}
    for (db, table), keys in need.items():
        for k, t in _resolve_titles(package, db, table, keys, locale).items():
            out[(db, table, k)] = t
    return out


def _ref_lines(refs: list[dict[str, Any]]) -> list[str]:
    """The readable form that goes into an indexed document — this text is what gets embedded,
    so it says what is actually known rather than padding it with identifiers."""
    lines = []
    for r in refs:
        if r.get("title"):
            lines.append(f'{r["pool"]}: {r["title"]}')
        elif r.get("resolved"):
            lines.append(f'{r["pool"]}: {r["id"]}')            # exists, but carries no label
        else:
            lines.append(f'{r["field"]}: {r["id"]} (no {r["pool"]} object)')
    return list(dict.fromkeys(lines))


@tool()
def refs(pool: str, db: str | None = None, app: int | None = None, refresh: bool = False,
         package: str | None = None) -> dict:
    """Which fields of a pool are references to other pools, verified against real data.
    Each entry gives the JSON path inside the decoded object, the target pool, and hit_rate —
    the share of sampled ids that actually exist in that pool (1.0 = every sample resolved).
    Used automatically by search indexes and explain_object; call it to see the object graph."""
    package = _pkg(package)
    p = _resolve_pool(package, pool, db, app)
    m = _ref_map(package, pool, db, app, refresh)
    return {"pool": p["name"], "db": p["db"], "table": p["table"], "references": m,
            "sampled_objects": _REF_SAMPLE, "min_hit_rate": _REF_MIN_HITS,
            "note": "Derived by decoding sample objects and probing the guessed target pool; "
                    "fields whose ids did not resolve are not listed."}


def _locate_key(package: str, key: str) -> list[dict[str, Any]]:
    """Find which pool(s) hold a #key, batched 60 entry tables to a query."""
    hits = []
    for d in _bo_databases(package):
        ps = [p for p in _pools_in(package, d) if p["exists"]]
        for i in range(0, len(ps), 60):
            batch = ps[i:i + 60]
            q = " UNION ALL ".join(
                f'SELECT {_sq(p["table"])}, COUNT(*) FROM {_q(p["table"])} WHERE "#key"={_sq(key)}'
                for p in batch)
            try:
                rows = _read(package, d, q, limit=10000)["rows"]
            except Exception:
                continue
            by_table = {p["table"]: p for p in batch}
            for table, n in rows:
                if n:
                    hits.append({**by_table[table], "matches": n})
    return hits


def _fetch_objects(package: str, wanted: list[tuple[str, str]], locale: str) -> dict[tuple[str, str], dict]:
    """(pool, key) -> decoded record, fetched one query per pool instead of one per object."""
    by_pool: dict[str, list[str]] = {}
    for pool, key in wanted:
        by_pool.setdefault(pool, []).append(str(key))
    out: dict[tuple[str, str], dict] = {}
    for pool, keys in by_pool.items():
        try:
            p = _resolve_pool(package, pool, None, None)
        except ValueError:
            continue
        keys = list(dict.fromkeys(keys))
        for i in range(0, len(keys), 200):
            batch = keys[i:i + 200]
            sql = (f'SELECT "#key", "#thingId", "#lastChange", "#value" FROM {_q(p["table"])} '
                   f'WHERE "#key" IN ({", ".join(_sq(k) for k in batch)})')
            try:
                rows = _read(package, p["db"], sql, limit=len(batch) + 1, blobs="base64")["rows"]
            except Exception:
                continue
            for key, thing, change, blob in rows:
                value = _redact(_decode_cell(blob))
                out[(pool, str(key))] = {"pool": pool, "key": key, "thingId": thing, "lastChange": change,
                                         "db": p["db"], "table": p["table"], "value": value,
                                         "title": bo_codec.display_title(value, locale)}
    return out


@tool()
def related(key: str, pool: str | None = None, depth: int = 2, incoming: bool = True,
            locale: str | None = None, limit: int = 200, package: str | None = None) -> dict:
    """The neighbourhood of one object: what it points at, what points at it, and the same again
    one hop further out. Answers "what is attached to work order 10018?" in a single call, where
    explain_object answers "what is IN this object".

    Each hop reads its objects in one query per pool rather than one per object, so widening the
    depth costs round trips proportional to the number of pools touched, not the number of objects.
    Incoming hops are only searched in pools whose reference map is already built — call
    refs(pool=...) on a pool to include it — and the reply says which pools were and were not."""
    _need_codec()
    package = _pkg(package)
    loc = locale or LOCALE
    depth = max(1, min(int(depth), 4))
    if pool:
        start = _resolve_pool(package, pool, None, None)
    else:
        found = _locate_key(package, key)
        if not found:
            raise ValueError(f"No object with #key={key!r} in any pool of {package}")
        if len(found) > 1:
            return {"key": key, "ambiguous": True,
                    "found_in": [{"pool": f["name"], "db": f["db"]} for f in found],
                    "note": "#key is unique per pool, not globally. Pass pool= to choose one."}
        start = found[0]

    root = (start["name"], str(key))
    nodes: dict[tuple[str, str], dict[str, Any]] = {root: {"pool": root[0], "key": key, "hop": 0, "title": None}}
    links: list[dict[str, Any]] = []
    seen_link: set[tuple] = set()
    frontier = [root]
    searched: set[str] = set()
    skipped: set[str] = set()

    for hop in range(1, depth + 1):
        loaded = _fetch_objects(package, frontier, loc)
        # resolve every reference title this hop needs in one pass per target pool
        refmaps = {}
        for pname in {p for p, _k in frontier}:
            try:
                refmaps[pname] = _ref_map(package, pname, None, None)
            except Exception:
                refmaps[pname] = []
        need = _collect_ref_keys(package, [r["value"] for r in loaded.values()],
                                 [rule for m in refmaps.values() for rule in m])
        titles = _titles_for_batch(package, need, loc)

        nxt: list[tuple[str, str]] = []
        for ident in frontier:
            rec = loaded.get(ident)
            if rec is None:
                continue
            nodes[ident]["title"] = rec["title"]
            for r in _expand_refs(package, rec["value"], refmaps.get(ident[0], []), loc, titles):
                t = (r["pool"], str(r["id"]))
                edge = (ident, t, r["path"], "out")
                if edge not in seen_link:
                    seen_link.add(edge)
                    links.append({"from": f"{ident[0]}/{ident[1]}", "to": f"{t[0]}/{t[1]}",
                                  "path": r["path"], "direction": "out", "resolved": r.get("resolved", True)})
                if r.get("resolved") and t not in nodes and len(nodes) < limit:
                    nodes[t] = {"pool": t[0], "key": r["id"], "hop": hop, "title": r.get("title")}
                    nxt.append(t)
            if incoming:
                inc = _incoming_refs(package, str(ident[1]), rec["table"], loc)
                searched.update(inc.get("searched_pools", []))
                skipped.update(inc.get("skipped_pools_without_reference_map", []))
                for r in inc.get("hits", []):
                    t = (r["pool"], str(r["key"]))
                    edge = (t, ident, r["path"], "in")
                    if edge not in seen_link:
                        seen_link.add(edge)
                        links.append({"from": f"{t[0]}/{t[1]}", "to": f"{ident[0]}/{ident[1]}",
                                      "path": r["path"], "direction": "in", "resolved": True})
                    if t not in nodes and len(nodes) < limit:
                        nodes[t] = {"pool": t[0], "key": r["key"], "hop": hop, "title": r.get("title")}
                        nxt.append(t)
        frontier = nxt
        if not frontier:
            break

    out_nodes = sorted(nodes.values(), key=lambda n: (n["hop"], n["pool"], str(n["key"])))
    by_pool_count: dict[str, int] = {}
    for n in out_nodes:
        by_pool_count[n["pool"]] = by_pool_count.get(n["pool"], 0) + 1
    return {"root": f"{root[0]}/{key}", "depth": depth,
            "nodes": out_nodes, "links": links,
            "counts_by_pool": dict(sorted(by_pool_count.items(), key=lambda kv: -kv[1])),
            "truncated": len(nodes) >= limit,
            "incoming": {"searched_pools": sorted(searched),
                         "skipped_pools_without_reference_map": sorted(skipped)} if incoming else None}


@tool()
def explain_object(key: str, pool: str | None = None, db: str | None = None, app: int | None = None,
                   depth: int = 1, locale: str | None = None, incoming: bool = False,
                   package: str | None = None) -> dict:
    """Everything about one business object in a single call: decode it, label it, resolve its
    references to real titles ('Priority: High' instead of a uuid), and list its non-empty fields.
    Omit `pool` and the key is located across every pool first. depth=2 also inlines the decoded
    value of each referenced object. incoming=True additionally looks for objects that point back
    at this one (only pools whose reference map is already built are searched)."""
    _need_codec()
    package = _pkg(package)
    loc = locale or LOCALE
    if pool:
        p = _resolve_pool(package, pool, db, app)
        found = [p]
    else:
        found = _locate_key(package, key)
        if not found:
            raise ValueError(f"No object with #key={key!r} in any pool of {package}")
        if len(found) > 1:
            return {"key": key, "ambiguous": True,
                    "found_in": [{"pool": f["name"], "db": f["db"], "table": f["table"]} for f in found],
                    "note": "Pass pool= to choose one."}
        p = found[0]
    sql = f'SELECT * FROM {_q(p["table"])} WHERE "#key"={_sq(key)}'
    r = _read(package, p["db"], sql, limit=2, blobs="base64")
    if not r["rows"]:
        raise ValueError(f"No object with #key={key!r} in pool {p['name']}")
    rec = dict(zip(r["columns"], r["rows"][0]))
    value = _decode_cell(rec.get("#value"))
    refmap = _ref_map(package, p["name"] or p["table"], p["db"], p.get("applicationId"))
    need = _collect_ref_keys(package, [value], refmap)
    titles = _titles_for_batch(package, need, loc)
    references = _expand_refs(package, value, refmap, loc, titles)
    if depth >= 2:
        for ref in references:
            rule = next((m for m in refmap if m["path"] == ref["path"]), None)
            if not rule:
                continue
            rr = _read(package, rule["target_db"],
                       f'SELECT "#value" FROM {_q(rule["target_table"])} WHERE "#key"={_sq(ref["id"])}',
                       limit=1, blobs="base64")
            ref["value"] = _decode_cell(rr["rows"][0][0]) if rr["rows"] else None
    hidden: set[str] = set()
    value = _redact(value, hidden)
    fields = [{"path": path, "text": text} for path, text in bo_codec.flatten(value, loc)]
    out = {
        "key": key, "pool": p["name"], "db": p["db"], "table": p["table"],
        "thingId": rec.get("#thingId"), "lastChange": rec.get("#lastChange"),
        "immutable": rec.get("#immutable"),
        "title": bo_codec.display_title(value, loc),
        "summary": "\n".join([f'{p["name"]}: {bo_codec.display_title(value, loc) or key}']
                             + _ref_lines(references)
                             + [f'{f["path"]}: {f["text"]}' for f in fields[:40]]),
        "references": references,
        "promoted_columns": {k: (REDACTED if _is_secret(k) else v) for k, v in rec.items()
                             if not str(k).startswith("#") and v is not None},
        "redacted_fields": sorted(hidden) or None,
        "fields": fields,
        "value": value,
    }
    if incoming:
        out["referenced_by"] = _incoming_refs(package, key, p["table"], loc)
    return out


def _incoming_refs(package: str, key: str, target_table: str, locale: str,
                   scan: int = 2000) -> dict[str, Any]:
    """Objects pointing at this key. Only pools whose *already built* reference map has a rule
    aimed at this key's table are scanned, and each scan is capped — the reply says which pools
    were searched and whether any of them was truncated."""
    import hashlib
    searched, skipped, hits, partial = [], [], [], []
    for d in _bo_databases(package):
        for p in _pools_in(package, d):
            if not p["exists"] or p["table"] == target_table:
                continue
            h = hashlib.sha1(f'{p["db"]}#{p["table"]}'.encode()).hexdigest()[:10]
            m = _cached_json(_disk_cache(package, f"refmap_{h}.json"), _REF_TTL)
            if m is None:
                skipped.append(p["name"])
                continue
            rules = [r for r in m if r["target_table"] == target_table]
            if not rules:
                continue
            searched.append(p["name"])
            r = _read(package, p["db"], f'SELECT "#key", "#value" FROM {_q(p["table"])}',
                      limit=scan, blobs="base64")
            if r.get("truncated"):
                partial.append(p["name"])
            for k, blob in r["rows"]:
                v = _decode_cell(blob)
                ids = _ids_by_path(v)
                for rule in rules:
                    if key in ids.get(rule["path"], []):
                        hits.append({"pool": p["name"], "key": k, "path": rule["path"],
                                     "title": bo_codec.display_title(v, locale)})
    return {"hits": hits, "searched_pools": searched, "truncated_pools": partial,
            "skipped_pools_without_reference_map": skipped,
            "complete": not partial and not skipped,
            "note": "Call refs(pool=...) on a pool to have it included here."}


_CONTEXT_TTL = int(os.environ.get("CONTEXT_TTL", "60"))
_TENANT_RE = re.compile(r"/files/([^/]+)/databases/")


def _tenant_of(db: str) -> str | None:
    """External per-tenant databases live in files/<tenant>/databases/<name>."""
    m = _TENANT_RE.search(db)
    return m.group(1) if m else None


def _device_props() -> dict[str, str]:
    """Model / Android version / abi in one shell round-trip."""
    keys = ("ro.product.model", "ro.build.version.release", "ro.build.version.sdk", "ro.product.cpu.abi")
    try:
        out = str(_as_shell("; ".join(f"getprop {k}" for k in keys), check=False)).splitlines()
    except Exception:
        out = []
    vals = [line.strip() for line in out if line.strip()]
    got = dict(zip(("model", "android", "sdk", "abi"), vals))
    return got if len(vals) == len(keys) else got


def _index_inventory(package: str) -> list[dict[str, Any]]:
    """What retrieval indexes exist for this device+package, without touching the device."""
    out = []
    for f in sorted(_pkg_cache(package).glob("index_*.sqlite")):
        entry: dict[str, Any] = {"path": str(f), "bytes": f.stat().st_size,
                                 "age_s": round(time.time() - f.stat().st_mtime, 1)}
        try:
            con = _open_ro(str(f), 5)
            try:
                meta = {k: json.loads(v) for k, v in con.execute("SELECT k, v FROM meta")}
                spec = meta.get("spec") or {}
                entry.update({"pool": spec.get("pool"), "table": spec.get("table"), "db": spec.get("db"),
                              "chunk": spec.get("chunk") or None, "locale": spec.get("locale"),
                              "docs": con.execute("SELECT COUNT(*) FROM docs").fetchone()[0],
                              "embedded": bool(meta.get("embedded")), "embed_model": meta.get("embed_model"),
                              "watermark": meta.get("watermark")})
            finally:
                con.close()
        except sqlite3.Error as e:
            entry["error"] = str(e)
        out.append(entry)
    return out


@tool()
def app_context(package: str | None = None, refresh: bool = False, pools_limit: int = 25) -> dict:
    """START HERE. One call that says what this session is actually looking at: the device, the
    package and where it was detected, the read backend, every database grouped by tenant, the
    business-object pools that hold data (largest first) and the retrieval indexes already built.
    Cached for CONTEXT_TTL seconds (default 60); pass refresh=True after switching tenant or
    reinstalling the app."""
    package = _pkg(package)
    _prewarm(package)
    cache = _disk_cache(package, "context.json")
    if not refresh:
        hit = _cached_json(cache, _CONTEXT_TTL)
        if hit is not None:
            hit["age_s"] = round(time.time() - cache.stat().st_mtime, 1)
            hit["cached"] = True
            return hit
    if refresh:
        _forget_jar(package)
        _pool_cache.clear()

    dbs = list_databases(package, refresh=refresh)
    by_tenant: dict[str, list[str]] = {}
    for d in dbs:
        by_tenant.setdefault(_tenant_of(d) or "(app private)", []).append(d)
    bo_dbs = _bo_databases(package, refresh=refresh)

    pool_rows: list[dict[str, Any]] = []
    empty = 0
    for d in bo_dbs:
        ps = _pools_in(package, d, refresh=refresh)
        counts = _pool_counts(package, d, [p["table"] for p in ps if p["exists"]])
        for p in ps:
            n = counts.get(p["table"], 0)
            if n:
                pool_rows.append({"pool": p["name"], "rows": n, "db": p["db"],
                                  "applicationId": p["applicationId"], "poolId": p["poolId"]})
            else:
                empty += 1
    pool_rows.sort(key=lambda r: -r["rows"])

    jar = _jar_available(package)
    out = {
        "device": {"serial": _serial(), **_device_props()},
        "package": package,
        "package_source": "ANDROID_PACKAGE env" if DEFAULT_PKG else _detected.get("source", "explicit"),
        "backend": {"effective": "jar" if jar else "snapshot", "runner": RUNNER,
                    "setting": BACKEND, "jar_deployed": jar, "persistent_runner": PERSIST},
        "databases": {"total": len(dbs), "business_object_dbs": bo_dbs, "by_tenant": by_tenant},
        "tenants": [t for t in by_tenant if t != "(app private)"],
        "pools": {"populated": len(pool_rows), "empty": empty,
                  "objects_total": sum(r["rows"] for r in pool_rows),
                  "top": pool_rows[:max(0, pools_limit)]},
        "indexes": _index_inventory(package),
        "locale": LOCALE,
        "cache_dir": str(_pkg_cache(package)),
        "hints": [
            "Promoted columns on entry tables are often empty; the real content is the #value BLOB "
            "— read it with entries(pool=...) or decode().",
            "Pool names are the handle for entries() and search().",
            "execute() mutates live app data — only when explicitly asked.",
        ],
        "age_s": 0.0, "cached": False, "built_at": time.time(),
    }
    cache.write_text(json.dumps(out, default=str))
    return out


# ------------------------------------------------------------------ retrieval indexes
#
# One SQLite file per index (FTS5 + optional embedding vectors), keyed by what was indexed.
# Documents use the same read backend as query(). Pool source rows are tracked independently
# of chunks, including rows producing no documents. NULL change markers are always re-read.

_AUTO_TTL = int(os.environ.get("INDEX_TTL", "120"))   # plain-table indexes: rebuild when older than this
_INDEX_VERSION = 6
_index_lock = threading.RLock()


def _embedding_model() -> str:
    return os.environ.get("EMBED_MODEL", "BAAI/bge-small-en-v1.5")


_INDEX_REFS = os.environ.get("INDEX_REFS", "1") != "0"


def _index_spec(package: str, db: str, table: str | None, columns: list[str] | None, pool: str | None,
                chunk: str | None, locale: str, app: int | None, refs: bool | None = None) -> dict[str, Any]:
    if refs is None:
        refs = _INDEX_REFS
    if pool:
        p = _resolve_pool(package, pool, db, app)
        spec = {"kind": "pool", "db": p["db"], "table": p["table"], "pool": p["name"],
                "applicationId": p["applicationId"], "poolId": p["poolId"], "chunk": chunk or "",
                "locale": locale, "refs": bool(refs)}
    else:
        if not (db and table and columns):
            raise ValueError("Give pool=<name>, or db=... table=... with columns=[...]")
        spec = {"kind": "table", "db": db, "table": table, "columns": list(columns), "chunk": "", "locale": locale}
    spec["device"] = _device_tag()
    import hashlib
    h = hashlib.sha1(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:12]
    spec["path"] = str(_pkg_cache(package) / f"index_{h}.sqlite")
    return spec


def _open_ro(path: str, timeout: float = 120) -> sqlite3.Connection:
    """Read-only connection to an index file.

    Deliberately not a `file:...?mode=ro` URI: the cache directory comes from ANDROID_DB_CACHE and
    a '#' or '?' in it is parsed as a URI fragment/query, which silently opens a different (empty)
    database. A plain connect plus query_only gives the same protection with no parsing."""
    con = sqlite3.connect(path, timeout=timeout)
    con.execute("PRAGMA query_only = 1")
    return con


def _open_index(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=120)
    # These files are a derived cache under ANDROID_DB_CACHE: if one is ever lost or torn, the next
    # refresh rebuilds it from the device. Paying for durable fsyncs on every commit is pure cost.
    con.execute("PRAGMA synchronous = OFF")
    con.execute("PRAGMA journal_mode = WAL")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS docs(doc_id TEXT PRIMARY KEY, src_key TEXT, last_change INTEGER,
                                        text TEXT, meta TEXT, vec BLOB);
        CREATE INDEX IF NOT EXISTS docs_src ON docs(src_key);
        CREATE TABLE IF NOT EXISTS sources(src_key TEXT PRIMARY KEY, last_change INTEGER);
        CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(text, doc_id UNINDEXED);
        -- decoded mirror: one row per business object, its #value as JSON. json_each/json_extract
        -- then give real SQL over nested fields without decoding anything again.
        CREATE TABLE IF NOT EXISTS decoded(src_key TEXT PRIMARY KEY, key TEXT, thing TEXT,
                                           last_change INTEGER, title TEXT, json TEXT);
        CREATE INDEX IF NOT EXISTS decoded_key ON decoded(key);
        -- which field paths exist in this pool and how often they carry a value
        CREATE TABLE IF NOT EXISTS paths(path TEXT PRIMARY KEY, kind TEXT, n INTEGER,
                                         n_filled INTEGER, example TEXT);
    """)
    return con


def _meta(con: sqlite3.Connection) -> dict[str, Any]:
    return {k: json.loads(v) for k, v in con.execute("SELECT k, v FROM meta")}


def _set_meta(con: sqlite3.Connection, **kv: Any) -> None:
    con.executemany("INSERT OR REPLACE INTO meta(k, v) VALUES (?, ?)", [(k, json.dumps(v)) for k, v in kv.items()])


def _source_key(key: Any, thing: Any) -> str:
    return json.dumps([key, thing], separators=(",", ":"), ensure_ascii=False)


def _index_read(package: str, spec: dict[str, Any], sql: str, blobs: str = "summary") -> list:
    result = _read(package, spec["db"], sql, limit=10_000_000, blobs=blobs)
    if result.get("truncated"):
        raise RuntimeError("Index input exceeded the row limit; refusing to publish a partial index")
    return result["rows"]


def _docs_from_pool(package: str, spec: dict[str, Any], keys: list[tuple[Any, Any]] | None = None
                    ) -> tuple[list[dict[str, Any]], dict[str, Any], list[tuple]]:
    """Decode a pool into retrieval documents.

    References are resolved to titles before the text is written, so a document says
    'Priority: High' where the raw object only holds a uuid. Titles for the whole batch are
    fetched in one pass per target pool, which keeps this to a couple of extra device queries
    regardless of how many objects are indexed."""
    _need_codec()
    sql = f'SELECT "#key", "#thingId", "#lastChange", "#value" FROM {_q(spec["table"])}'
    rows = []
    if keys is None:
        rows = _index_read(package, spec, sql, "base64")
    else:
        literal = lambda v: "NULL" if v is None else _sq(str(v))
        for i in range(0, len(keys), 100):
            clauses = [f'("#key" IS {literal(k)} AND "#thingId" IS {literal(t)})' for k, t in keys[i:i + 100]]
            rows.extend(_index_read(package, spec, sql + " WHERE " + " OR ".join(clauses), "base64"))

    decoded: list[tuple[str, Any, Any, Any, Any]] = []
    sources: dict[str, Any] = {}
    for key, thing, last_change, blob in rows:
        src_key = _source_key(key, thing)
        sources[src_key] = last_change
        decoded.append((src_key, key, thing, last_change, _decode_cell(blob)))

    refmap: list[dict[str, Any]] = []
    titles: dict[tuple[str, str, str], str | None] = {}
    if spec.get("refs") and decoded:
        try:
            refmap = _ref_map(package, spec["pool"], spec["db"], spec.get("applicationId"))
            if refmap:
                titles = _titles_for_batch(package, _collect_ref_keys(
                    package, [v for *_rest, v in decoded], refmap), spec["locale"])
        except Exception as e:      # retrieval must not fail because the object graph is odd
            if os.environ.get("ANDROID_DB_DEBUG"):
                print(f"[refs skipped] {e}", file=sys.stderr)
            refmap = []

    chunk_path = spec["chunk"] or None
    docs = []
    for src_key, key, thing, last_change, value in decoded:
        all_refs = _expand_refs(package, value, refmap, spec["locale"], titles) if refmap else []
        outside = [r for r in all_refs
                   if not chunk_path or not r["at"].startswith(chunk_path + "[")]
        value = _redact(value)          # never let a credential reach an index or an embedding
        for suffix, unit, ctx in bo_codec.chunks(value, chunk_path, spec["locale"]):
            scope = suffix[1:] if suffix else None
            inside = [r for r in all_refs
                      if scope and (r["at"] == scope or r["at"].startswith(scope + ".")
                                    or r["at"].startswith(scope + "["))] if scope else []
            use = outside + inside
            head = [f"pool: {spec['pool']}", f"key: {key}"]
            if ctx.get("parent_title"):
                head.append(f"parent: {ctx['parent_title']}")
            text = "\n".join(head + _ref_lines(use) + [bo_codec.to_text(unit, spec["locale"])])
            meta = {"key": key, "thingId": thing, "applicationId": spec["applicationId"], "poolId": spec["poolId"],
                    "title": bo_codec.display_title(unit, spec["locale"]), **ctx}
            if use:
                meta["refs"] = [{"field": r["field"], "pool": r["pool"], "id": r["id"],
                                 "title": r["title"], "resolved": r.get("resolved")}
                                for r in use]
            docs.append({"doc_id": src_key + suffix, "src_key": src_key, "last_change": last_change,
                         "text": text, "meta": meta})
    mirror = [(src_key, key, thing, last_change, bo_codec.display_title(value, spec["locale"]),
               json.dumps(value, default=str, ensure_ascii=False))
              for src_key, key, thing, last_change, value in decoded]
    return docs, sources, mirror


def _docs_from_table(package: str, spec: dict[str, Any]) -> list[dict[str, Any]]:
    cols = spec["columns"]
    rows = _index_read(package, spec, f"SELECT rowid, {', '.join(_q(c) for c in cols)} FROM {_q(spec['table'])}")
    return [{"doc_id": str(row[0]), "src_key": str(row[0]), "last_change": None,
             "text": "\n".join(f"{c}: {v}" for c, v in zip(cols, row[1:]) if v is not None and not isinstance(v, dict)),
             "meta": {"rowid": row[0]}} for row in rows]


def _kind_of(v: Any) -> str:
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        return "text"
    if isinstance(v, list):
        return "list"
    if isinstance(v, dict):
        return "object"
    return "null"


def _walk_paths(obj: Any, locale: str, prefix: str = "", out: dict[str, dict[str, Any]] | None = None,
                depth: int = 10) -> dict[str, dict[str, Any]]:
    """Generic path -> {kind, filled, example} for one object. List positions collapse to [*], so
    the result describes the shape of the pool rather than one row."""
    if out is None:
        out = {}
    if depth < 0:
        return out
    if isinstance(obj, dict) and ("$b64" in obj or "$bytes" in obj or "$undecoded" in obj):
        return out
    text = bo_codec.resolve_locale(obj, locale)
    if text is not None:
        rec = out.setdefault(prefix or "$", {"kind": "text(translated)", "filled": 0, "example": None})
        rec["filled"] += 1 if str(text).strip() else 0
        rec["example"] = rec["example"] or (text[:80] if text else None)
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            _walk_paths(v, locale, f"{prefix}.{k}" if prefix else str(k), out, depth - 1)
    elif isinstance(obj, list):
        for v in obj:
            _walk_paths(v, locale, prefix + "[*]", out, depth - 1)
    else:
        rec = out.setdefault(prefix or "$", {"kind": _kind_of(obj), "filled": 0, "example": None})
        if obj is not None and str(obj).strip() != "":
            rec["filled"] += 1
            if rec["example"] is None:
                rec["example"] = str(obj)[:80]
    return out


_PATHS_MAX = int(os.environ.get("PATHS_MAX", "20000"))


def _rebuild_paths(con: sqlite3.Connection, locale: str) -> int:
    """Recompute the path/fill-rate table from the decoded mirror. Skipped above PATHS_MAX objects,
    where walking every object on each incremental refresh would cost more than the table is worth."""
    n = con.execute("SELECT COUNT(*) FROM decoded").fetchone()[0]
    if n > _PATHS_MAX:
        con.execute("DELETE FROM paths")
        return 0
    stats: dict[str, dict[str, Any]] = {}
    total = 0
    for (raw,) in con.execute("SELECT json FROM decoded"):
        total += 1
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            continue
        for path, rec in _walk_paths(value, locale).items():
            agg = stats.setdefault(path, {"kind": rec["kind"], "n": 0, "filled": 0, "example": None})
            agg["n"] += 1
            agg["filled"] += 1 if rec["filled"] else 0
            agg["example"] = agg["example"] or rec["example"]
            if agg["kind"] != rec["kind"]:
                agg["kind"] = "mixed"
    con.execute("DELETE FROM paths")
    con.executemany("INSERT INTO paths(path, kind, n, n_filled, example) VALUES (?,?,?,?,?)",
                    [(p, a["kind"], a["n"], a["filled"], a["example"]) for p, a in sorted(stats.items())])
    return total


def _live_sources(package: str, spec: dict[str, Any]) -> dict[str, tuple[Any, Any, Any]]:
    rows = _index_read(package, spec, f'SELECT "#key", "#thingId", "#lastChange" FROM {_q(spec["table"])}')
    return {_source_key(k, t): (k, t, changed) for k, t, changed in rows}


def _changed_sources(live: dict, stored: dict) -> list[str]:
    return [k for k, (_, _, change) in live.items() if k not in stored or change is None or stored[k] != change]


def _delete_sources(con: sqlite3.Connection, keys: list[str]) -> None:
    for k in keys:
        con.execute("DELETE FROM fts WHERE doc_id IN (SELECT doc_id FROM docs WHERE src_key=?)", (k,))
        con.execute("DELETE FROM docs WHERE src_key=?", (k,))
        con.execute("DELETE FROM sources WHERE src_key=?", (k,))
        con.execute("DELETE FROM decoded WHERE src_key=?", (k,))


def _upsert_docs(con: sqlite3.Connection, docs: list[dict[str, Any]]) -> None:
    for d in docs:
        con.execute("INSERT INTO docs(doc_id, src_key, last_change, text, meta, vec) VALUES (?,?,?,?,?,?)",
                    (d["doc_id"], d["src_key"], d["last_change"], d["text"], json.dumps(d["meta"]), None))
        con.execute("INSERT INTO fts(text, doc_id) VALUES (?, ?)", (d["text"], d["doc_id"]))


def _upsert_decoded(con: sqlite3.Connection, rows: list[tuple]) -> None:
    con.executemany("INSERT OR REPLACE INTO decoded(src_key, key, thing, last_change, title, json) "
                    "VALUES (?,?,?,?,?,?)", rows)


def _fill_vectors(con: sqlite3.Connection) -> int:
    """Repair only missing vectors, invalidating all vectors if the model changed."""
    import numpy as np
    meta = _meta(con)
    model = _embedding_model()
    dimensions = meta.get("embedding_dimensions")
    if meta.get("embed_model") != model:
        con.execute("UPDATE docs SET vec=NULL")
        dimensions = None
    count = 0
    while True:
        rows = con.execute("SELECT doc_id, text FROM docs WHERE vec IS NULL LIMIT 128").fetchall()
        if not rows:
            break
        vectors = list(_embedder().embed([r[1] for r in rows]))
        if len(vectors) != len(rows):
            raise RuntimeError("Embedding model returned the wrong number of vectors")
        for (doc_id, _), vector in zip(rows, vectors):
            vec = np.asarray(vector, dtype=np.float32)
            if vec.ndim != 1 or not vec.size or not np.isfinite(vec).all():
                raise RuntimeError("Embedding model returned an invalid vector")
            if dimensions is not None and dimensions != vec.size:
                raise RuntimeError("Embedding dimensions changed; rebuild with refresh='full'")
            dimensions = int(vec.size)
            con.execute("UPDATE docs SET vec=? WHERE doc_id=?", (vec.tobytes(), doc_id))
            count += 1
    _set_meta(con, embed_model=model, embedding_dimensions=dimensions)
    return count


def _ensure_index(package: str, spec: dict[str, Any], refresh: str, embed: bool) -> dict[str, Any]:
    """Serialize refreshes so concurrent tools cannot publish conflicting index state."""
    if refresh not in ("auto", "full", "never"):
        raise ValueError("refresh must be auto, full, or never")
    with _index_lock:
        return _refresh_index(package, spec, refresh, embed)


def _refresh_index(package: str, spec: dict[str, Any], refresh: str, embed: bool) -> dict[str, Any]:
    con = _open_index(spec["path"])
    try:
        # Also serialize writers from separate MCP/CLI processes sharing this cache.
        con.execute("BEGIN IMMEDIATE")
        meta = _meta(con)
        exists = bool(meta.get("built_at"))
        if exists and meta.get("index_version") != _INDEX_VERSION:
            if refresh == "never":
                raise RuntimeError("Index format changed; use refresh='full' to rebuild")
            refresh = "full"
        if exists and refresh == "auto" and spec["kind"] == "table" and time.time() - meta["built_at"] > _AUTO_TTL:
            refresh = "full"
        status = "reused"
        if refresh == "full" or not exists:
            con.execute("DELETE FROM docs")
            con.execute("DELETE FROM fts")
            con.execute("DELETE FROM sources")
            con.execute("DELETE FROM decoded")
            con.execute("DELETE FROM paths")
            if spec["kind"] == "pool":
                docs, sources, mirror = _docs_from_pool(package, spec)
                con.executemany("INSERT INTO sources VALUES (?, ?)", sources.items())
                _upsert_decoded(con, mirror)
                _rebuild_paths(con, spec["locale"])
            else:
                docs = _docs_from_table(package, spec)
            _upsert_docs(con, docs)
            _set_meta(con, built_at=time.time(), index_version=_INDEX_VERSION, embedding_dimensions=None,
                      spec={k: v for k, v in spec.items() if k != "path"})
            status = f"built ({len(docs)} docs)"
        elif refresh == "auto" and spec["kind"] == "pool":
            live = _live_sources(package, spec)
            stored = dict(con.execute("SELECT src_key, last_change FROM sources"))
            changed = _changed_sources(live, stored)
            gone = list(stored.keys() - live.keys())
            _delete_sources(con, changed + gone)
            if changed:
                docs, sources, mirror = _docs_from_pool(package, spec, [(live[k][0], live[k][1]) for k in changed])
                _upsert_docs(con, docs)
                _upsert_decoded(con, mirror)
                con.executemany("INSERT INTO sources VALUES (?, ?)", sources.items())
            if changed or gone:
                _rebuild_paths(con, spec["locale"])
                _set_meta(con, built_at=time.time())
                status = f"refreshed (+{len(changed)} changed, -{len(gone)} deleted)"
            else:
                status = "up to date"
        embedded_now = _fill_vectors(con) if embed else 0
        missing = con.execute("SELECT COUNT(*) FROM docs WHERE vec IS NULL").fetchone()[0]
        wm = con.execute("SELECT MAX(last_change) FROM sources").fetchone()[0]
        _set_meta(con, watermark=wm, embedded=missing == 0 and bool(_meta(con).get("embed_model")))
        if refresh != "never":
            _set_meta(con, checked_at=time.time())
        con.commit()
        meta = _meta(con)
        n = con.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
        stale = deleted = None
        if refresh == "never" and spec["kind"] == "pool":
            live = _live_sources(package, spec)
            stored = dict(con.execute("SELECT src_key, last_change FROM sources"))
            stale = len(_changed_sources(live, stored))
            deleted = len(stored.keys() - live.keys())
        return {"status": status, "docs": n, "built_at": meta.get("built_at"),
                "age_s": round(time.time() - meta["built_at"], 1), "watermark": meta.get("watermark"),
                "checked_at": meta.get("checked_at"), "stale_rows": stale, "deleted_rows": deleted,
                "index_version": _INDEX_VERSION, "embed_model": meta.get("embed_model"),
                "missing_vectors": missing, "embedded_now": embedded_now,
                "path": spec["path"]}
    finally:
        con.close()












# ------------------------------------------------------------------ hybrid retrieval
#
# bm25 and vectors answer different questions: one finds the exact code or id you typed, the
# other finds the thing you described. Both already live in the same index file, so fusing their
# rankings (reciprocal rank fusion) costs one extra query and removes the need to guess which
# search to use. Every hit says which retriever found it and why.

_VEC_CACHE: dict[str, tuple[tuple, tuple]] = {}
# Measured on this data with eval.py: bm25 clearly leads on short coded titles, so the fusion
# leans on it while still letting a vector-only hit surface. Override per call with weights=.
# Embeddings are off unless asked for: measured on this data they matched bm25 overall
# (MRR 0.78 both) while costing a model download and several seconds per index build.
EMBEDDINGS = os.environ.get("EMBEDDINGS", "off").lower() not in ("0", "off", "false", "no")
_HYBRID_WEIGHTS = {"bm25": float(os.environ.get("HYBRID_W_BM25", "1.0")),
                   "vector": float(os.environ.get("HYBRID_W_VECTOR", "0.5"))}
_FTS_TOKEN = re.compile(r"[\w']+", re.UNICODE)


def _fts_search(path: str, q: str, n: int) -> list[tuple]:
    """bm25 over the index, with two fallbacks.

    FTS5 joins bare words with AND, so a natural-language query like "cause of failure" matches
    nothing unless every word appears in one document. When the strict reading finds nothing, the
    terms are retried joined with OR — which is what a person typing into a search box means. The
    other fallback is for syntax: a stray '-' or ':' is a MATCH error, not a query."""
    sql = ("SELECT fts.doc_id, bm25(fts) AS score, snippet(fts, 0, '[', ']', '…', 24), d.meta, d.text "
           "FROM fts JOIN docs d ON d.doc_id = fts.doc_id WHERE fts MATCH ? ORDER BY score LIMIT ?")
    terms = _FTS_TOKEN.findall(q or "")
    any_of = " OR ".join(f'"{t}"' for t in terms)
    con = _open_ro(path)
    try:
        try:
            rows = con.execute(sql, (q, n)).fetchall()
        except sqlite3.OperationalError:
            rows = con.execute(sql, (any_of, n)).fetchall() if any_of else []
        if not rows and len(terms) > 1 and any_of and not os.environ.get("FTS_NO_OR"):
            rows = con.execute(sql, (any_of, n)).fetchall()
        return rows
    finally:
        con.close()


def _vectors(path: str):
    """(doc_ids, texts, metas, unit_matrix) for an index, cached until the file changes.

    Searching used to re-read and re-stack every vector on each call; caching the matrix
    (already L2-normalised) turns a repeat search into one matrix-vector product."""
    st = os.stat(path)
    stamp = (st.st_mtime_ns, st.st_size)
    hit = _VEC_CACHE.get(path)
    if hit and hit[0] == stamp:
        return hit[1]
    import numpy as np
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=120)
    try:
        rows = con.execute("SELECT doc_id, text, meta, vec FROM docs WHERE vec IS NOT NULL").fetchall()
    finally:
        con.close()
    if not rows:
        data: tuple = ([], [], [], None)
    else:
        mat = np.stack([np.frombuffer(r[3], dtype=np.float32) for r in rows])
        mat = mat / (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9)
        data = ([r[0] for r in rows], [r[1] for r in rows], [json.loads(r[2]) for r in rows], mat)
    _VEC_CACHE[path] = (stamp, data)
    return data


def _vector_hits(path: str, q: str, n: int) -> list[tuple[str, float, str, dict]]:
    import numpy as np
    doc_ids, texts, metas, mat = _vectors(path)
    if mat is None:
        return []
    qv = np.asarray(list(_embedder().embed([q]))[0], dtype=np.float32)
    if qv.ndim != 1 or qv.size != mat.shape[1] or not np.isfinite(qv).all():
        raise RuntimeError("Query embedding is incompatible with the index; rebuild with refresh='full'")
    sims = mat @ (qv / (np.linalg.norm(qv) + 1e-9))
    top = np.argsort(-sims)[:n]
    return [(doc_ids[i], float(sims[i]), texts[i], metas[i]) for i in top]


def _passes(meta: dict[str, Any], text: str, filters: dict[str, Any] | None,
            keys: list[str] | None) -> bool:
    if keys and str(meta.get("key")) not in {str(k) for k in keys}:
        return False
    for field, want in (filters or {}).items():
        have = meta.get(field)
        if have is None and field == "text":
            have = text
        if have is None:
            return False
        if str(want).lower() not in str(have).lower():
            return False
    return True


def _why(text: str, q: str, limit: int = 3) -> list[str]:
    """The lines of the document that carry the query's words — the literal reason it matched."""
    terms = {t.lower() for t in _FTS_TOKEN.findall(q) if len(t) > 1}
    if not terms:
        return []
    out = []
    for line in text.splitlines():
        low = line.lower()
        if any(t in low for t in terms):
            out.append(line if len(line) <= 160 else line[:160] + "…")
        if len(out) >= limit:
            break
    return out


def _rrf(ranked: dict[str, list[str]], k: int, weights: dict[str, float] | None = None) -> dict[str, float]:
    """Reciprocal rank fusion. Weights let a retriever that is measurably better on a given corpus
    count for more — see eval.py, which sweeps them against a known-item query set."""
    fused: dict[str, float] = {}
    for name, docs in ranked.items():
        w = (weights or {}).get(name, 1.0)
        if not w:
            continue
        for rank, doc_id in enumerate(docs, 1):
            fused[doc_id] = fused.get(doc_id, 0.0) + w / (k + rank)
    return fused




# ------------------------------------------------------------------ SQL over decoded objects


def _index_for_pool(package: str, pool: str, db: str | None, app: int | None, chunk: str | None,
                    locale: str | None, refresh: str) -> tuple[dict[str, Any], dict[str, Any]]:
    spec = _index_spec(package, db, None, None, pool, chunk, locale or LOCALE, app)
    with _index_lock:
        info = _ensure_index(package, spec, refresh, embed=False)
    return spec, info


@tool()
def search(q: str, db: str | None = None, pool: str | None = None, table: str | None = None,
           columns: list[str] | None = None, chunk: str | None = None, limit: int = 20,
           mode: str = "auto", refresh: str = "auto", locale: str | None = None,
           app: int | None = None, filters: dict[str, str] | None = None,
           keys: list[str] | None = None, candidates: int = 50, k: int = 60,
           weights: dict[str, float] | None = None, package: str | None = None) -> dict:
    """Search a pool's decoded objects, or a plain table's columns.

    Either pool=<name> — every object is decoded and indexed as text with translations resolved to
    `locale` and references expanded to titles (chunk='Codes' indexes one document per element of
    that list) — or table=... columns=[...] for an ordinary table.

    mode:
      auto      keyword, plus embeddings when they are switched on (EMBEDDINGS=on) — the default
      keyword   bm25 only. Fast, and on short coded titles it is the strongest retriever here
      semantic  embeddings only
      hybrid    both, fused with weighted reciprocal rank

    Every hit reports `matched_by` (which retriever found it, at which rank) and `why` (the lines
    carrying your words, or a note that the match was semantic only). `filters` narrows by document
    metadata, e.g. {"parent_title": "Cause"}; `keys` restricts to specific #key values.
    refresh='auto' re-reads only records whose #lastChange moved; 'full' rebuilds; 'never' uses
    what is stored and reports how stale it is."""
    _positive_limit(limit)
    if mode not in ("auto", "keyword", "semantic", "hybrid"):
        raise ValueError("mode must be auto, keyword, semantic or hybrid")
    package = _pkg(package)
    spec = _index_spec(package, db, table, columns, pool, chunk, locale or LOCALE, app)
    want_vectors = mode in ("semantic", "hybrid") or (mode == "auto" and EMBEDDINGS)
    degraded = None

    with _index_lock:
        try:
            info = _ensure_index(package, spec, refresh, embed=want_vectors)
        except RuntimeError as e:
            if not want_vectors or "fastembed" not in str(e):
                raise
            degraded, want_vectors = str(e), False     # no fastembed: keyword still works
            info = _ensure_index(package, spec, refresh, embed=False)

        bm: list = [] if mode == "semantic" else _fts_search(spec["path"], q, candidates)
        bm_rows = {r[0]: {"score": round(-r[1], 3), "snippet": r[2], "meta": json.loads(r[3]), "text": r[4]}
                   for r in bm}
        vec_rows: dict[str, dict[str, Any]] = {}
        if want_vectors:
            try:
                for doc_id, score, text, meta in _vector_hits(spec["path"], q, candidates):
                    vec_rows[doc_id] = {"score": round(score, 4), "text": text, "meta": meta}
            except RuntimeError as e:
                degraded = str(e)

    ranked: dict[str, list[str]] = {}
    if bm:
        ranked["bm25"] = [r[0] for r in bm]
    if vec_rows:
        ranked["vector"] = list(vec_rows)
    if not ranked:
        return {"index": info, "mode": mode, "retrievers": [], "hits": [],
                **({"degraded": degraded} if degraded else {})}

    weights = weights or _HYBRID_WEIGHTS
    fused = _rrf(ranked, k, weights)
    order = sorted(fused, key=lambda d: -fused[d])

    hits, considered = [], 0
    for doc_id in order:
        src = bm_rows.get(doc_id) or vec_rows.get(doc_id)
        text, meta = src["text"], src["meta"]
        considered += 1
        if not _passes(meta, text, filters, keys):
            continue
        matched_by = []
        for name, docs in ranked.items():
            if doc_id in docs:
                raw = bm_rows[doc_id]["score"] if name == "bm25" else vec_rows[doc_id]["score"]
                matched_by.append({"retriever": name, "rank": docs.index(doc_id) + 1, "score": raw})
        why = _why(text, q)
        hits.append({"doc_id": doc_id, "score": round(fused[doc_id], 5), "matched_by": matched_by,
                     "why": why or (["semantic match — no shared words with the query"]
                                    if any(m["retriever"] == "vector" for m in matched_by) else []),
                     "snippet": bm_rows.get(doc_id, {}).get("snippet"), "meta": meta,
                     "text": text if len(text) <= 1200 else text[:1200] + "…"})
        if len(hits) >= limit:
            break

    out: dict[str, Any] = {"index": info, "mode": mode, "retrievers": list(ranked),
                           "weights": weights if len(ranked) > 1 else None,
                           "candidates_per_retriever": candidates, "considered": considered,
                           "hits": hits}
    if degraded:
        out["degraded"] = f"keyword-only: {degraded}"
    if mode == "auto" and not EMBEDDINGS:
        out["note"] = ("Keyword only: embeddings are off (EMBEDDINGS=on enables them). On this "
                       "data they measured no better than bm25 — see eval.py.")
    if filters or keys:
        out["filters_applied"] = {"filters": filters, "keys": keys,
                                  "note": "Filters are applied to the fused candidate list, not "
                                          "pushed into the retrievers: raise `candidates` if a "
                                          "filter is narrow."}
    return out


@tool()
def fields(pool: str, db: str | None = None, app: int | None = None, locale: str | None = None,
           refresh: str = "auto", limit: int = 300, min_filled: int = 0,
           package: str | None = None) -> dict:
    """Which field paths actually exist inside a pool's decoded objects, how many objects carry
    each one, and an example value. This is the map you need before writing query_decoded() SQL or
    entries(fields=..., filters=...) — a path that is present in 2 of 80 objects is a trap.
    '[*]' in a path means 'each element of this list'."""
    package = _pkg(package)
    spec, info = _index_for_pool(package, pool, db, app, None, locale, refresh)
    con = _open_ro(spec["path"], 60)
    try:
        objects = con.execute("SELECT COUNT(*) FROM decoded").fetchone()[0]
        rows = con.execute("SELECT path, kind, n, n_filled, example FROM paths "
                           "WHERE n_filled >= ? ORDER BY n_filled DESC, path LIMIT ?",
                           (min_filled, limit)).fetchall()
    finally:
        con.close()
    return {"pool": spec["pool"], "objects": objects, "index": info,
            "fields": [{"path": r[0], "kind": r[1], "present": r[2], "filled": r[3],
                        "fill_rate": round(r[3] / objects, 3) if objects else None,
                        "example": r[4]} for r in rows]}


_SQL_OK = re.compile(r"(?is)^\s*(with|select)\b")


@tool()
def query_decoded(sql: str, pool: str, db: str | None = None, app: int | None = None,
                  locale: str | None = None, refresh: str = "auto", limit: int = 200,
                  package: str | None = None) -> dict:
    """Run real SQL over a pool's *decoded* objects, including nested fields.

    The index keeps every object's decoded #value as JSON in a table `decoded(src_key, key, thing,
    last_change, title, json)`, so SQLite's json_each / json_extract work on it directly:

        SELECT json_extract(c.value, '$.Title') AS title, COUNT(*)
        FROM decoded d, json_each(d.json, '$.Codes') c GROUP BY 1 ORDER BY 2 DESC

    Also available in the same database: paths(path, kind, n, n_filled, example), docs(doc_id,
    src_key, text, meta), sources(src_key, last_change). Read-only, SELECT/WITH only.
    The data is the index's snapshot — the reply carries its age and staleness."""
    _positive_limit(limit)
    if not _SQL_OK.match(sql or ""):
        raise ValueError("query_decoded runs read-only queries: start with SELECT or WITH")
    package = _pkg(package)
    spec, info = _index_for_pool(package, pool, db, app, None, locale, refresh)
    con = _open_ro(spec["path"])
    try:
        res = _rows(con.execute(sql), limit)
    except sqlite3.Error as e:
        raise RuntimeError(f"SQL error over the decoded mirror: {e}") from e
    finally:
        con.close()
    res.update({"pool": spec["pool"], "source": "decoded mirror (index snapshot)",
                "as_of_age_s": info.get("age_s"), "index_status": info.get("status"),
                "stale_rows": info.get("stale_rows")})
    return res


_embedder_cache: dict[str, Any] = {}
_embedder_lock = threading.Lock()


def _embedder():
    """fastembed model, loaded once per process. Its download logging/progress bars are muted and
    routed away from stdout, which carries the MCP protocol."""
    name = _embedding_model()
    hit = _embedder_cache.get(name)
    if hit is not None:
        return hit
    import logging
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    os.environ.setdefault("TQDM_DISABLE", "1")
    for lg in ("httpx", "httpcore", "huggingface_hub", "fastembed"):
        logging.getLogger(lg).setLevel(logging.WARNING)
    try:
        from fastembed import TextEmbedding  # type: ignore
    except ImportError as e:
        raise RuntimeError("Semantic search needs `pip install fastembed` (small ONNX model, no GPU)") from e
    # One loader at a time: the stdout swap below is process-global, so two threads racing it
    # leave sys.stdout pointing at stderr for good — and both would download the model.
    with _embedder_lock:
        hit = _embedder_cache.get(name)
        if hit is not None:
            return hit
        return _load_embedder(name)


def _load_embedder(name: str):
    from fastembed import TextEmbedding  # type: ignore
    real_stdout = sys.stdout
    sys.stdout = sys.stderr            # anything the loader prints must not reach the MCP stream
    try:
        _embedder_cache[name] = TextEmbedding(model_name=name)
    except ValueError as e:
        supported = []
        try:
            supported = sorted({m["model"] for m in TextEmbedding.list_supported_models()})
        except Exception:
            pass
        multilingual = [m for m in supported if any(w in m.lower() for w in ("multilingual", "m3", "paraphrase"))]
        raise RuntimeError(
            f"EMBED_MODEL={name!r} is not available in this fastembed build: {e}. "
            + (f"Multilingual options here: {', '.join(multilingual[:6])}. " if multilingual else "")
            + f"{len(supported)} models supported in total.") from e
    finally:
        sys.stdout = real_stdout
    return _embedder_cache[name]


def _cli(argv: list[str]) -> int:
    """Manual testing without an MCP client:
         python3 android_db_mcp.py call backend_status
         python3 android_db_mcp.py call query db=app.db sql="SELECT count(*) FROM orders"
         python3 android_db_mcp.py call search db=app.db table=orders columns='["note"]' q=refund
       Values are parsed as JSON when possible, else kept as strings."""
    if len(argv) < 1 or argv[0] not in _TOOLS:
        print("tools: " + ", ".join(sorted(_TOOLS)), file=sys.stderr)
        return 2
    kwargs: dict[str, Any] = {}
    for kv in argv[1:]:
        k, _, v = kv.partition("=")
        try:
            kwargs[k] = json.loads(v)
        except json.JSONDecodeError:
            kwargs[k] = v
    try:
        result = _TOOLS[argv[0]](**kwargs)
    except Exception as e:  # show the same message Claude would get
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=1, default=str))
    return 0


def main() -> int:
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "call":
            return _cli(sys.argv[2:])
        mcp.run(transport="stdio")
        return 0
    finally:
        _shutdown_runners()


if __name__ == "__main__":
    sys.exit(main())
