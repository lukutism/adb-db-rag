#!/usr/bin/env python3
"""Retrieval evaluation for android-db-mcp.

Scores keyword, vector and hybrid search against known-item queries, so a change of embedding
model or fusion setting is measured instead of guessed.

    python3 eval.py build --pool CodesGroup --chunk Codes --n 40
    python3 eval.py run   --pool CodesGroup --chunk Codes
    python3 eval.py run   --pool CodesGroup --chunk Codes --models BAAI/bge-small-en-v1.5,intfloat/multilingual-e5-small

Queries are generated from the indexed documents themselves — each one has a known correct
answer, which needs no hand labelling:

  exact        the document's own title                (favours keyword search; a sanity check)
  partial      two or three words of the title         (typos and half-remembered names)
  descriptive  text from another field of the document (the case vectors are supposed to win)

Hand-written queries in evals/manual.json are merged in:
  [{"q": "which group covers pump damage", "expect_key": "…", "note": "…"}]
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sqlite3
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import android_db_mcp as A  # noqa: E402

EVAL_DIR = Path(os.environ.get("EVAL_DIR") or (Path.cwd() / "evals"))
SWEEP: dict[str, dict[str, float]] = {}
STOP = {"the", "a", "an", "of", "and", "or", "to", "for", "in", "on", "with", "new", "code", "codes"}
WORD = re.compile(r"[\w']+", re.UNICODE)


def _set_path(pool: str, chunk: str | None, locale: str | None) -> Path:
    """One eval set per pool + chunk + locale: a German query set must not overwrite an English one."""
    loc = (locale or A.LOCALE)
    return EVAL_DIR / f"{pool}{('-' + chunk) if chunk else ''}-{loc}.json"


def _spec(pool: str, db: str | None, chunk: str | None, locale: str | None):
    pkg = A._pkg(None)
    return pkg, A._index_spec(pkg, db, None, None, pool, chunk, locale or A.LOCALE, None)


def _docs(path: str) -> list[dict]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT doc_id, text, meta FROM docs").fetchall()
    finally:
        con.close()
    return [{"doc_id": r[0], "text": r[1], "meta": json.loads(r[2])} for r in rows]


# --------------------------------------------------------------------------- build

def build(pool: str, db: str | None, chunk: str | None, locale: str | None, n: int, seed: int) -> Path:
    pkg, spec = _spec(pool, db, chunk, locale)
    A._ensure_index(pkg, spec, "auto", embed=False)
    docs = _docs(spec["path"])
    rnd = random.Random(seed)
    rnd.shuffle(docs)
    cases: list[dict] = []
    for d in docs:
        title = (d["meta"] or {}).get("title")
        if not title or len(title) < 4:
            continue
        words = [w for w in WORD.findall(title) if w.lower() not in STOP and len(w) > 1]
        # lines of this document that are not the title — the "describe it differently" case
        other = [ln.split(": ", 1)[1] for ln in d["text"].splitlines()
                 if ": " in ln and not ln.startswith(("pool:", "key:", "parent:"))
                 and ln.split(": ", 1)[1].strip() and ln.split(": ", 1)[1] != title]
        cases.append({"q": title, "family": "exact", "expect_doc": d["doc_id"], "key": d["meta"].get("key")})
        if len(words) >= 2:
            take = rnd.sample(words, min(3, len(words)))
            cases.append({"q": " ".join(take), "family": "partial", "expect_doc": d["doc_id"],
                          "key": d["meta"].get("key")})
        if other:
            cases.append({"q": rnd.choice(other)[:120], "family": "descriptive", "expect_doc": d["doc_id"],
                          "key": d["meta"].get("key")})
        # A phrase the way a person actually types one: two words from the target plus one word it
        # does NOT contain. FTS5 joins bare terms with AND, so the strict reading returns nothing —
        # this is the family the OR fallback exists for, and the only one that can measure it.
        if len(words) >= 2:
            cases.append({"q": " ".join(rnd.sample(words, 2) + ["__distractor__"]),
                          "family": "phrase", "expect_doc": d["doc_id"], "key": d["meta"].get("key"),
                          "needs_distractor": True})
        if len({c["expect_doc"] for c in cases}) >= n:
            break
    # swap the placeholder for a real word that belongs to some other document
    vocab = sorted({w for d in docs for w in WORD.findall((d["meta"] or {}).get("title") or "")
                    if w.lower() not in STOP and len(w) > 3})
    for c in cases:
        if c.pop("needs_distractor", False) and vocab:
            own = {w.lower() for w in WORD.findall(c["q"])}
            foreign = [w for w in vocab if w.lower() not in own]
            c["q"] = c["q"].replace("__distractor__", rnd.choice(foreign) if foreign else "").strip()
    cases = [c for c in cases if c["q"].strip()]

    EVAL_DIR.mkdir(exist_ok=True)
    out = _set_path(pool, chunk, locale)
    out.write_text(json.dumps({"pool": pool, "chunk": chunk, "db": db, "built": time.strftime("%F %T"),
                               "documents": len(docs), "cases": cases}, indent=1))
    print(f"{len(cases)} cases over {len({c['expect_doc'] for c in cases})} documents -> {out}")
    return out


def _manual() -> list[dict]:
    f = EVAL_DIR / "manual.json"
    if not f.is_file():
        return []
    items = json.loads(f.read_text())
    return [{**i, "family": "manual"} for i in items]


# --------------------------------------------------------------------------- run

def _rank_of(hits: list[dict], case: dict) -> int | None:
    for i, h in enumerate(hits, 1):
        if case.get("expect_doc") and h.get("doc_id") == case["expect_doc"]:
            return i
        if case.get("expect_key") and str((h.get("meta") or {}).get("key")) == str(case["expect_key"]):
            return i
    return None


def _score(ranks: list[int | None]) -> dict:
    n = len(ranks) or 1
    return {"n": len(ranks),
            "recall@1": round(sum(1 for r in ranks if r == 1) / n, 3),
            "recall@5": round(sum(1 for r in ranks if r and r <= 5) / n, 3),
            "mrr@10": round(sum(1 / r for r in ranks if r and r <= 10) / n, 3),
            "misses": sum(1 for r in ranks if r is None)}


def run(pool: str, db: str | None, chunk: str | None, locale: str | None, models: list[str],
        limit: int, only: str | None) -> int:
    pkg, spec = _spec(pool, db, chunk, locale)
    f = _set_path(pool, chunk, locale)
    if not f.is_file():
        print(f"no eval set at {f} — run `eval.py build` first", file=sys.stderr)
        return 2
    data = json.loads(f.read_text())
    cases = [c for c in data["cases"] + _manual() if not only or c["family"] == only]
    print(f"{len(cases)} cases, pool={pool} chunk={chunk or '-'} locale={locale or A.LOCALE}  ({f.name})\n")

    report: dict[str, dict] = {}
    for model in models:
        os.environ["EMBED_MODEL"] = model
        A._VEC_CACHE.clear()
        t0 = time.perf_counter()
        A._ensure_index(pkg, spec, "auto", embed=True)          # (re)embeds when the model changed
        index_s = time.perf_counter() - t0
        per: dict[str, dict[str, list]] = {}
        latency: dict[str, list[float]] = {"bm25": [], "vector": [], "hybrid": []}
        for c in cases:
            q = c["q"]
            runs = {}
            t = time.perf_counter()
            common = dict(pool=pool, db=db, chunk=chunk, limit=limit, refresh="never")
            runs["bm25"] = A.search(q=q, mode="keyword", **common)["hits"]
            latency["bm25"].append(time.perf_counter() - t)
            t = time.perf_counter()
            runs["vector"] = A.search(q=q, mode="semantic", **common)["hits"]
            latency["vector"].append(time.perf_counter() - t)
            t = time.perf_counter()
            runs["hybrid"] = A.search(q=q, mode="hybrid", **common)["hits"]
            latency["hybrid"].append(time.perf_counter() - t)
            for wname, w in SWEEP.items():
                runs[wname] = A.search(q=q, mode="hybrid", weights=w, **common)["hits"]
            for name, hits in runs.items():
                per.setdefault(name, {}).setdefault(c["family"], []).append(_rank_of(hits, c))
                per[name].setdefault("ALL", []).append(_rank_of(hits, c))
        report[model] = {"index_s": round(index_s, 2),
                         "retrievers": {name: {fam: _score(r) for fam, r in fams.items()}
                                        for name, fams in per.items()},
                         "latency_ms": {k: round(statistics.median(v) * 1000, 1) for k, v in latency.items()}}
        print(f"model: {model}   (index/embed {index_s:.2f}s)")
        fams = sorted({f for fams in per.values() for f in fams if f != "ALL"}) + ["ALL"]
        print(f"  {'retriever':<10}" + "".join(f"{fam:>26}" for fam in fams))
        for name in ["bm25", "vector", "hybrid"] + list(SWEEP):
            cells = []
            for fam in fams:
                ranks = per[name].get(fam)
                if not ranks:
                    cells.append(f"{'—':>26}")
                    continue
                sc = _score(ranks)
                cells.append(f"{'r@1 ' + format(sc['recall@1'], '.2f') + '  mrr ' + format(sc['mrr@10'], '.2f'):>26}")
            print(f"  {name:<10}" + "".join(cells))
        print(f"  median latency: " + ", ".join(f"{k} {v}ms" for k, v in report[model]["latency_ms"].items()) + "\n")

    EVAL_DIR.mkdir(exist_ok=True)
    (EVAL_DIR / "results.json").write_text(json.dumps(report, indent=1))
    best = max(report, key=lambda m: report[m]["retrievers"]["hybrid"]["ALL"]["mrr@10"])
    if len(models) > 1:
        print(f"best hybrid MRR@10: {best}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("build", "run"):
        sp = sub.add_parser(name)
        sp.add_argument("--pool", required=True)
        sp.add_argument("--db")
        sp.add_argument("--chunk")
        sp.add_argument("--locale")
        if name == "build":
            sp.add_argument("--n", type=int, default=40)
            sp.add_argument("--seed", type=int, default=7)
        else:
            sp.add_argument("--models", default=os.environ.get("EMBED_MODEL", "BAAI/bge-small-en-v1.5"))
            sp.add_argument("--limit", type=int, default=10)
            sp.add_argument("--only")
            sp.add_argument("--sweep", action="store_true",
                            help="also score alternative bm25/vector fusion weights")
    a = ap.parse_args()
    try:
        if a.cmd == "build":
            build(a.pool, a.db, a.chunk, a.locale, a.n, a.seed)
            return 0
        if getattr(a, "sweep", False):
            SWEEP.update({f"hyb {b}/{v}": {"bm25": b, "vector": v}
                          for b, v in ((1.0, 0.0), (1.0, 0.3), (1.0, 0.5), (1.0, 1.0), (0.5, 1.0))})
        return run(a.pool, a.db, a.chunk, a.locale, [m.strip() for m in a.models.split(",") if m.strip()],
                   a.limit, a.only)
    finally:
        A._shutdown_runners()


if __name__ == "__main__":
    sys.exit(main())
