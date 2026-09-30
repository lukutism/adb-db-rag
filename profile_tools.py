#!/usr/bin/env python3
"""Per-function profiling for android-db-mcp.

    python3 profile_tools.py            # every scenario, cold then warm
    python3 profile_tools.py --only entries --reps 5

Wall time alone is misleading here: most tools spend their time waiting for the device, and host
CPU spent while waiting is free. Each scenario is therefore reported as

    wall = device wait (select/read on the runner pipe, or an adb subprocess) + host CPU

and the ranking at the end is by host CPU only, because that is the part worth optimising.
"""
from __future__ import annotations

import argparse
import cProfile
import io
import json
import os
import pstats
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import android_db_mcp as A  # noqa: E402

# functions that are pure waiting, not work
WAIT = {("~", "0", "<method 'read' of '_io.BufferedReader' objects>"),
        ("~", "0", "<built-in method select.select>"),
        ("~", "0", "<built-in method posix.read>"),
        ("~", "0", "<method 'poll' of 'select.poll' objects>"),
        ("~", "0", "<built-in method time.sleep>")}
WAIT_NAMES = ("select.select", "posix.read", "_io.BufferedReader", "subprocess", "time.sleep",
              "_winapi", "Popen.wait", "communicate")


def _is_wait(fn) -> bool:
    return any(w in f"{fn[0]}:{fn[2]}" for w in WAIT_NAMES)


def scenarios(pkg):
    dbs = A.list_databases(pkg)
    bo = A._bo_databases(pkg)
    db = bo[0]
    pools = sorted((p for p in A.pools(db=db) if p.get("rows")), key=lambda p: -p["rows"])
    big, small = pools[0]["name"], next((p["name"] for p in reversed(pools) if p["rows"] >= 5), pools[0]["name"])
    key = A.entries(pool=big, db=db, limit=1)["rows"][0]["key"]
    S = [
        ("query (1 row)", lambda: A.query(sql="SELECT 1", db=db, limit=1)),
        ("list_databases", lambda: A.list_databases(pkg)),
        ("schema (stats)", lambda: A.schema(db=db, stats=True, tables=[big])),
        ("pools", lambda: A.pools(db=db)),
        (f"entries 50 [{big}]", lambda: A.entries(pool=big, db=db, limit=50)),
        (f"entries fields+filters", lambda: A.entries(pool=big, db=db, fields=["Title"],
                                                      filters={"Title": {"op": "exists"}}, limit=20, scan=200)),
        (f"explain_object", lambda: A.explain_object(key=key, pool=big, db=db)),
        (f"related depth=2", lambda: A.related(key=key, pool=big, depth=2, incoming=False)),
        (f"refs [{big}]", lambda: A.refs(pool=big, db=db)),
        (f"search keyword [{small}]", lambda: A.search(q="a", pool=small, db=db, mode="keyword", limit=10)),
        (f"search rebuild [{small}]", lambda: A.search(q="a", pool=small, db=db, mode="keyword",
                                                       limit=10, refresh="full")),
        (f"fields [{small}]", lambda: A.fields(pool=small, db=db)),
        (f"query_decoded [{small}]", lambda: A.query_decoded(sql="SELECT COUNT(*) FROM decoded",
                                                             pool=small, db=db)),
        ("app_context", lambda: A.app_context()),
        ("mark (all pools)", lambda: A.mark(label="profile")),
        ("changes_since", lambda: A.changes_since(limit=20)),
    ]
    big_index = f"index rebuild [{big}]"
    S.append((big_index, lambda: A.search(q="a", pool=big, db=db, mode="keyword", limit=10, refresh="full")))
    return S, {"db": db, "big": big, "small": small, "key": key, "dbs": len(dbs)}


def run(only: str | None, reps: int, top: int):
    pkg = A._pkg(None)
    S, info = scenarios(pkg)
    print(f"package {pkg}\n  {info['dbs']} databases, big pool {info['big']}, small pool {info['small']}\n")
    host_total: dict[tuple, float] = {}
    rows = []
    for name, fn in S:
        if only and only.lower() not in name.lower():
            continue
        fn()                                    # warm: we profile the steady state
        pr = cProfile.Profile()
        t0 = time.perf_counter()
        pr.enable()
        for _ in range(reps):
            fn()
        pr.disable()
        wall = (time.perf_counter() - t0) / reps
        st = pstats.Stats(pr)
        wait = host = 0.0
        for func, (cc, nc, tt, ct, callers) in st.stats.items():
            (wait := wait + tt) if _is_wait(func) else (host := host + tt)
            if not _is_wait(func):
                host_total[func] = host_total.get(func, 0.0) + tt / reps
        rows.append((name, wall, wait / reps, host / reps))
        print(f"{name:<28} {wall*1000:8.1f}ms  device {wait/reps*1000:7.1f}ms  host {host/reps*1000:7.1f}ms"
              f"  ({host/max(wall*reps,1e-9)*100:4.1f}% host)")
    print(f"\n{'':<28} {'wall':>10} {'device':>14} {'host':>12}")
    tw, td, th = sum(r[1] for r in rows), sum(r[2] for r in rows), sum(r[3] for r in rows)
    print(f"{'TOTAL':<28} {tw*1000:8.1f}ms  device {td*1000:7.1f}ms  host {th*1000:7.1f}ms")
    print(f"\nhost CPU by function (summed across scenarios, own time):\n")
    ranked = sorted(host_total.items(), key=lambda kv: -kv[1])[:top]
    for func, t in ranked:
        where = f"{Path(func[0]).name}:{func[1]}" if func[0] != "~" else "builtin"
        print(f"  {t*1000:8.2f}ms  {func[2]:<42} {where}")
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--top", type=int, default=25)
    a = ap.parse_args()
    try:
        run(a.only, a.reps, a.top)
        return 0
    finally:
        A._shutdown_runners()


if __name__ == "__main__":
    sys.exit(main())
