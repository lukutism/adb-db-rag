#!/usr/bin/env python3
"""android-db-explorer — a database dashboard for an Android app's on-device SQLite.

    python3 explorer.py                     # http://127.0.0.1:8765 (next free port if busy)
    python3 explorer.py --dump snap.json    # capture the tenant for offline work
    python3 explorer.py --fixtures snap.json --no-device   # serve that capture, no emulator

What it is: pgAdmin-shaped tooling for a database that has no usable schema of its own. The
relations are not declared anywhere — an `AssetId` lives inside a serialized BLOB — so the object
graph here is derived by decoding every object and verifying that the ids resolve.

The tenant is loaded once into memory, so browsing costs no device round trips. Only the data
grid's raw-table pages and the SQL console go back to the device per request.

Binds to 127.0.0.1 only: this is the app's data.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent))
import android_db_mcp as A  # noqa: E402
import bo_codec  # noqa: E402

WEB = Path(__file__).resolve().parent / "web"
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".json": "application/json"}


# --------------------------------------------------------------------------- store


class View:
    """An immutable snapshot of the decoded tenant.

    Every query method reads one of these, taken once at entry. Rebuilding produces a new View and
    swaps it in with a single assignment, so a request in flight during Reload keeps serving the
    previous, self-consistent one instead of iterating a dict that another thread is still filling.
    """
    __slots__ = ("records", "objects", "by_pool", "edges", "incoming", "state",
                 "table_pool", "refmaps", "targets", "db_info", "schemas", "locale")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))


class Store:
    """The decoded tenant, its reference graph, and both directions of every edge."""

    def __init__(self, package: str | None, locale: str | None, max_objects: int,
                 fixture: str | None = None, use_device: bool = True):
        self.use_device = use_device
        self.package = A._pkg(package) if use_device else (package or "fixture")
        self.locale = locale or A.LOCALE
        self.max_objects = max_objects
        self.fixture = fixture
        self.lock = threading.RLock()
        self.state: dict = {"loaded": False}
        self.ctx: dict = {}
        self.targets: list[dict] = []
        self.refmaps: dict[str, list[dict]] = {}
        self.records: list[dict] = []
        self.objects: dict[tuple[str, str], dict] = {}
        self.by_pool: dict[str, list[dict]] = {}
        self.incoming: dict[tuple[str, str], list[dict]] = {}
        self.edges: list[dict] = []
        self.db_info: list[dict] = []
        self.schemas: dict = {}
        self.error: str | None = None
        self.view = View(records=[], objects={}, by_pool={}, edges=[], incoming={}, state={"loaded": False},
                         table_pool={}, refmaps={}, targets=[], db_info=[], schemas={}, locale=self.locale)

    # ---- loading -----------------------------------------------------------

    def load(self, refresh: bool = False) -> dict:
        with self.lock:
            t0 = time.perf_counter()
            try:
                if self.fixture:
                    self._from_fixture()      # a snapshot was given: use it, never the device
                else:
                    if not self.use_device:
                        raise RuntimeError("--no-device needs --fixtures: there is nothing to serve "
                                           "without either a device or a snapshot.")
                    self._from_device(refresh)
                self._index(t0)
                return self.state
            except Exception as e:
                traceback.print_exc()
                self.error = f"{type(e).__name__}: {e}"
                self.state = {"loaded": False, "error": self.error}
                return self.state

    def _from_device(self, refresh: bool) -> None:
        self.ctx = A.app_context(package=self.package, refresh=refresh, pools_limit=500)
        targets = A._target_pools(self.package, None, None)
        targets.sort(key=lambda p: p.get("rows") or 0)
        chosen, skipped, budget = [], [], 0
        for p in targets:
            n = p.get("rows") or 0
            if budget + n <= self.max_objects:
                chosen.append(p)
                budget += n
            else:
                skipped.append({"pool": p["name"], "rows": n})
        self.targets, self.skipped = chosen, skipped
        self.records = self._read_objects(chosen)
        self.refmaps = {}
        for p in chosen:
            try:
                self.refmaps[p["name"]] = A._ref_map(self.package, p["name"], p["db"],
                                                     p.get("applicationId"), refresh=refresh)
            except Exception:
                self.refmaps[p["name"]] = []
        self.db_info = self._read_db_info()
        self.schemas = {}

    def _from_fixture(self) -> None:
        d = json.loads(Path(self.fixture).read_text())
        self.ctx = d["ctx"]
        self.package = d.get("package", self.package)
        self.locale = d.get("locale", self.locale)
        self.targets = d["targets"]
        self.skipped = d.get("skipped", [])
        self.records = d["records"]
        self.refmaps = d["refmaps"]
        self.db_info = d.get("db_info", [])
        self.schemas = d.get("schemas", {})

    def dump(self, path: str) -> None:
        Path(path).write_text(json.dumps({
            "package": self.package, "locale": self.locale, "ctx": self.ctx,
            "targets": self.targets, "skipped": getattr(self, "skipped", []),
            "records": self.records, "refmaps": self.refmaps, "db_info": self.db_info,
            "schemas": {d["db"]: self.schema(d["db"], stats=True) for d in self.db_info if not d.get("error")},
        }, default=str))

    def _read_objects(self, targets: list[dict]) -> list[dict]:
        """Every object of every target pool, decoded. Batched 12 entry tables to a query."""
        by_db: dict[str, list[dict]] = {}
        for p in targets:
            by_db.setdefault(p["db"], []).append(p)
        out: list[dict] = []
        for db, ps in by_db.items():
            name_of = {p["table"]: p["name"] for p in ps}
            for i in range(0, len(ps), 12):
                batch = ps[i:i + 12]
                q = " UNION ALL ".join(
                    f'SELECT {A._sq(p["table"])}, "#key", "#thingId", "#lastChange", "#value" '
                    f'FROM {A._q(p["table"])}' for p in batch)
                r = A._read(self.package, db, q, limit=1_000_000, blobs="base64")
                for tbl, key, thing, change, blob in r["rows"]:
                    value = A._decode_cell(blob)
                    out.append({"pool": name_of.get(tbl, tbl), "db": db, "table": tbl,
                                "key": key, "thingId": thing, "lastChange": change,
                                "title": bo_codec.display_title(value, self.locale),
                                "value": value})
        return out

    def _read_db_info(self) -> list[dict]:
        """Size and table count per database — one cheap query each (page_count * page_size)."""
        out = []
        for db in A.list_databases(self.package):
            info = {"db": db, "name": db.rsplit("/", 1)[-1], "tenant": A._tenant_of(db)}
            try:
                r = A._read(self.package, db,
                            "SELECT (SELECT * FROM pragma_page_count()) * (SELECT * FROM pragma_page_size()), "
                            "(SELECT COUNT(*) FROM sqlite_master WHERE type='table'), "
                            "(SELECT COUNT(*) FROM sqlite_master WHERE type='index')", limit=1)
                info["bytes"], info["tables"], info["indexes"] = r["rows"][0]
            except Exception as e:
                info["error"] = str(e)
            out.append(info)
        return out

    def _index(self, t0: float) -> None:
        objects: dict[tuple[str, str], dict] = {}
        by_pool: dict[str, list[dict]] = {}
        for r in self.records:
            objects[(r["pool"], str(r["key"]))] = r
            by_pool.setdefault(r["pool"], []).append(r)
        table_pool = {r["table"]: r["pool"] for r in self.records}
        edges, incoming = self._build_graph(objects, by_pool, table_pool)
        counts = {name: len(v) for name, v in by_pool.items()}
        names = sorted({p["name"] for p in self.targets} | {e["to"] for e in edges})
        where = {}
        for r in self.records:
            where.setdefault(r["pool"], (r["db"], r["table"]))
        nodes = [{"name": n, "rows": counts.get(n, 0),
                  "db": where.get(n, (None, None))[0], "table": where.get(n, (None, None))[1],
                  "out": sum(1 for e in edges if e["from"] == n),
                  "in": sum(1 for e in edges if e["to"] == n),
                  "weak": sum(1 for e in edges if e["from"] == n and e["resolved_rate"] < 1)}
                 for n in names]
        state = {
            "loaded": True, "device": self.ctx.get("device", {}), "package": self.package,
            "tenants": self.ctx.get("tenants", []), "locale": self.locale,
            "backend": self.ctx.get("backend", {}),
            "objects": len(objects), "pools": len(nodes),
            "nodes": nodes, "edges": edges,
            "databases": self.db_info,
            "skipped_pools": getattr(self, "skipped", []),
            "live": self.use_device,
            "loaded_at": time.time(), "load_s": round(time.perf_counter() - t0, 2),
        }
        # one assignment: a reader either sees the whole previous tenant or the whole new one
        self.view = View(records=self.records, objects=objects, by_pool=by_pool, edges=edges,
                         incoming=incoming, state=state, table_pool=table_pool, refmaps=self.refmaps,
                         targets=self.targets, db_info=self.db_info,
                         schemas=getattr(self, "schemas", {}), locale=self.locale)
        self.objects, self.by_pool, self.edges, self.incoming, self.state = (
            objects, by_pool, edges, incoming, state)

    def _build_graph(self, objects, by_pool, table_pool):
        agg: dict[tuple[str, str, str], dict] = {}
        incoming: dict[tuple[str, str], list[dict]] = {}
        seen_in: set[tuple] = set()
        for pool, recs in by_pool.items():
            rules = {r["path"]: r for r in self.refmaps.get(pool, [])}
            if not rules:
                continue
            for rec in recs:
                for generic, _c, rid in A._walk_ids(rec["value"]):
                    rule = rules.get(generic)
                    if rule is None:
                        continue
                    target = table_pool.get(rule["target_table"], rule["target_pool"])
                    hit = (target, str(rid)) in objects
                    e = agg.setdefault((pool, generic, target),
                                       {"from": pool, "to": target, "path": generic,
                                        "field": rule["field"], "links": 0, "resolved": 0,
                                        "many": generic.endswith("[*]")})
                    e["links"] += 1
                    e["resolved"] += 1 if hit else 0
                    if hit:
                        # one entry per (source object, path): an array that points at the same
                        # target twice must not list the referrer twice or inflate the #refs count
                        mark = (target, str(rid), pool, str(rec["key"]), generic)
                        if mark not in seen_in:
                            seen_in.add(mark)
                            incoming.setdefault((target, str(rid)), []).append(
                                {"pool": pool, "key": rec["key"], "title": rec["title"], "path": generic})
        edges = sorted(agg.values(), key=lambda e: (e["from"], e["path"]))
        for e in edges:
            e["resolved_rate"] = round(e["resolved"] / e["links"], 3) if e["links"] else 0.0
        return edges, incoming

    # ---- queries -----------------------------------------------------------

    def overview(self) -> dict:
        v = self.view
        pools = sorted(({"pool": n, "rows": len(x)} for n, x in v.by_pool.items()),
                       key=lambda p: -p["rows"])
        recent = sorted(v.records, key=lambda r: -(r["lastChange"] or 0))[:25]
        weak = [e for e in v.edges if e["resolved_rate"] < 1]
        return {
            "objects": len(v.objects), "pools_with_data": len(v.by_pool),
            "pools_total": len(v.state.get("nodes", [])), "edges": len(v.edges),
            "databases": v.db_info,
            "top_pools": pools[:15], "empty_pools": [n["name"] for n in v.state.get("nodes", []) if not n["rows"]],
            "dangling": [{"from": e["from"], "path": e["path"], "to": e["to"],
                          "links": e["links"], "resolved": e["resolved"],
                          "rate": e["resolved_rate"]} for e in sorted(weak, key=lambda e: e["resolved_rate"])][:20],
            "recent": [{"pool": r["pool"], "key": r["key"], "title": r["title"],
                        "lastChange": r["lastChange"]} for r in recent],
            "unresolved_total": sum(e["links"] - e["resolved"] for e in v.edges),
            "link_total": sum(e["links"] for e in v.edges),
            "titled": sum(1 for r in v.records if r["title"]),
        }

    _SORTS = {"key": lambda r: str(r["key"]), "title": lambda r: (r["title"] or "").lower(),
              "lastChange": lambda r: r["lastChange"] or 0, "refs": lambda r: 0}

    @staticmethod
    def _path_sort_key(rec, path, locale):
        """Sort on a decoded path. Mixed types are grouped (numbers, then text, then empty) so a
        column that is numeric for most objects and missing for the rest still sorts sensibly."""
        vals = [A._readable(x, locale) for x in A._path_values(rec["value"], path)]
        if not vals:
            return (2, 0.0, "")
        v = vals[0]
        if isinstance(v, bool):
            return (0, float(v), "")
        if isinstance(v, (int, float)):
            return (0, float(v), "")
        t = str(v)
        return (1, 0.0, t.lower()) if t.strip() else (2, 0.0, "")

    def pool(self, name: str, q: str = "", offset: int = 0, limit: int = 100,
             sort: str = "lastChange", direction: str = "desc", columns: list[str] | None = None,
             filters: dict | None = None) -> dict:
        v = self.view
        recs = v.by_pool.get(name, [])
        total = len(recs)
        if filters:
            recs = [r for r in recs if A._match_filters(r["value"], filters, v.locale)]
        if q:
            ql = q.lower()
            recs = [r for r in recs if ql in str(r["key"]).lower() or ql in str(r["title"] or "").lower()
                    or any(ql in str(t).lower() for _p, t in bo_codec.flatten(r["value"], v.locale))]
        raw_sort = str(sort or "")
        sort = raw_sort.lstrip("#")            # the grid sends the column name, e.g. "#lastChange"
        key_fn = self._SORTS.get(sort)
        if sort == "refs":
            key_fn = lambda r: len(v.incoming.get((name, str(r["key"])), []))
        elif key_fn is None and raw_sort and not raw_sort.startswith("#"):
            key_fn = lambda r: self._path_sort_key(r, raw_sort, v.locale)   # any decoded column
        if key_fn:
            recs = sorted(recs, key=key_fn, reverse=(direction == "desc"))
        offset = max(0, int(offset))       # a negative offset used to slice from the end
        page = recs[offset:offset + limit]
        cols = columns or self.pool_columns(name)
        rows = []
        for r in page:
            cells = {"#key": r["key"], "#title": r["title"], "#lastChange": r["lastChange"],
                     "#refs": len(v.incoming.get((name, str(r["key"])), []))}
            for c in cols:
                if c.startswith("#"):
                    continue
                vals = [A._readable(x, v.locale) for x in A._path_values(r["value"], c)]
                cells[c] = vals[0] if len(vals) == 1 else (vals or None)
            rows.append(cells)
        return {"pool": name, "total": total, "matched": len(recs), "offset": offset,
                "filters": filters or None,
                "limit": limit, "sort": sort, "dir": direction,
                "sorted_by": raw_sort,
                "columns": ["#key", "#title", "#lastChange", "#refs"] + [c for c in cols if not c.startswith("#")],
                "rows": rows}

    def pool_columns(self, name: str, top: int = 8) -> list[str]:
        """Most-filled scalar paths of a pool — the columns a grid should open with."""
        recs = self.view.by_pool.get(name, [])
        if not recs:
            return []
        counts: dict[str, int] = {}
        for r in recs[:200]:
            for path, text in bo_codec.flatten(r["value"], self.locale):
                if str(text).strip():
                    counts[re.sub(r"\[\d+\]", "[*]", path)] = counts.get(re.sub(r"\[\d+\]", "[*]", path), 0) + 1
        ranked = sorted(counts.items(), key=lambda kv: -kv[1])
        return [p for p, _n in ranked[:top]]

    def pool_fields(self, name: str) -> dict:
        recs = self.view.by_pool.get(name, [])
        stats: dict[str, dict] = {}
        for r in recs:
            seen = A._walk_paths(r["value"], self.locale)
            for path, rec in seen.items():
                a = stats.setdefault(path, {"kind": rec["kind"], "n": 0, "filled": 0, "example": None})
                a["n"] += 1
                a["filled"] += 1 if rec["filled"] else 0
                a["example"] = a["example"] or rec["example"]
                if a["kind"] != rec["kind"]:
                    a["kind"] = "mixed"
        return {"pool": name, "objects": len(recs),
                "fields": [{"path": p, **v, "fill_rate": round(v["filled"] / max(1, len(recs)), 3)}
                           for p, v in sorted(stats.items(), key=lambda kv: -kv[1]["filled"])]}

    def object(self, pool: str, key: str, view: "View | None" = None) -> dict:
        v = view or self.view
        rec = v.objects.get((pool, str(key)))
        if rec is None:
            raise KeyError(f"{pool}/{key}")
        rules = {r["path"]: r for r in v.refmaps.get(pool, [])}
        table_pool = v.table_pool       # precomputed once per load, not per call
        out_refs = []
        for generic, concrete, rid in A._walk_ids(rec["value"]):
            rule = rules.get(generic)
            if rule is None:
                continue
            target = table_pool.get(rule["target_table"], rule["target_pool"])
            hit = v.objects.get((target, str(rid)))
            out_refs.append({"path": concrete, "field": rule["field"], "pool": target, "key": rid,
                             "title": hit["title"] if hit else None, "resolved": hit is not None})
        return {"pool": pool, "key": rec["key"], "thingId": rec["thingId"], "lastChange": rec["lastChange"],
                "title": rec["title"], "db": rec["db"], "table": rec["table"],
                "fields": [{"path": p, "text": t} for p, t in bo_codec.flatten(rec["value"], v.locale)],
                "refs_out": out_refs, "refs_in": v.incoming.get((pool, str(rec["key"])), []),
                "value": rec["value"]}

    MAX_DEPTH = 4

    def related(self, pool: str, key: str, depth: int = 2) -> dict:
        """Breadth-first walk of the object graph, both directions."""
        v = self.view
        depth = max(1, min(int(depth), self.MAX_DEPTH))   # an unbounded depth walks the whole tenant
        seen = {(pool, str(key)): 0}
        frontier = [(pool, str(key))]
        nodes, links = [], []
        link_set: set[tuple] = set()
        for hop in range(1, depth + 1):
            nxt = []
            for p, k in frontier:
                try:
                    o = self.object(p, k, v)
                except KeyError:
                    continue
                for r in o["refs_out"]:
                    if not r["resolved"]:
                        continue
                    t = (r["pool"], str(r["key"]))
                    edge = (f"{p}/{k}", f"{t[0]}/{t[1]}", r["path"])
                    if edge not in link_set:
                        link_set.add(edge)
                        links.append({"from": edge[0], "to": edge[1], "path": r["path"], "dir": "out"})
                    if t not in seen:
                        seen[t] = hop
                        nxt.append(t)
                for r in o["refs_in"]:
                    t = (r["pool"], str(r["key"]))
                    edge = (f"{t[0]}/{t[1]}", f"{p}/{k}", r["path"])
                    if edge not in link_set:
                        link_set.add(edge)
                        links.append({"from": edge[0], "to": edge[1], "path": r["path"], "dir": "in"})
                    if t not in seen:
                        seen[t] = hop
                        nxt.append(t)
            frontier = nxt
        for (p, k), hop in seen.items():
            rec = v.objects.get((p, k))
            nodes.append({"pool": p, "key": k, "title": rec["title"] if rec else None, "hop": hop})
        return {"root": f"{pool}/{key}", "depth": depth, "nodes": nodes, "links": links}

    def find(self, q: str, limit: int = 100) -> dict:
        v = self.view
        ql = (q or "").strip().lower()
        if not ql:
            return {"q": q, "total": 0, "hits": []}
        exact, title, field = [], [], []
        for (pool, key), r in v.objects.items():
            if key.lower() == ql:
                exact.append((pool, r, "key", None))
            elif r["title"] and ql in r["title"].lower():
                title.append((pool, r, "title", r["title"]))
            elif len(ql) >= 3:
                for p, text in bo_codec.flatten(r["value"], v.locale):
                    if ql in str(text).lower():
                        field.append((pool, r, p, str(text)[:120]))
                        break
        all_hits = exact + title + field
        return {"q": q, "total": len(all_hits),
                "hits": [{"pool": p, "key": r["key"], "title": r["title"], "why": w, "snippet": s}
                         for p, r, w, s in all_hits[:limit]]}

    # ---- watch -------------------------------------------------------------

    def mark(self, label: str | None = None) -> dict:
        v = self.view
        self._mark = {"at": time.time(), "label": label,
                      "objects": {(r["pool"], str(r["key"])): (r["lastChange"], r["title"],
                                                               json.dumps(r["value"], default=str, sort_keys=True))
                                  for r in v.records}}
        return {"marked": len(self._mark["objects"]), "at": self._mark["at"], "label": label}

    def changes(self, reload: bool = False, limit: int = 200) -> dict:
        """Diff the current view against the mark.

        `reload` is off by default: a GET must not silently trigger a full device re-read, which on
        a large tenant takes longer than the dashboard's own auto-refresh interval and piles up
        handler threads behind Store.lock. The UI reloads explicitly."""
        mark = getattr(self, "_mark", None)
        if not mark:
            return {"marked": False, "note": "Nothing marked yet — press Mark, act in the app, then Refresh."}
        if reload and self.use_device:
            self.load(refresh=False)
        v = self.view
        cur = {(r["pool"], str(r["key"])): r for r in v.records}
        added, removed, changed = [], [], []
        for ident, r in cur.items():
            old = mark["objects"].get(ident)
            now_json = json.dumps(r["value"], default=str, sort_keys=True)
            if old is None:
                added.append({"change": "added", "pool": r["pool"], "key": r["key"], "title": r["title"],
                              "lastChange": r["lastChange"]})
            elif old[0] != r["lastChange"] or old[2] != now_json:
                try:
                    diff = A._diff_values(json.loads(old[2]), r["value"], v.locale)
                except Exception:
                    diff = None
                changed.append({"change": "changed", "pool": r["pool"], "key": r["key"], "title": r["title"],
                                "lastChange": {"before": old[0], "after": r["lastChange"]}, "diff": diff})
        for ident, old in mark["objects"].items():
            if ident not in cur:
                removed.append({"change": "removed", "pool": ident[0], "key": ident[1], "title": old[1],
                                "lastChange": old[0]})
        items = changed + added + removed
        return {"marked": True, "label": mark["label"], "at": mark["at"],
                "elapsed_s": round(time.time() - mark["at"], 1),
                "summary": {"added": len(added), "removed": len(removed), "changed": len(changed),
                            "unchanged": len(cur) - len(added) - len(changed)},
                "truncated": len(items) > limit, "changes": items[:limit]}

    # ---- live device passthrough -------------------------------------------

    def _need_device(self, what: str) -> None:
        if not self.use_device:
            raise RuntimeError(f"{what} reads the device, and this server was started with "
                               "--no-device against a snapshot. Restart it without --fixtures.")

    def schema(self, db: str, stats: bool = True, tables: list[str] | None = None) -> dict:
        cached = (self.view.schemas or {}).get(db)
        if cached is not None and not self.use_device:
            return cached
        self._need_device("Schema")
        return A.schema(db=db, package=self.package, stats=stats, tables=tables, refresh=False)

    def table(self, db: str, name: str, offset: int = 0, limit: int = 100,
              order: str | None = None, direction: str = "asc", where: str | None = None) -> dict:
        self._need_device("Reading a raw table")
        ob = f'ORDER BY {A._q(order)} {"DESC" if direction == "desc" else "ASC"}' if order else ""
        wh = f"WHERE {where}" if where else ""
        total = A.query(sql=f"SELECT COUNT(*) FROM {A._q(name)} {wh}", db=db,
                        package=self.package, limit=1)["rows"][0][0]
        r = A.query(sql=f"SELECT rowid, * FROM {A._q(name)} {wh} {ob} LIMIT {int(limit)} OFFSET {int(offset)}",
                    db=db, package=self.package, limit=limit)
        return {"db": db, "table": name, "total": total, "offset": offset, "limit": limit,
                "columns": r["columns"], "rows": r["rows"], "backend": r.get("backend")}

    def explain(self, db: str, sql: str, pool: str = "") -> dict:
        """EXPLAIN QUERY PLAN for a statement, the way any SQL client offers it."""
        plan = "EXPLAIN QUERY PLAN " + sql
        if pool:
            return self.sql_decoded(pool, plan, 200)
        return self.sql(db, plan, 200)

    def sql(self, db: str, sql: str, limit: int = 500) -> dict:
        self._need_device("Raw SQL")
        t0 = time.perf_counter()
        r = A.query(sql=sql, db=db, package=self.package, limit=limit)
        r["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        return r

    def sql_decoded(self, pool: str, sql: str, limit: int = 500) -> dict:
        t0 = time.perf_counter()
        if not self.use_device:
            r = self._local_decoded_sql(pool, sql, limit)
            r["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            return r
        rec = (self.view.by_pool.get(pool) or [None])[0]
        r = A.query_decoded(sql=sql, pool=pool, db=rec["db"] if rec else None,
                            package=self.package, limit=limit)
        r["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        return r


    _mirror_cache: dict = {}

    def _local_mirror(self, pool: str):
        """In-memory twin of the decoded index, built once per pool per load.

        It used to be rebuilt on every request — a full json.dumps of the pool plus a path walk over
        every object — which made each keystroke of Explain cost seconds on a large pool."""
        import sqlite3
        v = self.view
        key = (id(v), pool)
        hit = Store._mirror_cache.get(key)
        if hit is not None:
            return hit
        recs = v.by_pool.get(pool, [])
        con = sqlite3.connect(":memory:", check_same_thread=False)
        con.executescript("""
            CREATE TABLE decoded(src_key TEXT PRIMARY KEY, key TEXT, thing, last_change INTEGER,
                                 title TEXT, json TEXT);
            CREATE TABLE paths(path TEXT PRIMARY KEY, kind TEXT, n INTEGER, n_filled INTEGER, example TEXT);
            CREATE TABLE sources(src_key TEXT PRIMARY KEY, last_change INTEGER);
            CREATE TABLE docs(doc_id TEXT PRIMARY KEY, src_key TEXT, last_change INTEGER, text TEXT, meta TEXT);
        """)
        rows = [(f'{r["key"]}|{r["thingId"]}', r["key"], r["thingId"], r["lastChange"], r["title"],
                 json.dumps(r["value"], default=str, ensure_ascii=False)) for r in recs]
        con.executemany("INSERT OR REPLACE INTO decoded VALUES (?,?,?,?,?,?)", rows)
        con.executemany("INSERT OR REPLACE INTO sources VALUES (?,?)", [(r[0], r[3]) for r in rows])
        con.executemany("INSERT OR REPLACE INTO docs VALUES (?,?,?,?,?)",
                        [(r[0], r[0], r[3], bo_codec.to_text(rec["value"], v.locale),
                          json.dumps({"key": rec["key"], "title": rec["title"]}))
                         for r, rec in zip(rows, recs)])
        f = self.pool_fields(pool)
        con.executemany("INSERT OR REPLACE INTO paths VALUES (?,?,?,?,?)",
                        [(x["path"], x["kind"], x["n"], x["filled"], x["example"]) for x in f["fields"]])
        con.commit()
        Store._mirror_cache.clear()          # only the current load is worth keeping
        Store._mirror_cache[key] = (con, len(recs))
        return Store._mirror_cache[key]

    def _local_decoded_sql(self, pool: str, sql: str, limit: int) -> dict:
        """Offline twin of query_decoded: the same `decoded` and `paths` tables, built in memory
        from the snapshot, so the SQL console works against a capture with no device attached."""
        import sqlite3
        if not A._SQL_OK.match(sql or "") and not sql.strip().upper().startswith("EXPLAIN"):
            raise ValueError("Read-only queries only: start with SELECT or WITH")
        limit = A._positive_limit(int(limit))
        con, n = self._local_mirror(pool)
        try:
            res = A._rows(con.execute(sql), limit)
        except sqlite3.Error as e:
            raise RuntimeError(f"SQL error over the decoded mirror: {e}") from e
        res.update({"pool": pool, "source": f"snapshot mirror ({n} objects, in memory)"})
        return res


# --------------------------------------------------------------------------- http


_EXPORT_MAX_ROWS = int(os.environ.get("EXPORT_MAX_ROWS", "200000"))


def _csv(columns: list[str], rows: list) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(columns)
    for r in rows:
        w.writerow([json.dumps(c, default=str) if isinstance(c, (dict, list)) else c
                    for c in (r if isinstance(r, list) else [r.get(c) for c in columns])])
    return buf.getvalue().encode()


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class Handler(BaseHTTPRequestHandler):
    store: Store = None
    port: int = 0

    def _host_ok(self) -> bool:
        """Reject anything that did not address us as localhost.

        Listening on 127.0.0.1 keeps other machines out, but a page on the internet can point its
        own hostname at 127.0.0.1 (DNS rebinding) and then read this API as same-origin — which
        here means the app's data, including the session token in MobileUser."""
        host = (self.headers.get("Host") or "").strip()
        name = host.rsplit(":", 1)[0].strip("[]").lower() if host else ""
        return name in ("127.0.0.1", "localhost", "::1", "")

    def _guard(self) -> bool:
        if self._host_ok():
            return True
        self._json({"error": "Refused: this dashboard only answers requests addressed to "
                             "127.0.0.1 or localhost."}, 403)
        return False

    def log_message(self, fmt, *args):
        if os.environ.get("EXPLORER_DEBUG"):
            super().log_message(fmt, *args)

    def _send(self, body: bytes, ctype: str, code: int = 200, filename: str | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, code: int = 200):
        self._send(json.dumps(payload, default=str).encode(), "application/json", code)

    def _static(self, path: str):
        f = (WEB / path.lstrip("/")).resolve()
        # startswith() would also accept a sibling directory called web-anything; and
        # BaseHTTPRequestHandler does not normalise ".." for us.
        try:
            inside = f.is_relative_to(WEB)
        except AttributeError:                                  # Python < 3.9
            inside = str(f) == str(WEB) or str(f).startswith(str(WEB) + os.sep)
        if not inside or not f.is_file():
            return self._json({"error": "not found"}, 404)
        self._send(f.read_bytes(), MIME.get(f.suffix, "application/octet-stream"))

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if not self._guard():
            return
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        i = lambda k, d: int(q.get(k, d))
        s = self.store
        try:
            if u.path == "/" or u.path == "/index.html":
                return self._static("index.html")
            if not u.path.startswith("/api/"):
                return self._static(u.path)
            if u.path == "/api/state":
                return self._json(s.state if s.state.get("loaded") else s.load())
            if u.path == "/api/overview":
                return self._json(s.overview())
            if u.path == "/api/pool":
                cols = json.loads(q["columns"]) if q.get("columns") else None
                flt = json.loads(q["filters"]) if q.get("filters") else None
                return self._json(s.pool(q.get("name", ""), q.get("q", ""), i("offset", 0),
                                         i("limit", 100), q.get("sort", "lastChange"),
                                         q.get("dir", "desc"), cols, flt))
            if u.path == "/api/pool_columns":
                return self._json({"columns": s.pool_columns(q.get("name", ""), i("top", 8))})
            if u.path == "/api/fields":
                return self._json(s.pool_fields(q.get("name", "")))
            if u.path == "/api/object":
                return self._json(s.object(q.get("pool", ""), q.get("key", "")))
            if u.path == "/api/related":
                return self._json(s.related(q.get("pool", ""), q.get("key", ""), i("depth", 2)))
            if u.path == "/api/find":
                return self._json(s.find(q.get("q", ""), i("limit", 100)))
            if u.path == "/api/schema":
                return self._json(s.schema(q.get("db", ""), q.get("stats", "1") != "0"))
            if u.path == "/api/table":
                return self._json(s.table(q.get("db", ""), q.get("name", ""), i("offset", 0),
                                          i("limit", 100), q.get("order") or None,
                                          q.get("dir", "asc"), q.get("where") or None))
            if u.path == "/api/indexes":
                try:
                    return self._json({"indexes": A._index_inventory(s.package)})
                except Exception as e:
                    return self._json({"indexes": [], "error": str(e)})
            if u.path == "/api/explain":
                return self._json(s.explain(q.get("db", ""), q.get("sql", ""), q.get("pool", "")))
            if u.path == "/api/changes":
                return self._json(s.changes(q.get("reload", "0") == "1", i("limit", 200)))
            if u.path == "/api/export":
                return self._export(q)
            self._json({"error": "not found"}, 404)
        except KeyError as e:
            self._json({"error": f"no such object {e}"}, 404)
        except Exception as e:
            traceback.print_exc()
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def _export(self, q: dict):
        kind, fmt = q.get("kind", "pool"), q.get("format", "csv")
        s = self.store
        if kind == "pool":
            d = s.pool(q.get("name", ""), q.get("q", ""), 0, 1_000_000,
                       q.get("sort", "lastChange"), q.get("dir", "desc"),
                       json.loads(q["columns"]) if q.get("columns") else None,
                       json.loads(q["filters"]) if q.get("filters") else None)
            cols, rows, stem = d["columns"], d["rows"], d["pool"]
        else:
            d = s.table(q.get("db", ""), q.get("name", ""), 0, _EXPORT_MAX_ROWS)
            cols, rows, stem = d["columns"], d["rows"], d["table"]
        # the name lands in a Content-Disposition header, which CPython does not validate:
        # a CR/LF in a pool or table name would inject headers of the caller's choosing
        stem = _SAFE_NAME.sub("_", str(stem or "export"))[:80] or "export"
        if len(rows) > _EXPORT_MAX_ROWS:
            return self._json({"error": f"{len(rows)} rows is over the export cap of "
                                        f"{_EXPORT_MAX_ROWS} (EXPORT_MAX_ROWS). Narrow it with a "
                                        f"filter, or raise the cap."}, 413)
        if fmt == "json":
            payload = json.dumps(rows, default=str, indent=1).encode()
            return self._send(payload, "application/json", filename=f"{stem}.json")
        return self._send(_csv(cols, rows), "text/csv", filename=f"{stem}.csv")

    def do_POST(self):
        if not self._guard():
            return
        u = urlparse(self.path)
        try:
            if u.path == "/api/reload":
                return self._json(self.store.load(refresh=True))
            if u.path == "/api/mark":
                return self._json(self.store.mark(self._body().get("label")))
            if u.path == "/api/sql":
                b = self._body()
                return self._json(self.store.sql(b.get("db", ""), b.get("sql", ""), int(b.get("limit", 500))))
            if u.path == "/api/sql_decoded":
                b = self._body()
                return self._json(self.store.sql_decoded(b.get("pool", ""), b.get("sql", ""), int(b.get("limit", 500))))
            self._json({"error": "not found"}, 404)
        except Exception as e:
            traceback.print_exc()
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)


class _Server(ThreadingHTTPServer):
    daemon_threads = True


def _ours(port: int) -> bool:
    """Is an android-db explorer already answering on this port?"""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=1.5) as r:
            return b'"pools"' in r.read(4096)
    except Exception:
        return False


def _bind(port: int, tries: int = 20):
    """Return (server, port), or (None, port) when our own explorer already owns it."""
    import errno
    for n in range(tries):
        try:
            return _Server(("127.0.0.1", port + n), Handler), port + n
        except OSError as e:
            if e.errno not in (errno.EADDRINUSE, errno.EACCES):
                raise
            if n == 0 and _ours(port):
                return None, port
            print(f"  port {port + n} is busy, trying {port + n + 1} …", flush=True)
    raise SystemExit(f"no free port in {port}..{port + tries - 1}")


def _open(url: str) -> None:
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("EXPLORER_PORT", "8765")))
    ap.add_argument("--package")
    ap.add_argument("--locale")
    ap.add_argument("--max-objects", type=int, default=int(os.environ.get("EXPLORER_MAX", "20000")))
    ap.add_argument("--dump", help="write a snapshot of the tenant and exit")
    ap.add_argument("--fixtures", help="serve a snapshot written by --dump")
    ap.add_argument("--no-device", action="store_true", help="with --fixtures: never touch a device")
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args()

    srv = None
    if not a.dump:
        srv, a.port = _bind(a.port)
        if srv is None:
            url = f"http://127.0.0.1:{a.port}"
            print(f"  an explorer is already running at {url} — opening it")
            if not a.no_open:
                _open(url)
            return 0

    store = Store(a.package, a.locale, a.max_objects, a.fixtures,
                  use_device=not (a.fixtures or a.no_device))
    Handler.store = store
    Handler.port = a.port
    print("loading …", flush=True)
    st = store.load()
    if st.get("loaded"):
        print(f"  {st['objects']} objects · {st['pools']} pools · {len(st['edges'])} edges · {st['load_s']}s",
              flush=True)
    else:
        print(f"  load failed: {st.get('error')}", flush=True)
    if a.dump:
        store.dump(a.dump)
        print(f"  wrote {a.dump} ({Path(a.dump).stat().st_size // 1024} KB)")
        A._shutdown_runners()
        return 0

    url = f"http://127.0.0.1:{a.port}"
    print(f"\n  {url}\n  ctrl-c to stop", flush=True)
    if not a.no_open:
        _open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        A._shutdown_runners()
    return 0


if __name__ == "__main__":
    sys.exit(main())
