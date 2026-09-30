#!/usr/bin/env python3
"""Timing harness for android-db-mcp.

Runs each tool the way an MCP client does — in one long-lived process, so the persistent
device runner and in-process caches behave as they do in a real session.

    python3 bench.py run --label before --reps 5
    python3 bench.py compare .bench/before.json .bench/after.json

Each scenario records median / min / p90 wall time over `reps` repetitions. Scenarios marked
cold=True wipe the caches they depend on before *every* repetition, so a cold number is the
first-call cost a fresh Claude session pays; warm numbers are every call after that.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import traceback
from pathlib import Path

# ANDROID_DB_SRC lets one harness measure another checkout (e.g. the pre-change baseline),
# so before/after numbers come from identical scenarios and timing code.
sys.path.insert(0, os.environ.get("ANDROID_DB_SRC") or str(Path(__file__).resolve().parent))
import android_db_mcp as A  # noqa: E402

BENCH_DIR = Path(os.environ.get("BENCH_DIR") or (Path.cwd() / ".bench"))


# --------------------------------------------------------------------------- helpers

def _time(fn):
    t0 = time.perf_counter()
    try:
        out = fn()
        return time.perf_counter() - t0, out, None
    except Exception as e:
        if os.environ.get("BENCH_DEBUG"):
            traceback.print_exc()
        return time.perf_counter() - t0, None, f"{type(e).__name__}: {e}"


def _cache_dir(pkg: str) -> Path:
    return getattr(A, "_pkg_cache", lambda p: A.CACHE / p)(pkg)


def wipe(pkg: str, *, discovery: bool = False, indexes: bool = False, snapshots: bool = False) -> None:
    """Drop the on-disk caches a cold scenario must not benefit from."""
    d = _cache_dir(pkg)
    if not d.exists():
        return
    for f in d.iterdir():
        name = f.name
        if discovery and (name in ("databases.json", "bo_databases.json") or name.startswith("pools_")
                          or name.startswith("refmap_") or name == "context.json"):
            f.unlink(missing_ok=True)
        if indexes and name.startswith("index_"):
            f.unlink(missing_ok=True)
        if snapshots and (name.endswith(".meta.json") or "__" in name):
            f.unlink(missing_ok=True)
    if discovery:
        A._pool_cache.clear()
        A._detected.pop("package", None)


def _pct(values: list[float], q: float) -> float:
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    i = min(len(s) - 1, int(round(q * (len(s) - 1))))
    return s[i]


# --------------------------------------------------------------------------- targets

def discover() -> dict:
    """Pick real targets on the attached device so the numbers mean something."""
    pkg = A._pkg(None)
    dbs = A.list_databases(pkg)
    bo = A._bo_databases(pkg)
    db = bo[0] if bo else (dbs[0] if dbs else None)
    pool_rows: list[tuple[str, int]] = []
    if db:
        for p in A.pools(db=db):
            if p.get("rows"):
                pool_rows.append((p["name"], p["rows"]))
    pool_rows.sort(key=lambda pr: -pr[1])
    big = pool_rows[0][0] if pool_rows else None
    # something small enough that a full index rebuild is measurable but not punishing
    small = next((n for n, r in reversed(pool_rows) if r >= 5), big)
    table = None
    if db:
        r = A.query("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name LIMIT 1", db=db, limit=1)
        table = r["rows"][0][0] if r["rows"] else None
    return {"package": pkg, "databases": len(dbs), "db": db, "bo_databases": bo,
            "big_pool": big, "small_pool": small, "table": table,
            "pools_with_rows": len(pool_rows), "top_pools": pool_rows[:8]}


def scenarios(t: dict) -> list[dict]:
    pkg, db, big, small = t["package"], t["db"], t["big_pool"], t["small_pool"]
    S: list[dict] = []

    def add(name, fn, reps=None, cold=None, note=""):
        S.append({"name": name, "fn": fn, "reps": reps, "cold": cold or {}, "note": note})

    add("devices", lambda: A.devices())
    add("detect_package", lambda: A.detect_package())
    add("backend_status", lambda: A.backend_status())
    add("list_databases (warm)", lambda: A.list_databases(pkg))
    add("list_databases (cold)", lambda: A.list_databases(pkg), reps=3, cold={"discovery": True})
    if db:
        add("query: SELECT 1", lambda: A.query("SELECT 1", db=db, limit=1))
        add("query: count sqlite_master", lambda: A.query("SELECT COUNT(*) FROM sqlite_master", db=db, limit=1))
        add("schema (no stats)", lambda: A.schema(db=db, stats=False), reps=3)
        add("schema (stats)", lambda: A.schema(db=db, stats=True), reps=3)
        add("pools (warm)", lambda: A.pools(db=db))
        add("pools (cold)", lambda: A.pools(db=db), reps=2, cold={"discovery": True})
    if t["table"]:
        add("sample 5 rows", lambda: A.sample(table=t["table"], db=db, n=5))
    if big:
        add(f"entries limit=50 [{big}]", lambda: A.entries(pool=big, db=db, limit=50))
        add(f"entries limit=5 [{big}]", lambda: A.entries(pool=big, db=db, limit=5))
    if small:
        add(f"search warm [{small}]", lambda: A.search(q="a", pool=small, db=db, limit=10))
        add(f"index build full [{small}]", lambda: A.search(q="a", pool=small, db=db, limit=10, refresh="full"),
            reps=2, note="full rebuild, no embeddings")
        add(f"index refresh auto [{small}]", lambda: A.search(q="a", pool=small, db=db, limit=10, refresh="auto"),
            note="incremental check, nothing changed")
        if os.environ.get("BENCH_EMBED", "1") != "0":
            add(f"semantic_index [{small}]", lambda: A.semantic_index(pool=small, db=db, refresh="auto"), reps=2)
            add(f"semantic_search [{small}]", lambda: A.semantic_search(q="status", pool=small, db=db, limit=10))
    # optional tools, only if this build has them
    if "app_context" in A._TOOLS:
        add("app_context (warm)", lambda: A._TOOLS["app_context"](), note="new")
        add("app_context (cold)", lambda: A._TOOLS["app_context"](), reps=2,
            cold={"discovery": True}, note="new; full discovery")
    if small and "refs" in A._TOOLS:
        add(f"refs [{small}]", lambda: A._TOOLS["refs"](pool=small, db=db), note="new; cached map")
        add(f"refs cold [{small}]", lambda: A._TOOLS["refs"](pool=small, db=db, refresh=True), reps=2,
            note="new; sample + verify")
    if small and "hybrid_search" in A._TOOLS:
        add(f"hybrid_search [{small}]", lambda: A._TOOLS["hybrid_search"](q="status", pool=small, db=db, limit=10),
            note="new; bm25 + vectors")
    if small and "fields" in A._TOOLS:
        add(f"fields [{small}]", lambda: A._TOOLS["fields"](pool=small, db=db), note="new")
    if small and "query_decoded" in A._TOOLS:
        add(f"query_decoded [{small}]",
            lambda: A._TOOLS["query_decoded"](sql="SELECT COUNT(*) FROM decoded", pool=small, db=db), note="new")
    if big and "entries" in A._TOOLS:
        add(f"entries fields+filters [{big}]",
            lambda: A._TOOLS["entries"](pool=big, db=db, fields=["Title"], filters={"Title": {"op": "exists"}},
                                        limit=20, scan=200), note="new; post-decode")
    if "mark" in A._TOOLS:
        add("mark (all pools)", lambda: A._TOOLS["mark"](label="bench"), reps=2, note="new")
        add("changes_since (no changes)", lambda: A._TOOLS["changes_since"](limit=20), reps=2, note="new")
    return S


# --------------------------------------------------------------------------- run

def run(label: str, reps: int, only: str | None, warmup: int = 1) -> dict:
    t0 = time.time()
    targets = discover()
    out = {"label": label, "when": time.strftime("%Y-%m-%d %H:%M:%S"), "reps": reps, "warmup": warmup,
           "source": os.environ.get("ANDROID_DB_SRC") or ".",
           "targets": targets, "env": {k: os.environ.get(k) for k in
                                       ("ANDROID_DB_RUNNER", "ANDROID_DB_BACKEND", "ANDROID_DB_PERSIST",
                                        "EMBED_MODEL", "ANDROID_SERIAL")},
           "discover_s": round(time.time() - t0, 3), "results": []}
    for sc in scenarios(targets):
        if only and only.lower() not in sc["name"].lower():
            continue
        n = sc["reps"] or reps
        times, err, last = [], None, None
        for _ in range(warmup):          # first call pays one-off costs (runner start, caches)
            if sc["cold"]:
                wipe(targets["package"], **sc["cold"])
            _time(sc["fn"])
        for _ in range(n):
            if sc["cold"]:
                wipe(targets["package"], **sc["cold"])
            dt, res, e = _time(sc["fn"])
            if e:
                err = e
                break
            times.append(dt)
            last = res
        row = {"name": sc["name"], "reps": len(times), "note": sc["note"]}
        if err or not times:
            row.update({"ok": False, "error": err})
        else:
            row.update({"ok": True, "median": round(statistics.median(times), 4),
                        "mean": round(statistics.fmean(times), 4),
                        "min": round(min(times), 4), "p90": round(_pct(times, 0.9), 4),
                        "size": _size(last)})
        out["results"].append(row)
        flag = "" if row.get("ok") else "  FAILED"
        print(f"{row['name']:<42} {row.get('median', 0):8.3f}s  (min {row.get('min', 0):.3f}"
              f"  p90 {row.get('p90', 0):.3f})  n={row['reps']}{flag}", flush=True)
        if not row.get("ok"):
            print(f"    {row['error']}", flush=True)
    BENCH_DIR.mkdir(exist_ok=True)
    path = BENCH_DIR / f"{label}.json"
    path.write_text(json.dumps(out, indent=1, default=str))
    print(f"\nwrote {path}")
    return out


def _size(res) -> int | None:
    """Rough result size, so a speed-up that silently drops data is visible."""
    try:
        if isinstance(res, dict):
            for k in ("rows", "hits", "tables", "docs"):
                v = res.get(k)
                if isinstance(v, list):
                    return len(v)
                if isinstance(v, int):
                    return v
        if isinstance(res, list):
            return len(res)
    except Exception:
        pass
    return None


# --------------------------------------------------------------------------- compare

def compare(a_path: str, b_path: str) -> int:
    a, b = json.loads(Path(a_path).read_text()), json.loads(Path(b_path).read_text())
    ax = {r["name"]: r for r in a["results"]}
    bx = {r["name"]: r for r in b["results"]}
    names = list(ax) + [n for n in bx if n not in ax]
    w = max(len(n) for n in names) + 1
    print(f"{'scenario':<{w}} {a['label']:>11} {b['label']:>11} {'change':>10}   size")
    print("-" * (w + 40))
    worse = []
    for n in names:
        ra, rb = ax.get(n), bx.get(n)
        key = os.environ.get("BENCH_METRIC", "min")
        sa = f"{ra[key]:.3f}s" if ra and ra.get("ok") else ("—" if not ra else "FAIL")
        sb = f"{rb[key]:.3f}s" if rb and rb.get("ok") else ("—" if not rb else "FAIL")
        chg, size = "", ""
        if ra and rb and ra.get("ok") and rb.get("ok"):
            ra, rb = {**ra, "median": ra[key]}, {**rb, "median": rb[key]}
            d = (rb["median"] - ra["median"]) / ra["median"] * 100 if ra["median"] else 0
            chg = f"{d:+.0f}%" if abs(d) >= 1 else "—"
            if rb["median"] > ra["median"] * 1.25 and rb["median"] - ra["median"] > 0.05:
                chg += " !"
                worse.append((n, ra["median"], rb["median"]))
            if ra.get("size") != rb.get("size"):
                size = f"{ra.get('size')} -> {rb.get('size')}"
        print(f"{n:<{w}} {sa:>11} {sb:>11} {chg:>10}   {size}")
    if worse:
        print("\nREGRESSIONS (>25% slower):")
        for n, x, y in worse:
            print(f"  {n}: {x:.3f}s -> {y:.3f}s")
    return 1 if worse else 0


def ab(before_src: str, rounds: int, reps: int) -> int:
    """Interleave two checkouts round by round.

    An emulator drifts: the same call can take 0.5s now and 1.3s two minutes later, which makes
    'run all of A, then all of B' produce imaginary regressions. Alternating A/B/A/B and keeping
    the best time per scenario cancels the drift."""
    import subprocess
    BENCH_DIR.mkdir(exist_ok=True)
    runs: dict[str, list[str]] = {"before": [], "after": []}
    for r in range(1, rounds + 1):
        for side, src in (("before", before_src), ("after", "")):
            label = f"ab-{side}-{r}"
            env = dict(os.environ, BENCH_DIR=str(BENCH_DIR))
            env["ANDROID_DB_SRC"] = src or str(Path(__file__).resolve().parent)
            print(f"--- round {r}: {side}", flush=True)
            subprocess.run([sys.executable, str(Path(__file__).resolve()), "run",
                            "--label", label, "--reps", str(reps), "--warmup", "1"],
                           env=env, stdout=subprocess.DEVNULL)
            runs[side].append(label)

    def best(labels: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for lb in labels:
            f = BENCH_DIR / f"{lb}.json"
            if not f.is_file():
                continue
            for row in json.loads(f.read_text())["results"]:
                if not row.get("ok"):
                    continue
                cur = out.get(row["name"])
                if cur is None or row["min"] < cur["min"]:
                    out[row["name"]] = row
        return out

    a, b = best(runs["before"]), best(runs["after"])
    names = list(a) + [n for n in b if n not in a]
    w = max(len(n) for n in names) + 1
    print(f"\nbest of {rounds} interleaved rounds x {reps} reps\n")
    print(f"{'scenario':<{w}} {'before':>9} {'after':>9} {'change':>9}")
    print("-" * (w + 31))
    worse = []
    for n in names:
        ra, rb = a.get(n), b.get(n)
        sa = f"{ra['min']:.3f}s" if ra else "—"
        sb = f"{rb['min']:.3f}s" if rb else "—"
        chg = ""
        if ra and rb and ra["min"]:
            d = (rb["min"] - ra["min"]) / ra["min"] * 100
            chg = f"{d:+.0f}%"
            if rb["min"] > ra["min"] * 1.25 and rb["min"] - ra["min"] > 0.02:
                worse.append((n, ra["min"], rb["min"]))
                chg += " !"
        print(f"{n:<{w}} {sa:>9} {sb:>9} {chg:>9}")
    if worse:
        print("\nREGRESSIONS:")
        for n, x, y in worse:
            print(f"  {n}: {x:.3f}s -> {y:.3f}s")
    else:
        print("\nno regressions")
    (BENCH_DIR / "ab-summary.json").write_text(json.dumps(
        {"rounds": rounds, "reps": reps,
         "before": {n: r["min"] for n, r in a.items()}, "after": {n: r["min"] for n, r in b.items()}}, indent=1))
    return 1 if worse else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--label", required=True)
    r.add_argument("--reps", type=int, default=5)
    r.add_argument("--warmup", type=int, default=1)
    r.add_argument("--only")
    c = sub.add_parser("compare")
    c.add_argument("before")
    c.add_argument("after")
    x = sub.add_parser("ab")
    x.add_argument("--before-src", required=True, help="path to the baseline checkout")
    x.add_argument("--rounds", type=int, default=3)
    x.add_argument("--reps", type=int, default=5)
    args = ap.parse_args()
    try:
        if args.cmd == "run":
            run(args.label, args.reps, args.only, args.warmup)
            return 0
        if args.cmd == "ab":
            return ab(args.before_src, args.rounds, args.reps)
        return compare(args.before, args.after)
    finally:
        A._shutdown_runners()


if __name__ == "__main__":
    sys.exit(main())
