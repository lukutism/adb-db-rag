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


def _jar_available(package: str) -> bool:
    if BACKEND == "snapshot":
        return False
    if package not in _jar_state:
        out = _in_sandbox(package, f"test -f {_jar_path(package)} && echo yes || echo no", check=False)
        out2 = _as_shell("test -f /data/local/tmp/dbq.jar && echo yes || echo no", check=False)
        _jar_state[package] = "yes" in str(out) and "yes" in str(out2)
        if BACKEND == "jar" and not _jar_state[package]:
            raise RuntimeError("ANDROID_DB_BACKEND=jar but dbq.jar is not deployed for "
                               f"{package}; call deploy_runner or run runner/deploy.sh")
    return _jar_state[package]


PERSIST = os.environ.get("ANDROID_DB_PERSIST", "1") != "0"
_servers: dict[tuple[str, str, str, str], "_Server"] = {}
_servers_lock = threading.RLock()


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


def _shutdown_runners() -> None:
    with _servers_lock:
        servers = list(_servers.values())
        _servers.clear()
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
            serial = SERIAL or str(_adb("get-serialno")).strip()
            if not serial or serial == "unknown":
                raise RuntimeError("No Android device selected")
            key = (ADB, serial, package, ident)
            with _servers_lock:
                srv = _servers.get(key)
                if srv is None or srv.proc.poll() is not None:
                    if srv is not None:
                        srv.close()
                    _servers.pop(key, None)
                    srv = None
                    srv = _servers[key] = _Server(ident, package, serial)
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


def _snapshot(package: str, db: str, refresh: bool = True, require_consistent: bool = False) -> dict[str, Any]:
    package, db = _pkg(package), _safe_db_name(db)
    src = _db_path(package, db)
    local_dir = CACHE / package
    local_dir.mkdir(parents=True, exist_ok=True)
    local = local_dir / _cache_name(db)
    meta = local_dir / f"{_cache_name(db)}.meta.json"

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
    if _jar_available(package):
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
    d = CACHE / package
    d.mkdir(parents=True, exist_ok=True)
    return d / name


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
def detect_package() -> dict:
    """Work out which app to talk to when no package was given: ANDROID_PACKAGE, else the
    applicationId in the project's android/app/build.gradle, else the app in the foreground on
    the device. Returns the package and where it came from."""
    _detected.clear()
    p = _pkg(None)
    return {"package": p, "source": "ANDROID_PACKAGE env" if DEFAULT_PKG else _detected.get("source", "explicit")}


@tool()
def backend_status(package: str | None = None) -> dict:
    """Which backend will be used for this package (jar runner inside the app sandbox, or
    host-side snapshot), plus runner mode and on-device sqlite3 availability."""
    package = _pkg(package)
    _jar_state.pop(package, None)
    return {"package": package, "package_source": "ANDROID_PACKAGE env" if DEFAULT_PKG else _detected.get("source", "explicit"),
            "runner": RUNNER, "backend_setting": BACKEND,
            "jar_deployed": _jar_available(package), "device_sqlite3": _device_has_sqlite3(package),
            "effective_backend": "jar" if _jar_available(package) else "snapshot"}


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
    _jar_state.pop(package, None)
    ok = _jar_available(package)
    dbs = list_databases(package) if ok else []
    smoke = _device_sql(package, dbs[0], "SELECT sqlite_version()", limit=1) if dbs else None
    return {"deployed": ok, "runner": RUNNER, "smoke_test": smoke}


@tool()
def snapshot(db: str, package: str | None = None, require_consistent: bool = False) -> dict:
    """Pull a fresh database copy. Reports consistent=True for SQLite .backup, otherwise a
    best-effort raw DB/WAL copy with a warning. require_consistent=True refuses raw copying."""
    return _snapshot(package, db, refresh=True, require_consistent=require_consistent)


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
            if sel is None or (batch and size + len(sel) > 24000):
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
    Results are JSON rows; large results are truncated to `limit`."""
    return _read(package, db, sql, limit, refresh)


@tool()
def sample(table: str, db: str, package: str | None = None, n: int = 5) -> dict:
    """Show the first `n` rows of a table (quick look at real data shapes)."""
    return _read(package, db, f"SELECT * FROM {_q(table)}", limit=_positive_limit(n))


@tool()
def execute(sql: str, db: str, package: str | None = None) -> dict:
    """Run a WRITE statement (INSERT/UPDATE/DELETE/DDL) on the live device database, so the
    running app sees the change. Uses the jar runner if deployed, else on-device sqlite3.
    Use with care — this mutates live app data."""
    package, db = _pkg(package), _safe_db_name(db)
    if _jar_available(package):
        return _device_sql(package, db, sql, "write")
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
    if not refresh and key in _pool_cache:
        return _pool_cache[key]
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


def _pool_map(package: str, db: str) -> dict[tuple[int, int], str]:
    return {(p["applicationId"], p["poolId"]): p["name"] for p in _pools_in(package, db)}


def _bo_databases(package: str) -> list[str]:
    cache = _disk_cache(package, "bo_databases.json")
    hit = _cached_json(cache, _LIST_TTL)
    if hit is not None:
        return hit
    out = [d for d in list_databases(package) if _has_table(package, d, "BusinessObjectPool")]
    cache.write_text(json.dumps(out))
    return out


def _resolve_pool(package: str, pool: str, db: str | None = None, app: int | None = None) -> dict[str, Any]:
    """Pool name (case-insensitive) or entry table name → {db, table, name, applicationId, poolId}."""
    if pool.startswith(ENTRY_PREFIX):
        app_id, pool_id = _parse_entry_table(pool)
        if db is None:
            raise ValueError("db is required when addressing a pool by table name")
        return {"db": db, "table": pool, "name": _pool_map(package, db).get((app_id, pool_id)),
                "applicationId": app_id, "poolId": pool_id}
    dbs = [db] if db else _bo_databases(package)
    hits = [p for d in dbs for p in _pools_in(package, d)
            if p["name"].lower() == pool.lower() and (app is None or p["applicationId"] == app)]
    if not hits:
        known = sorted({p["name"] for d in dbs for p in _pools_in(package, d)})
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
def pools(db: str | None = None, package: str | None = None, refresh: bool = False) -> list[dict]:
    """osapiens business-object pools: pool name → entry table (BusinessObjectEntry#app#pool),
    applicationId, poolId, row count, and the promoted (named) fields with their JSON paths.
    In a field's `path`, 'default' means "each element of this list" (Codes.default.Title.default.Text
    = Codes[*].Title[*].Text); such list-derived columns are usually left empty by the app, so read
    the decoded #value instead. Scans every database with a BusinessObjectPool table unless `db`
    is given. Use the `name` with entries()/search()/semantic_index(pool=...)."""
    package = _pkg(package)
    out = []
    if refresh:
        list_databases(package, refresh=True)
        _disk_cache(package, "bo_databases.json").unlink(missing_ok=True)
    for d in ([db] if db else _bo_databases(package)):
        ps = [dict(p) for p in _pools_in(package, d, refresh)]
        existing = [p["table"] for p in ps if p["exists"]]
        counts: dict[str, int] = {}
        for i in range(0, len(existing), 60):          # one UNION ALL per 60 tables
            q = " UNION ALL ".join(f"SELECT {_sq(t)}, COUNT(*) FROM {_q(t)}" for t in existing[i:i + 60])
            counts.update({r[0]: r[1] for r in _read(package, d, q, limit=100000)["rows"]})
        fields = _all_named_fields(package, d)
        for p in ps:
            p["rows"] = counts.get(p["table"], 0)
            p["named_fields"] = fields.get((p["applicationId"], p["poolId"]), [])
            out.append(p)
    return out


def _decode_cell(v: Any) -> Any:
    raw = _cell_bytes(v)
    if raw is None:
        return v
    try:
        return bo_codec.decode(raw)
    except Exception as e:
        return {"$undecoded": f"{e}", "$bytes": len(raw)}


@tool()
def entries(pool: str, db: str | None = None, app: int | None = None, keys: list[str] | None = None,
            where: str | None = None, limit: int = 50, package: str | None = None,
            locale: str | None = None, raw: bool = False, after_rowid: int | None = None) -> dict:
    """Read business objects from a pool by NAME (e.g. 'CodesGroup'), with the #value BLOB decoded
    into JSON. Filter with keys=[...] (exact #key values — use this to follow references from
    another object) and/or a SQL `where` over the promoted columns / #lastChange. Each row carries
    key, thingId, lastChange, the promoted columns, `value` (decoded) and `title` (display label
    in `locale`, default LOCALE env / 'en'). raw=True also returns the base64 BLOB.
    Ordered by rowid; pass next_after_rowid as after_rowid to get the next page. Pages are live
    reads, not a transaction spanning all pages; rowid tables are required."""
    _need_codec()
    _positive_limit(limit)
    package = _pkg(package)
    loc = locale or LOCALE
    p = _resolve_pool(package, pool, db, app)
    conds = []
    if keys:
        conds.append('"#key" IN (' + ", ".join(_sq(str(k)) for k in keys) + ")")
    if where:
        conds.append(f"({where})")
    if after_rowid is not None:
        conds.append(f"rowid > {int(after_rowid)}")
    sql = (f'SELECT rowid, * FROM {_q(p["table"])}' + (" WHERE " + " AND ".join(conds) if conds else "")
           + f" ORDER BY rowid LIMIT {limit + 1}")
    r = _read(package, p["db"], sql, limit=limit, blobs="base64")
    cols = r["columns"]
    rows = []
    for row in r["rows"]:
        rec = dict(zip(cols, row))
        value = _decode_cell(rec.get("#value"))
        item: dict[str, Any] = {"rowid": rec["rowid"], "key": rec.get("#key"), "thingId": rec.get("#thingId"), "lastChange": rec.get("#lastChange"),
                                "columns": {k: v for k, v in rec.items() if not k.startswith("#") and k != "rowid"},
                                "title": bo_codec.display_title(value, loc), "value": value}
        if raw:
            item["raw_b64"] = rec.get("#value", {}).get("$b64") if isinstance(rec.get("#value"), dict) else None
        rows.append(item)
    return {"pool": p["name"], "table": p["table"], "db": p["db"], "applicationId": p["applicationId"],
            "poolId": p["poolId"], "backend": r.get("backend"), "row_count": len(rows), "truncated": r.get("truncated"),
            "next_after_rowid": rows[-1]["rowid"] if r.get("truncated") and rows else None,
            "rows": rows}


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
    return {"table": table, "column": column, "backend": r.get("backend"), "truncated": r.get("truncated"),
            "next_after_rowid": r["rows"][-1][0] if r.get("truncated") and r["rows"] else None,
            "rows": [{"rowid": row[0], "value": _decode_cell(row[1])} for row in r["rows"]]}


# ------------------------------------------------------------------ retrieval indexes
#
# One SQLite file per index (FTS5 + optional embedding vectors), keyed by what was indexed.
# Documents use the same read backend as query(). Pool source rows are tracked independently
# of chunks, including rows producing no documents. NULL change markers are always re-read.

_AUTO_TTL = int(os.environ.get("INDEX_TTL", "120"))   # plain-table indexes: rebuild when older than this
_INDEX_VERSION = 2
_index_lock = threading.RLock()


def _embedding_model() -> str:
    return os.environ.get("EMBED_MODEL", "BAAI/bge-small-en-v1.5")


def _index_spec(package: str, db: str, table: str | None, columns: list[str] | None, pool: str | None,
                chunk: str | None, locale: str, app: int | None) -> dict[str, Any]:
    if pool:
        p = _resolve_pool(package, pool, db, app)
        spec = {"kind": "pool", "db": p["db"], "table": p["table"], "pool": p["name"],
                "applicationId": p["applicationId"], "poolId": p["poolId"], "chunk": chunk or "", "locale": locale}
    else:
        if not (db and table and columns):
            raise ValueError("Give pool=<name>, or db=... table=... with columns=[...]")
        spec = {"kind": "table", "db": db, "table": table, "columns": list(columns), "chunk": "", "locale": locale}
    import hashlib
    h = hashlib.sha1(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:12]
    spec["path"] = str(CACHE / package / f"index_{h}.sqlite")
    return spec


def _open_index(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=120)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS docs(doc_id TEXT PRIMARY KEY, src_key TEXT, last_change INTEGER,
                                        text TEXT, meta TEXT, vec BLOB);
        CREATE INDEX IF NOT EXISTS docs_src ON docs(src_key);
        CREATE TABLE IF NOT EXISTS sources(src_key TEXT PRIMARY KEY, last_change INTEGER);
        CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(text, doc_id UNINDEXED);
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


def _docs_from_pool(package: str, spec: dict[str, Any],
                    keys: list[tuple[Any, Any]] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
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
    docs = []
    sources = {}
    for key, thing, last_change, blob in rows:
        src_key = _source_key(key, thing)
        sources[src_key] = last_change
        value = _decode_cell(blob)
        for suffix, unit, ctx in bo_codec.chunks(value, spec["chunk"] or None, spec["locale"]):
            head = [f"pool: {spec['pool']}", f"key: {key}"]
            if ctx.get("parent_title"):
                head.append(f"parent: {ctx['parent_title']}")
            text = "\n".join(head + [bo_codec.to_text(unit, spec["locale"])])
            meta = {"key": key, "thingId": thing, "applicationId": spec["applicationId"], "poolId": spec["poolId"],
                    "title": bo_codec.display_title(unit, spec["locale"]), **ctx}
            docs.append({"doc_id": src_key + suffix, "src_key": src_key, "last_change": last_change, "text": text, "meta": meta})
    return docs, sources


def _docs_from_table(package: str, spec: dict[str, Any]) -> list[dict[str, Any]]:
    cols = spec["columns"]
    rows = _index_read(package, spec, f"SELECT rowid, {', '.join(_q(c) for c in cols)} FROM {_q(spec['table'])}")
    return [{"doc_id": str(row[0]), "src_key": str(row[0]), "last_change": None,
             "text": "\n".join(f"{c}: {v}" for c, v in zip(cols, row[1:]) if v is not None and not isinstance(v, dict)),
             "meta": {"rowid": row[0]}} for row in rows]


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


def _upsert_docs(con: sqlite3.Connection, docs: list[dict[str, Any]]) -> None:
    for d in docs:
        con.execute("INSERT INTO docs(doc_id, src_key, last_change, text, meta, vec) VALUES (?,?,?,?,?,?)",
                    (d["doc_id"], d["src_key"], d["last_change"], d["text"], json.dumps(d["meta"]), None))
        con.execute("INSERT INTO fts(text, doc_id) VALUES (?, ?)", (d["text"], d["doc_id"]))


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
            if spec["kind"] == "pool":
                docs, sources = _docs_from_pool(package, spec)
                con.executemany("INSERT INTO sources VALUES (?, ?)", sources.items())
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
                docs, sources = _docs_from_pool(package, spec, [(live[k][0], live[k][1]) for k in changed])
                _upsert_docs(con, docs)
                con.executemany("INSERT INTO sources VALUES (?, ?)", sources.items())
            if changed or gone:
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


@tool()
def search(q: str, db: str | None = None, pool: str | None = None, table: str | None = None,
           columns: list[str] | None = None, chunk: str | None = None, limit: int = 20,
           refresh: str = "auto", locale: str | None = None, app: int | None = None,
           package: str | None = None) -> dict:
    """Full-text (FTS5, bm25) search. Either pool=<name> — every business object is decoded and
    indexed as text with translations resolved to `locale` (chunk='Codes' indexes one document per
    element of that list, with the parent's title) — or table=... columns=[...] for plain tables.
    The index uses the selected read backend. refresh='auto' checks per-record #lastChange
    values (pools) or rebuilds after INDEX_TTL seconds (tables); 'full' rebuilds; 'never' retains
    stored documents. Replies include age, last check time and embedding coverage. For pools,
    'never' reports changed and deleted source counts; other stale counts are unknown (null)."""
    _positive_limit(limit)
    package = _pkg(package)
    spec = _index_spec(package, db, table, columns, pool, chunk, locale or LOCALE, app)
    with _index_lock:
        info = _ensure_index(package, spec, refresh, embed=False)
        con = sqlite3.connect(spec["path"], timeout=120)
        try:
            rows = con.execute("SELECT fts.doc_id, bm25(fts) AS score, snippet(fts, 0, '[', ']', '…', 24), d.meta, d.text "
                               "FROM fts JOIN docs d ON d.doc_id = fts.doc_id WHERE fts MATCH ? ORDER BY score LIMIT ?", (q, limit)).fetchall()
        finally:
            con.close()
        hits = [{"doc_id": r[0], "score": round(-r[1], 3), "snippet": r[2], "meta": json.loads(r[3]),
                 "text": r[4] if len(r[4]) <= 1200 else r[4][:1200] + "…"} for r in rows]
    return {"index": info, "hits": hits}


@tool()
def semantic_index(db: str | None = None, pool: str | None = None, table: str | None = None,
                   columns: list[str] | None = None, chunk: str | None = None, refresh: str = "auto",
                   locale: str | None = None, app: int | None = None, package: str | None = None) -> dict:
    """Build or refresh an embedding index (pip install fastembed) over a pool (decoded objects,
    translations resolved, optional chunk='Codes' for one vector per embedded element) or over
    plain table columns. Incremental like search(): new/changed records are refreshed and only
    missing vectors are embedded. Changing EMBED_MODEL regenerates vectors; 'never' retains
    documents but still repairs missing/incompatible vectors. Then use semantic_search()."""
    package = _pkg(package)
    spec = _index_spec(package, db, table, columns, pool, chunk, locale or LOCALE, app)
    return _ensure_index(package, spec, refresh, embed=True)


@tool()
def semantic_search(q: str, db: str | None = None, pool: str | None = None, table: str | None = None,
                    columns: list[str] | None = None, chunk: str | None = None, limit: int = 10,
                    refresh: str = "auto", locale: str | None = None, app: int | None = None,
                    package: str | None = None) -> dict:
    """Meaning-based search over an index built by semantic_index (same arguments). Returns the
    best-matching documents with cosine score, their metadata (key, title, parent) and the index
    age / staleness."""
    _positive_limit(limit)
    import numpy as np
    package = _pkg(package)
    spec = _index_spec(package, db, table, columns, pool, chunk, locale or LOCALE, app)
    with _index_lock:
        info = _ensure_index(package, spec, refresh, embed=True)
        con = sqlite3.connect(spec["path"], timeout=120)
        try:
            rows = con.execute("SELECT doc_id, text, meta, vec FROM docs WHERE vec IS NOT NULL").fetchall()
        finally:
            con.close()
    if not rows:
        return {"index": info, "hits": []}
    vecs = np.stack([np.frombuffer(r[3], dtype=np.float32) for r in rows])
    qv = np.asarray(list(_embedder().embed([q]))[0], dtype=np.float32)
    if qv.ndim != 1 or qv.size != vecs.shape[1] or not np.isfinite(qv).all():
        raise RuntimeError("Query embedding is incompatible with the index; rebuild with refresh='full'")
    sims = vecs @ qv / (np.linalg.norm(vecs, axis=1) * np.linalg.norm(qv) + 1e-9)
    top = np.argsort(-sims)[:limit]
    return {"index": info, "hits": [{"doc_id": rows[i][0], "score": round(float(sims[i]), 4), "meta": json.loads(rows[i][2]),
                                     "text": rows[i][1] if len(rows[i][1]) <= 1200 else rows[i][1][:1200] + "…"} for i in top]}


_embedder_cache: dict[str, Any] = {}


def _embedder():
    """fastembed model, loaded once per process. Its download logging/progress bars are muted and
    routed away from stdout, which carries the MCP protocol."""
    name = _embedding_model()
    if name in _embedder_cache:
        return _embedder_cache[name]
    import logging
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    os.environ.setdefault("TQDM_DISABLE", "1")
    for lg in ("httpx", "httpcore", "huggingface_hub", "fastembed"):
        logging.getLogger(lg).setLevel(logging.WARNING)
    try:
        from fastembed import TextEmbedding  # type: ignore
    except ImportError as e:
        raise RuntimeError("Semantic search needs `pip install fastembed` (small ONNX model, no GPU)") from e
    real_stdout = sys.stdout
    sys.stdout = sys.stderr            # anything the loader prints must not reach the MCP stream
    try:
        _embedder_cache[name] = TextEmbedding(model_name=name)
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
