"""Offline regression tests: real SQLite, fake ADB and deterministic embeddings."""
import asyncio
import concurrent.futures
import importlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


_import_cache = tempfile.TemporaryDirectory(prefix="android-db-tests-import-")
with patch.dict(os.environ, {"ANDROID_DB_CACHE": _import_cache.name}):
    m = importlib.import_module("android_db_mcp")


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="android-db-tests-")
        self.addCleanup(self.tmp.cleanup)
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE entry ("#key" TEXT, "#thingId" TEXT, "#lastChange" INTEGER, "#value" BLOB)')
        self.pool = {"name": "Test", "table": "entry", "db": "test.db", "applicationId": 1, "poolId": 1}
        for name, value in (("CACHE", Path(self.tmp.name)), ("_read", self.read),
                            ("_pkg", lambda p: "com.test"), ("_resolve_pool", lambda *args: self.pool)):
            p = patch.object(m, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.dict(os.environ, {"EMBED_MODEL": "test-model"})
        p.start()
        self.addCleanup(p.stop)
        self.spec = m._index_spec("com.test", "test.db", None, None, "Test", "Codes", "en", None)

    def read(self, package, database, sql, limit=200, **kwargs):
        return {**m._rows(self.db.execute(sql), limit, kwargs.get("blobs", "summary")), "backend": "test sqlite"}

    def put(self, key="key", change=1, codes=None):
        value = m.bo_codec.encode({"Codes": codes if codes is not None else [{"Title": "Original"}]})
        self.db.execute('DELETE FROM entry WHERE "#key"=?', (key,))
        self.db.execute('INSERT INTO entry VALUES (?, ?, ?, ?)', (key, "thing", change, value))

    def ensure(self, refresh="auto", embed=False):
        return m._ensure_index("com.test", self.spec, refresh, embed)

    def index_rows(self):
        with sqlite3.connect(self.spec["path"]) as con:
            return con.execute("SELECT text, vec FROM docs ORDER BY doc_id").fetchall()

    def test_initially_empty_pool_gets_first_insert(self):
        self.ensure("full")
        self.put()
        self.assertEqual(self.ensure()["docs"], 1)

    def test_clearing_list_removes_old_chunks(self):
        self.put()
        self.ensure("full")
        self.put(change=2, codes=[])
        self.assertEqual(self.ensure()["docs"], 0)
        self.assertEqual(self.ensure()["status"], "up to date")

    def test_new_row_at_same_timestamp_is_indexed(self):
        self.put(change=10)
        self.ensure("full")
        self.put(key="new", change=10)
        self.assertEqual(self.ensure()["docs"], 2)

    def test_changed_record_with_lower_timestamp_is_refreshed(self):
        self.put(change=10)
        self.ensure("full")
        self.put(change=5, codes=[{"Title": "Replacement"}])
        self.ensure()
        self.assertIn("Replacement", self.index_rows()[0][0])

    def test_deletion_removes_docs_and_fts(self):
        self.put()
        self.ensure("full")
        self.db.execute("DELETE FROM entry")
        self.assertEqual(self.ensure()["docs"], 0)
        with sqlite3.connect(self.spec["path"]) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM fts").fetchone()[0], 0)

    def test_keyword_refresh_then_semantic_repairs_missing_vectors(self):
        self.put()
        self.ensure("full")
        with sqlite3.connect(self.spec["path"]) as con:
            con.execute("UPDATE docs SET vec=?", (struct.pack("ff", 1, 0),))
            m._set_meta(con, embedded=True, embed_model="test-model", embedding_dimensions=2)
        self.put(change=2, codes=[{"Title": "Replacement"}])
        self.ensure()
        model = FakeEmbedder()
        with patch.object(m, "_embedder", return_value=model):
            self.ensure(embed=True)
            self.ensure(embed=True)
        self.assertEqual(len(model.texts), 1)
        self.assertIsNotNone(self.index_rows()[0][1])

    def test_model_change_reembeds_even_with_never(self):
        self.put()
        old, new = FakeEmbedder(2), FakeEmbedder(3)
        with patch.object(m, "_embedder", return_value=old):
            self.ensure("full", True)
        with patch.dict(os.environ, {"EMBED_MODEL": "different-model"}), patch.object(m, "_embedder", return_value=new):
            self.ensure("never", True)
        self.assertEqual(len(new.texts), 1)
        self.assertEqual(len(self.index_rows()[0][1]), 3 * 4)

    def test_index_format_change_rebuilds(self):
        self.put()
        self.ensure("full")
        with sqlite3.connect(self.spec["path"]) as con:
            m._set_meta(con, index_version=-1)
        self.put(change=1, codes=[{"Title": "Updated"}])
        self.ensure()
        self.assertIn("Updated", self.index_rows()[0][0])

    def test_entries_and_decode_pagination(self):
        self.put("a")
        self.put("b")
        for name, args in (("entries", {"pool": "Test"}), ("decode", {"db": "test.db", "table": "entry"})):
            with self.subTest(tool=name):
                fn = m._TOOLS[name]
                first = fn(**args, limit=1)
                self.assertTrue(first["truncated"])
                second = fn(**args, limit=1, after_rowid=first["next_after_rowid"])
                self.assertFalse(second["truncated"])
                self.assertIsNone(second["next_after_rowid"])
                self.assertNotEqual(first["rows"][0]["rowid"], second["rows"][0]["rowid"])

    def test_invalid_page_limit(self):
        with self.assertRaises(ValueError):
            m._TOOLS["entries"]("Test", limit=0)

    def test_null_change_markers_are_refreshed(self):
        self.put(change=None)
        self.ensure("full")
        self.put(change=None, codes=[{"Title": "Updated"}])
        self.ensure()
        self.assertIn("Updated", self.index_rows()[0][0])

    def test_never_reports_changes_without_updating_documents(self):
        self.put()
        self.ensure("full")
        self.db.execute("DELETE FROM entry")
        self.put("new")
        info = self.ensure("never")
        self.assertEqual(info["stale_rows"], 1)
        self.assertEqual(info["deleted_rows"], 1)
        self.assertIn("key: key", self.index_rows()[0][0])

    def test_never_rejects_incompatible_index_format(self):
        self.put()
        self.ensure("full")
        with sqlite3.connect(self.spec["path"]) as con:
            m._set_meta(con, index_version=-1)
        with self.assertRaisesRegex(RuntimeError, "format changed"):
            self.ensure("never")

    def test_embedding_failure_rolls_back_refresh(self):
        self.put()
        with patch.object(m, "_embedder", return_value=FakeEmbedder()):
            self.ensure("full", True)
        before = self.index_rows()
        self.put(change=2, codes=[{"Title": "Updated"}])
        with patch.object(m, "_embedder", side_effect=RuntimeError("model unavailable")):
            with self.assertRaisesRegex(RuntimeError, "model unavailable"):
                self.ensure(embed=True)
        self.assertEqual(self.index_rows(), before)

    def test_keyword_and_semantic_modes_after_edit(self):
        self.put()
        kwargs = {"pool": "Test", "chunk": "Codes", "db": "test.db"}
        with patch.object(m, "_embedder", return_value=FakeEmbedder()):
            first = m.search("original", mode="semantic", **kwargs)
            self.assertEqual(first["hits"][0]["meta"]["title"], "Original")
            self.put(change=2, codes=[{"Title": "Replacement"}])
            keyword = m.search("Replacement", mode="keyword", **kwargs)
            self.assertEqual(keyword["hits"][0]["meta"]["title"], "Replacement")
            self.assertEqual(keyword["retrievers"], ["bm25"])
            semantic = m.search("replacement", mode="semantic", **kwargs)
            self.assertEqual(semantic["hits"][0]["meta"]["title"], "Replacement")
            self.assertEqual(semantic["index"]["missing_vectors"], 0)
            self.assertEqual(semantic["retrievers"], ["vector"])

    def test_auto_mode_follows_the_embeddings_switch(self):
        self.put()
        kwargs = {"pool": "Test", "chunk": "Codes", "db": "test.db"}
        with patch.object(m, "_embedder", return_value=FakeEmbedder()):
            with patch.object(m, "EMBEDDINGS", False):
                off = m.search("Original", **kwargs)
            self.assertEqual(off["retrievers"], ["bm25"])
            self.assertIn("embeddings are off", off["note"])
            with patch.object(m, "EMBEDDINGS", True):
                on = m.search("Original", **kwargs)
            self.assertEqual(sorted(on["retrievers"]), ["bm25", "vector"])

    def test_an_unknown_mode_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "mode must be"):
            m.search("x", pool="Test", db="test.db", mode="magic")

    def test_plain_table_ttl_rebuilds(self):
        self.spec = m._index_spec("com.test", "test.db", "entry", ["#key"], None, None, "en", None)
        self.put()
        self.ensure("full")
        self.put("new")
        self.assertEqual(self.ensure()["docs"], 1)
        with sqlite3.connect(self.spec["path"]) as con:
            m._set_meta(con, built_at=time.time() - m._AUTO_TTL - 1)
        self.assertEqual(self.ensure()["docs"], 2)


class FakeEmbedder:
    def __init__(self, dimensions=2):
        self.dimensions = dimensions
        self.texts = []

    def embed(self, texts):
        for text in texts:
            self.texts.append(text)
            yield [1.0] * self.dimensions


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.runners = {}
        for name, value in (("_servers", self.runners), ("SERIAL", "test-device"), ("RUNNER", "run-as"), ("PERSIST", True)):
            p = patch.object(m, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_package_and_device_isolation(self):
        class Runner:
            def __init__(self, identity, package, serial=None):
                self.package, self.serial = package, serial
                self.proc = type("Process", (), {"poll": lambda self: None})()
            def request(self, req): return {"package": self.package, "serial": self.serial}
            def close(self): pass
        with patch.object(m, "_Server", Runner):
            for mode in ("run-as", "root"):
                for package, serial in (("com.a", "one"), ("com.b", "one"), ("com.b", "two")):
                    with patch.object(m, "SERIAL", serial), patch.object(m, "RUNNER", mode):
                        result = m._device_sql(package, "app.db", "SELECT 1")
                    self.assertEqual(result["package"], package)
                    self.assertEqual(result["serial"], serial)

    def test_write_with_lost_response_is_not_retried(self):
        closed = []
        class Runner:
            def __init__(self, *args): self.proc = type("Process", (), {"poll": lambda self: None})()
            def request(self, req): raise RuntimeError("reply lost after commit")
            def close(self): closed.append(True)
        with patch.object(m, "_Server", Runner), patch.object(m, "_exec_for", return_value='{"ok":true}') as fallback:
            with self.assertRaisesRegex(RuntimeError, "outcome.*unknown"):
                m._device_sql("com.a", "app.db", "UPDATE counter SET n=n+1", mode="write")
            fallback.assert_not_called()
        self.assertTrue(closed)
        self.assertFalse(self.runners)

    def test_startup_failure_can_fallback_for_write(self):
        with patch.object(m, "_Server", side_effect=RuntimeError("ping failed")), patch.object(m, "_exec_for", return_value='{"ok":true}') as fallback:
            self.assertTrue(m._device_sql("com.a", "app.db", "UPDATE t SET n=1", mode="write")["ok"])
            fallback.assert_called_once()

    def test_read_failure_closes_runner_and_falls_back(self):
        closed = []
        class Runner:
            def __init__(self, *args): self.proc = type("Process", (), {"poll": lambda self: None})()
            def request(self, req): raise RuntimeError("lost read response")
            def close(self): closed.append(True)
        with patch.object(m, "_Server", Runner), patch.object(m, "_exec_for", return_value='{"rows":[[1]]}') as fallback:
            self.assertEqual(m._device_sql("com.a", "app.db", "SELECT 1")["rows"], [[1]])
            fallback.assert_called_once()
        self.assertTrue(closed)
        self.assertFalse(self.runners)

    def test_sql_error_does_not_retry(self):
        class Runner:
            def __init__(self, *args): self.proc = type("Process", (), {"poll": lambda self: None})()
            def request(self, req): return {"error": "no such table"}
            def close(self): pass
        with patch.object(m, "_Server", Runner), patch.object(m, "_exec_for") as fallback:
            with self.assertRaisesRegex(RuntimeError, "no such table"):
                m._device_sql("com.a", "app.db", "UPDATE missing SET n=1", mode="write")
            fallback.assert_not_called()

    def test_ping_failure_reaps_process(self):
        processes = []
        real_popen = subprocess.Popen
        program = 'import sys; sys.stdin.readline(); print(\'{"serve":false}\', flush=True); sys.stdin.read()'
        def start(*args, **kwargs):
            proc = real_popen([sys.executable, "-u", "-c", program], **kwargs)
            processes.append(proc)
            return proc
        with patch.object(m.subprocess, "Popen", side_effect=start):
            with self.assertRaisesRegex(RuntimeError, "does not support"):
                m._Server("run-as", "com.test", "test-device")
        self.assertIsNotNone(processes[0].poll())
        self.assertTrue(processes[0].stdin.closed)

    def test_timeout_reaps_process(self):
        real_popen = subprocess.Popen
        program = 'import sys,time; sys.stdin.readline(); print(\'{"serve":true}\', flush=True); sys.stdin.readline(); time.sleep(30)'
        def start(*args, **kwargs):
            return real_popen([sys.executable, "-u", "-c", program], **kwargs)
        with patch.object(m.subprocess, "Popen", side_effect=start):
            runner = m._Server("run-as", "com.test", "test-device")
        self.addCleanup(runner.close)
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            runner.request({"mode": "read"}, timeout=0.03)
        self.assertIsNotNone(runner.proc.poll())

    def test_requests_are_serialized_and_shutdown_reaps_process(self):
        # A local child implements the runner wire protocol. No Android device is involved.
        program = '''import base64,json,sys,time
for line in sys.stdin:
    req=json.loads(base64.b64decode(line))
    time.sleep(0.002)
    print(json.dumps({"serve":True,"token":req.get("token")}),flush=True)
'''
        real_popen = subprocess.Popen
        def start(*args, **kwargs):
            return real_popen([sys.executable, "-u", "-c", program], **kwargs)
        with patch.object(m.subprocess, "Popen", side_effect=start):
            runner = m._Server("run-as", "com.test", "test-device")
        self.addCleanup(runner.close)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda token: runner.request({"token": token})["token"], range(24)))
        self.assertEqual(results, list(range(24)))
        self.runners[(m.ADB, "test-device", "com.test", "run-as")] = runner
        m._shutdown_runners()
        self.assertFalse(self.runners)
        self.assertIsNotNone(runner.proc.poll())
        self.assertTrue(runner.proc.stdin.closed)
        self.assertTrue(runner.proc.stdout.closed)


class SnapshotTests(unittest.TestCase):
    def test_consistency_metadata_and_strict_mode(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(m, "CACHE", Path(tmp)), patch.object(m, "_device_has_sqlite3", return_value=False), patch.object(m, "_exec_for", return_value=b"db bytes") as pull:
            result = m._TOOLS["snapshot"]("app.db", "com.test")
            self.assertFalse(result["consistent"])
            self.assertIn("warning", result)
            pull.reset_mock()
            with self.assertRaisesRegex(RuntimeError, "consistent"):
                m._TOOLS["snapshot"]("app.db", "com.test", require_consistent=True)
            pull.assert_not_called()

    def test_backup_metadata(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(m, "CACHE", Path(tmp)), patch.object(m, "_device_has_sqlite3", return_value=True), patch.object(m, "_in_sandbox", return_value=b"db bytes"):
            self.assertTrue(m._TOOLS["snapshot"]("app.db", "com.test")["consistent"])

    def test_snapshot_query_is_read_only_and_exposes_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "test.db")
            with sqlite3.connect(path) as con:
                con.execute("CREATE TABLE t(n INTEGER)")
                con.execute("INSERT INTO t VALUES (1)")
            info = {"path": path, "consistent": False, "warning": "best effort", "method": "raw"}
            with patch.object(m, "_snapshot", return_value=info):
                result = m._snapshot_sql("com.test", "test.db", "SELECT * FROM t", 10, True)
                self.assertEqual(result["snapshot"], info)
                with self.assertRaises(sqlite3.OperationalError):
                    m._snapshot_sql("com.test", "test.db", "UPDATE t SET n=2", 10, True)

    def test_failed_snapshot_does_not_inherit_old_consistency(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(m, "CACHE", Path(tmp)), patch.object(m, "_device_has_sqlite3", return_value=False):
            folder = Path(tmp) / "com.test"
            folder.mkdir()
            (folder / "app.db").write_bytes(b"old")
            (folder / "app.db.meta.json").write_text(json.dumps({"consistent": True, "method": "on-device sqlite3 .backup"}))
            with patch.object(m, "_exec_for", side_effect=[b"partial new copy", RuntimeError("device disconnected")]):
                with self.assertRaisesRegex(RuntimeError, "device disconnected"):
                    m._snapshot("com.test", "app.db")
            cached = m._snapshot("com.test", "app.db", refresh=False)
            self.assertFalse(cached["consistent"])


class MCPTests(unittest.TestCase):
    def test_stdio_tool_schemas(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        async def check(cache):
            env = dict(os.environ, ANDROID_DB_CACHE=cache)
            params = StdioServerParameters(command=sys.executable, args=[str(Path(m.__file__).resolve())], env=env)
            async with stdio_client(params) as (reader, writer):
                async with ClientSession(reader, writer) as client:
                    await client.initialize()
                    tools = {t.name: t for t in (await client.list_tools()).tools}
                    self.assertEqual(set(tools), set(m._TOOLS))
                    schemas = {name: tool.model_dump(by_alias=True)["inputSchema"] for name, tool in tools.items()}
                    self.assertIn("after_rowid", schemas["entries"]["properties"])
                    self.assertIn("after_rowid", schemas["decode"]["properties"])
                    self.assertIn("require_consistent", schemas["snapshot"]["properties"])
                    # Input errors must survive the actual MCP transport without calling ADB.
                    result = await client.call_tool("entries", {"pool": "Test", "limit": 0})
                    self.assertTrue(result.model_dump(by_alias=True)["isError"])
                    self.assertIn("limit must be a positive integer", result.content[0].text)

        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(check(tmp))

    def test_uncertain_write_message_reaches_mcp_client(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        program = '''import android_db_mcp as m
class Runner:
    def __init__(self, *args): self.proc=type("Process", (), {"poll":lambda self:None})()
    def request(self, req): raise RuntimeError("response lost after commit")
    def close(self): pass
m._Server=Runner
m.SERIAL="fake-device"
m._jar_available=lambda p,db=None:True
m._exec_for=lambda *a, **kw:'{"error":"write was incorrectly retried"}'
m.main()
'''
        async def check(cache):
            env = dict(os.environ, ANDROID_DB_CACHE=cache, PYTHONPATH=str(Path(m.__file__).parent))
            params = StdioServerParameters(command=sys.executable, args=["-c", program], env=env)
            async with stdio_client(params) as (reader, writer):
                async with ClientSession(reader, writer) as client:
                    await client.initialize()
                    result = await client.call_tool("execute", {"package": "com.test", "db": "app.db", "sql": "UPDATE t SET n=n+1"})
                    self.assertTrue(result.model_dump(by_alias=True)["isError"])
                    self.assertIn("Write outcome is unknown", result.content[0].text)
                    self.assertIn("not retried", result.content[0].text)
        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(check(tmp))



class ReferenceTests(unittest.TestCase):
    def test_reference_field_names(self):
        self.assertEqual(m._ref_base("AssetId"), "Asset")
        self.assertEqual(m._ref_base("OrderTypeId"), "OrderType")
        self.assertEqual(m._ref_base("CodeIds"), "Code")
        self.assertIsNone(m._ref_base("Id"))          # the object's own identity, not a reference
        self.assertIsNone(m._ref_base("Title"))

    def test_walk_ids_keeps_generic_and_concrete_paths(self):
        value = {"PriorityId": "pri-1", "Codes": [{"AssetId": "ast-1"}, {"AssetId": "ast-2"}], "Title": "x"}
        found = m._walk_ids(value)
        self.assertIn(("PriorityId", "PriorityId", "pri-1"), found)
        self.assertIn(("Codes[*].AssetId", "Codes[1].AssetId", "ast-2"), found)
        self.assertEqual(m._ids_by_path(value)["Codes[*].AssetId"], ["ast-1", "ast-2"])

    def test_walk_ids_ignores_values_too_short_to_be_ids(self):
        self.assertEqual(m._walk_ids({"AssetId": "a1"}), [])

    def test_walk_ids_skips_undecodable_blobs(self):
        self.assertEqual(m._walk_ids({"AssetId": "ast-1", "$b64": "zz"}), [])

    def test_target_guesses_handles_plurals(self):
        idx = {"asset": [{"name": "Asset", "db": "d", "table": "t", "exists": True}]}
        self.assertEqual(m._target_guesses("Codes[*].AssetIds", idx)[0]["name"], "Asset")
        self.assertEqual(m._target_guesses("Nope.WidgetId", idx), [])

    def test_expand_refs_scopes_to_one_chunk_element(self):
        value = {"Codes": [{"AssetId": "ast-1"}, {"AssetId": "ast-2"}]}
        rules = [{"path": "Codes[*].AssetId", "field": "AssetId", "target_pool": "Asset",
                  "target_db": "d", "target_table": "t"}]
        titles = {("d", "t", "ast-1"): "WTG 07", ("d", "t", "ast-2"): m._MISSING}
        every = m._expand_refs("pkg", value, rules, "en", titles)
        self.assertEqual(len(every), 2)
        only_second = m._expand_refs("pkg", value, rules, "en", titles, scope="Codes[1]")
        self.assertEqual([r["id"] for r in only_second], ["ast-2"])
        self.assertEqual(m._ref_lines(every),
                         ["Asset: WTG 07", "AssetId: ast-2 (no Asset object)"])

    def test_reference_that_exists_without_a_title_is_not_called_unresolved(self):
        rules = [{"path": "PriorityId", "field": "PriorityId", "target_pool": "Priority",
                  "target_db": "d", "target_table": "t"}]
        refs = m._expand_refs("pkg", {"PriorityId": "pri-1"}, rules, "en", {("d", "t", "pri-1"): None})
        self.assertTrue(refs[0]["resolved"])
        self.assertEqual(m._ref_lines(refs), ["Priority: pri-1"])


class PathTests(unittest.TestCase):
    value = {"Title": "Pump", "Status": "OPEN", "Count": 7,
             "Codes": [{"Title": "A", "N": 1}, {"Title": "B", "N": 5}]}

    def test_path_values_wildcard_and_index(self):
        self.assertEqual(m._path_values(self.value, "Title"), ["Pump"])
        self.assertEqual(m._path_values(self.value, "Codes[*].Title"), ["A", "B"])
        self.assertEqual(m._path_values(self.value, "Codes[1].Title"), ["B"])
        self.assertEqual(m._path_values(self.value, "Missing.Deep"), [])

    def test_project_collapses_single_values(self):
        got = m._project(self.value, ["Title", "Codes[*].Title", "Nope"], "en")
        self.assertEqual(got, {"Title": "Pump", "Codes[*].Title": ["A", "B"], "Nope": None})

    def test_filters(self):
        f = lambda flt: m._match_filters(self.value, flt, "en")
        self.assertTrue(f({"Status": "OPEN"}))
        self.assertFalse(f({"Status": "DONE"}))
        self.assertTrue(f({"Title": {"op": "contains", "value": "pum"}}))
        self.assertTrue(f({"Codes[*].Title": {"op": "eq", "value": "B"}}))
        self.assertTrue(f({"Count": {"op": "gt", "value": 3}}))
        self.assertFalse(f({"Count": {"op": "lt", "value": 3}}))
        self.assertTrue(f({"Nope": {"op": "missing"}}))
        self.assertTrue(f({"Title": {"op": "exists"}}))
        self.assertFalse(f({"Status": "OPEN", "Count": {"op": "lt", "value": 3}}))   # AND
        with self.assertRaises(ValueError):
            f({"Title": {"op": "wat", "value": 1}})

    def test_translated_values_resolve_before_matching(self):
        obj = {"Title": [{"locale": "en", "value": "Pump"}, {"locale": "de", "value": "Pumpe"}]}
        self.assertTrue(m._match_filters(obj, {"Title": "Pumpe"}, "de"))
        self.assertFalse(m._match_filters(obj, {"Title": "Pumpe"}, "en"))


class FusionTests(unittest.TestCase):
    def test_rrf_weights(self):
        ranked = {"bm25": ["a", "b"], "vector": ["b", "c"]}
        equal = m._rrf(ranked, 60)
        self.assertGreater(equal["b"], equal["a"])            # in both lists
        bm_only = m._rrf(ranked, 60, {"bm25": 1.0, "vector": 0.0})
        self.assertNotIn("c", bm_only)
        self.assertGreater(bm_only["a"], bm_only["b"])

    def test_why_reports_literal_overlap(self):
        text = "pool: X\nTitle: broken pump\nNote: nothing"
        self.assertEqual(m._why(text, "pump"), ["Title: broken pump"])
        self.assertEqual(m._why(text, "compressor"), [])

    def test_filter_predicate(self):
        meta = {"key": "k1", "title": "Broken Pump", "parent_title": "Cause"}
        self.assertTrue(m._passes(meta, "text", {"title": "pump"}, None))
        self.assertFalse(m._passes(meta, "text", {"title": "valve"}, None))
        self.assertFalse(m._passes(meta, "text", None, ["other"]))
        self.assertTrue(m._passes(meta, "text", None, ["k1"]))


class MirrorTests(DatabaseTests):
    def mirror(self):
        with sqlite3.connect(self.spec["path"]) as con:
            return con.execute("SELECT key, title, json FROM decoded ORDER BY key").fetchall()

    def paths(self):
        with sqlite3.connect(self.spec["path"]) as con:
            return {r[0]: (r[1], r[2], r[3]) for r in con.execute("SELECT path, kind, n, n_filled FROM paths")}

    def test_mirror_and_paths_follow_the_data(self):
        self.put(codes=[{"Title": "Original"}])
        self.ensure("full")
        rows = self.mirror()
        self.assertEqual(len(rows), 1)
        self.assertIn("Original", rows[0][2])
        self.assertIn("Codes[*].Title", self.paths())
        self.assertEqual(self.paths()["Codes[*].Title"][1:], (1, 1))

    def test_mirror_updates_incrementally_and_drops_deleted(self):
        self.put(codes=[{"Title": "Original"}])
        self.ensure("full")
        self.put(change=2, codes=[{"Title": "Edited"}])
        self.ensure()
        self.assertIn("Edited", self.mirror()[0][2])
        self.db.execute('DELETE FROM entry')
        self.ensure()
        self.assertEqual(self.mirror(), [])
        self.assertEqual(self.paths(), {})

    def test_query_decoded_rejects_writes(self):
        self.put()
        self.ensure("full")
        with self.assertRaises(ValueError):
            m.query_decoded(sql="DELETE FROM decoded", pool="Test", db="test.db")

    def test_entries_fields_and_filters_report_scan_honestly(self):
        for i in range(4):
            self.put(key=f"k{i}", codes=[{"Title": f"Code {i}"}])
        got = m.entries(pool="Test", db="test.db", fields=["Codes[*].Title"],
                        filters={"Codes[*].Title": {"op": "contains", "value": "Code"}},
                        limit=2, scan=3)
        self.assertEqual(got["matched"], 2)
        self.assertEqual(got["scanned"], 2)
        self.assertFalse(got["complete"])          # scan limit was below the row count
        self.assertNotIn("value", got["rows"][0])
        self.assertEqual(got["rows"][0]["fields"], {"Codes[*].Title": "Code 0"})

    def test_entries_without_projection_is_unchanged(self):
        self.put()
        got = m.entries(pool="Test", db="test.db", limit=5)
        self.assertNotIn("scanned", got)
        self.assertIn("value", got["rows"][0])


class CacheKeyTests(unittest.TestCase):
    def test_index_paths_are_per_device(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(m, "CACHE", Path(tmp)), \
                 patch.object(m, "_resolve_pool", lambda *a, **kw: {"db": "d", "table": "t", "name": "P",
                                                                    "applicationId": 1, "poolId": 2}):
                with patch.object(m, "SERIAL", "emulator-5554"):
                    a = m._index_spec("com.x", "d", None, None, "P", None, "en", None)["path"]
                with patch.object(m, "SERIAL", "emulator-5556"):
                    b = m._index_spec("com.x", "d", None, None, "P", None, "en", None)["path"]
            self.assertNotEqual(a, b)
            self.assertIn("emulator-5554", a)

    def test_disk_caches_are_per_device(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(m, "CACHE", Path(tmp)):
                with patch.object(m, "SERIAL", "one"):
                    a = m._disk_cache("com.x", "databases.json")
                with patch.object(m, "SERIAL", "two"):
                    b = m._disk_cache("com.x", "databases.json")
            self.assertNotEqual(a, b)


class DiffTests(unittest.TestCase):
    def test_field_level_diff(self):
        before = {"Title": "A", "Status": "OPEN", "Gone": "x"}
        after = {"Title": "B", "Status": "OPEN", "New": "y"}
        d = m._diff_values(before, after, "en")
        self.assertEqual(d["changed"], [{"path": "Title", "before": "A", "after": "B"}])
        self.assertEqual(d["added"], [{"path": "New", "after": "y"}])
        self.assertEqual(d["removed"], [{"path": "Gone", "before": "x"}])

    def test_identity_survives_sqlite_text_affinity(self):
        # #thingId is an int on the device and a string after a round trip through a TEXT column
        self.assertEqual(m._obj_ident("t", "k", 0), m._obj_ident("t", "k", "0"))
        self.assertEqual(m._obj_ident("t", "k", None), ("t", "k", ""))
        self.assertNotEqual(m._obj_ident("t", "k", 0), m._obj_ident("t", "k", 1))

    def test_unchanged_state_reports_no_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = [{"pool": "P", "db": "d", "tbl": "t", "key": "k1", "thing": 0,
                     "last_change": 5, "title": "T", "json": '{"a": 1}'}]
            targets = [{"name": "P", "db": "d", "table": "t", "rows": 1, "exists": True}]
            with patch.object(m, "CACHE", Path(tmp)), \
                 patch.object(m, "_pkg", lambda p: "com.test"), \
                 patch.object(m, "_target_pools", lambda *a, **kw: targets), \
                 patch.object(m, "_read_state", lambda *a, **kw: rows), \
                 patch.object(m, "_decode_plan", lambda *a, **kw: {"t"}):
                marked = m.mark(label="t")
                self.assertEqual(marked["objects"], 1)
                got = m.changes_since()
            self.assertEqual(got["summary"], {"added": 0, "removed": 0, "changed": 0, "unchanged": 1})

    def test_decode_plan_respects_size_caps(self):
        targets = [{"table": "small", "rows": 10}, {"table": "huge", "rows": 10 ** 6}]
        plan = m._decode_plan(targets, "auto")
        self.assertEqual(plan, {"small"})
        self.assertEqual(m._decode_plan(targets, "never"), set())
        self.assertEqual(len(m._decode_plan(targets, "always")), 2)


class TitleTests(unittest.TestCase):
    def t(self, obj, locale="en"):
        return m.bo_codec.display_title(obj, locale)

    def test_top_level_title_wins(self):
        self.assertEqual(self.t({"Title": "Direct", "Text": {"Title": "Nested"}}), "Direct")

    def test_nested_title_is_found(self):
        # a SmartForm keeps its label at Text.Title, not Title
        self.assertEqual(self.t({"ExternalId": "x", "Text": {"Title": "From Text"}}), "From Text")

    def test_first_list_element_is_searched(self):
        self.assertEqual(self.t({"Translations": [{"Title": "From list"}]}), "From list")

    def test_nested_translations_resolve_to_locale(self):
        obj = {"Text": {"Title": [{"locale": "de", "value": "Hallo"}, {"locale": "en", "value": "Hi"}]}}
        self.assertEqual(self.t(obj, "de"), "Hallo")
        self.assertEqual(self.t(obj, "en"), "Hi")

    def test_no_title_and_no_blob_traversal(self):
        self.assertIsNone(self.t({"ExternalId": "x"}))
        self.assertIsNone(self.t({"a": {"$b64": "zz"}}))
        self.assertIsNone(self.t("not an object"))



class RegressionFixTests(unittest.TestCase):
    """Each test here pins a defect found in review. Names say what used to go wrong."""

    def test_serial_failure_is_not_cached_forever(self):
        m._device_ident.clear()
        with patch.object(m, "SERIAL", None), patch.object(m, "_adb", return_value="unknown"):
            self.assertEqual(m._serial(), "unknown-device")
        self.assertNotIn("serial", m._device_ident)          # it must ask again next time
        with patch.object(m, "SERIAL", None), patch.object(m, "_adb", return_value="emulator-5554\n"):
            self.assertEqual(m._serial(), "emulator-5554")
        m._device_ident.clear()

    def test_shutdown_keeps_the_startup_locks(self):
        key = ("adb", "s", "com.x", "run-as")
        m._server_locks[key] = threading.RLock()
        held = m._server_locks[key]
        m._shutdown_runners()
        self.assertIs(m._server_locks.get(key), held)         # clearing them leaked a second runner

    def test_resolve_pool_rejects_an_empty_name(self):
        with self.assertRaisesRegex(ValueError, "pool name is required"):
            m._resolve_pool("com.x", "")

    def test_schema_never_sends_an_empty_statement(self):
        seen = []

        def read(package, db, sql, limit=200, **kw):
            seen.append(sql)
            if "sqlite_master" in sql and "type, name" in sql:
                return {"columns": ["type", "name", "tbl_name", "sql"],
                        "rows": [["table", "t", "t", "CREATE TABLE t(a)"]], "backend": "x"}
            if "pragma_table_info" in sql:
                return {"columns": [], "rows": [["t", 0, "a", "TEXT", 0, None, 0]], "backend": "x"}
            if "pragma_foreign_key_list" in sql:
                return {"columns": [], "rows": [], "backend": "x"}
            return {"columns": [], "rows": [["t", 0]], "backend": "x"}     # COUNT(*) -> 0 rows

        with patch.object(m, "_read", read), patch.object(m, "_pkg", lambda p: "com.x"):
            m.schema(db="app.db", stats=True)
        self.assertTrue(all(s.strip() for s in seen), "an empty SQL string reached the runner")

    def test_title_cache_does_not_remember_a_failed_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(m, "CACHE", Path(tmp)):
                m._title_cache.clear()
                calls = []

                def failing(*a, **kw):
                    calls.append(1)
                    raise RuntimeError("device went away")

                with patch.object(m, "_read", failing):
                    self.assertEqual(m._resolve_titles("com.x", "d", "t", ["k1"], "en"), {"k1": m._MISSING})
                with patch.object(m, "_read", lambda *a, **kw: {"columns": [], "rows": [["k1", None]]}), \
                     patch.object(m, "_decode_cell", lambda v: {"Title": "Found"}):
                    got = m._resolve_titles("com.x", "d", "t", ["k1"], "en")
                self.assertEqual(got, {"k1": "Found"})        # used to stay _MISSING for the process
                m._title_cache.clear()

    def test_reference_lines_distinguish_untitled_from_absent(self):
        rules = [{"path": "AssetId", "field": "AssetId", "target_pool": "Asset",
                  "target_db": "d", "target_table": "t"}]
        exists = m._expand_refs("p", {"AssetId": "aaa-1"}, rules, "en", {("d", "t", "aaa-1"): None})
        gone = m._expand_refs("p", {"AssetId": "bbb-1"}, rules, "en", {("d", "t", "bbb-1"): m._MISSING})
        self.assertEqual(m._ref_lines(exists), ["Asset: aaa-1"])
        self.assertEqual(m._ref_lines(gone), ["AssetId: bbb-1 (no Asset object)"])

    def test_one_letter_prefixes_are_not_treated_as_references(self):
        # "AId" would make every two-character field a reference candidate
        self.assertIsNone(m._ref_base("AId"))
        self.assertEqual(m._ref_base("AsId"), "As")


class EntriesPaginationTests(DatabaseTests):
    def test_a_scan_window_with_no_matches_still_returns_a_cursor(self):
        for i in range(1, 7):
            self.put(key=f"k{i}", codes=[{"Title": "Match" if i == 6 else "Other"}])
        page = m.entries(pool="Test", db="test.db", limit=2, scan=3,
                         filters={"Codes[*].Title": {"op": "eq", "value": "Match"}})
        self.assertEqual(page["row_count"], 0)
        self.assertTrue(page["truncated"])
        self.assertIsNotNone(page["next_after_rowid"], "no cursor: the rest of the pool is unreachable")
        rest = m.entries(pool="Test", db="test.db", limit=2, scan=10,
                         after_rowid=page["next_after_rowid"],
                         filters={"Codes[*].Title": {"op": "eq", "value": "Match"}})
        self.assertEqual([r["key"] for r in rest["rows"]], ["k6"])

    def test_cursor_is_the_last_scanned_row_not_the_last_match(self):
        for i in range(1, 5):
            self.put(key=f"k{i}", codes=[{"Title": "Match" if i == 1 else "Other"}])
        page = m.entries(pool="Test", db="test.db", limit=1, scan=4,
                         filters={"Codes[*].Title": {"op": "eq", "value": "Match"}})
        if page["next_after_rowid"] is not None:
            self.assertGreaterEqual(page["next_after_rowid"], page["rows"][-1]["rowid"])


class SnapshotCoalesceTests(unittest.TestCase):
    def test_a_burst_of_reads_shares_one_pull(self):
        with tempfile.TemporaryDirectory() as tmp:
            pulls = []

            def fake_exec(package, db, cmd, binary=False, check=True):
                pulls.append(cmd)
                return b"" if binary else ""

            with patch.object(m, "CACHE", Path(tmp)), patch.object(m, "_pkg", lambda p: "com.x"), \
                 patch.object(m, "_exec_for", fake_exec), patch.object(m, "_device_has_sqlite3", lambda p: False), \
                 patch.object(m, "SERIAL", "dev1"):
                first = m._snapshot("com.x", "app.db")
                n_after_first = len(pulls)
                second = m._snapshot("com.x", "app.db")
                self.assertTrue(second.get("reused"), "the second read pulled the database again")
                self.assertEqual(len(pulls), n_after_first)
                forced = m._snapshot("com.x", "app.db", force=True)
                self.assertFalse(forced.get("reused"), "snapshot(force=True) must always pull")
                self.assertGreater(len(pulls), n_after_first)


class WriteGuardTests(unittest.TestCase):
    def setUp(self):
        for name, value in (("WRITE_MODE", "on"), ("WRITE_ALLOW", [])):
            p = patch.object(m, name, value); p.start(); self.addCleanup(p.stop)

    def test_targets_are_read_from_the_statement(self):
        self.assertEqual(m._write_targets("UPDATE MobileUser SET a=1"), ["MobileUser"])
        self.assertEqual(m._write_targets('DELETE FROM "Odd#Name" WHERE x'), ["Odd#Name"])
        self.assertEqual(m._write_targets("INSERT OR REPLACE INTO t(a) VALUES (1)"), ["t"])
        self.assertEqual(m._write_targets("CREATE TABLE t(a)"), [])

    def test_off_refuses_every_write(self):
        with patch.object(m, "WRITE_MODE", "off"), self.assertRaisesRegex(RuntimeError, "disabled"):
            m._check_write("UPDATE t SET a=1", False)

    def test_dry_run_mode_forces_a_rehearsal(self):
        with patch.object(m, "WRITE_MODE", "dry-run"):
            self.assertTrue(m._check_write("UPDATE t SET a=1", False))

    def test_allow_list_blocks_other_tables(self):
        with patch.object(m, "WRITE_ALLOW", ["Allowed"]):
            self.assertFalse(m._check_write("UPDATE Allowed SET a=1", False))
            with self.assertRaisesRegex(RuntimeError, "not in ANDROID_DB_WRITE_ALLOW"):
                m._check_write("UPDATE Other SET a=1", False)
            with self.assertRaisesRegex(RuntimeError, "no target table"):
                m._check_write("CREATE TABLE t(a)", False)

    def test_dry_run_is_sent_to_the_runner_as_dryrun(self):
        seen = {}

        def fake(package, db, sql, mode="read", limit=200, blobs="summary"):
            seen["mode"] = mode
            return {"ok": True, "changes": 3}

        with patch.object(m, "_pkg", lambda p: "com.x"), patch.object(m, "_jar_available", lambda p, db=None: True), \
             patch.object(m, "_device_sql", fake):
            res = m.execute(sql="UPDATE t SET a=1", db="app.db", dry_run=True)
        self.assertEqual(seen["mode"], "dryrun")
        self.assertTrue(res["dry_run"])
        self.assertIn("Rolled back", res["note"])

    def test_dry_run_refuses_the_sqlite3_fallback(self):
        with patch.object(m, "_pkg", lambda p: "com.x"), patch.object(m, "_jar_available", lambda p, db=None: False), \
             patch.object(m, "_device_has_sqlite3", lambda p: True):
            with self.assertRaisesRegex(RuntimeError, "dry_run needs the jar runner"):
                m.execute(sql="UPDATE t SET a=1", db="app.db", dry_run=True)


class RedactionTests(unittest.TestCase):
    def test_secret_looking_fields_are_replaced(self):
        hits = set()
        got = m._redact({"username": "u", "passwordHash": "h", "Nested": {"apiKey": "k", "ok": 1}}, hits)
        self.assertEqual(got, {"username": "u", "passwordHash": m.REDACTED,
                               "Nested": {"apiKey": m.REDACTED, "ok": 1}})
        self.assertEqual(hits, {"passwordHash", "Nested.apiKey"})

    def test_structure_is_preserved_in_lists(self):
        got = m._redact({"Items": [{"token": "t", "id": "1"}]})
        self.assertEqual(got["Items"][0], {"token": m.REDACTED, "id": "1"})

    def test_columns_are_redacted_by_name(self):
        rows, hidden = m._redact_rows(["username", "token"], [["u", "abc"], ["v", None]])
        self.assertEqual(rows, [["u", m.REDACTED], ["v", None]])
        self.assertEqual(hidden, ["token"])

    def test_words_are_matched_whole_not_as_substrings(self):
        for keep in ("Passes", "Bypass", "CompassBearing", "PassengerCount", "Signage",
                     "AuthorName", "Title", "remember"):
            self.assertFalse(m._is_secret(keep), f"{keep} must not be redacted")
        for hide in ("password", "passwordHash", "apiKey", "api_key", "token", "authToken",
                     "sessionId", "client_secret", "SignatureBlob", "privateKey"):
            self.assertTrue(m._is_secret(hide), f"{hide} must be redacted")

    def test_an_explicit_regex_overrides_the_word_list(self):
        import re as _re
        with patch.object(m, "_REDACT", _re.compile("(?i)internal")):
            self.assertTrue(m._is_secret("InternalNote"))
            self.assertFalse(m._is_secret("password"))

    def test_nothing_happens_when_disabled(self):
        with patch.object(m, "_REDACT", None), patch.object(m, "REDACTION_ON", False):
            obj = {"password": "p"}
            self.assertEqual(m._redact(obj), obj)
            self.assertEqual(m._redact_rows(["password"], [["p"]]), ([["p"]], []))

    def test_ordinary_fields_are_untouched(self):
        self.assertEqual(m._redact({"Title": "x", "Passes": 3}), {"Title": "x", "Passes": 3})
        self.assertEqual(m._redact({"PassengerCount": 4}), {"PassengerCount": 4})

class SpliceTests(unittest.TestCase):
    """In-place editing must not retype fields. decode()->encode() does: a LONG comes back an INT,
    which hands the app's Java deserializer the wrong class."""

    # {"Duration": <LONG 0>, "Title": "test", "Items": [{"ItemId": "0001"}]}
    BLOB = (bytes([m.bo_codec.T_MAP]) + struct.pack(">i", 3)
            + bytes([m.bo_codec.T_STRING]) + struct.pack(">H", 8) + b"Duration"
            + bytes([m.bo_codec.T_LONG]) + struct.pack(">q", 0)
            + bytes([m.bo_codec.T_STRING]) + struct.pack(">H", 5) + b"Title"
            + bytes([m.bo_codec.T_STRING]) + struct.pack(">H", 4) + b"test"
            + bytes([m.bo_codec.T_STRING]) + struct.pack(">H", 5) + b"Items"
            + bytes([m.bo_codec.T_LIST]) + struct.pack(">i", 1)
            + bytes([m.bo_codec.T_MAP]) + struct.pack(">i", 1)
            + bytes([m.bo_codec.T_STRING]) + struct.pack(">H", 6) + b"ItemId"
            + bytes([m.bo_codec.T_STRING]) + struct.pack(">H", 4) + b"0001")

    def test_encode_does_not_round_trip(self):
        self.assertNotEqual(m.bo_codec.encode(m.bo_codec.decode(self.BLOB)), self.BLOB)

    def test_splice_keeps_the_long_tag(self):
        out = m.bo_codec.splice(self.BLOB, "Duration", 7)
        self.assertEqual(m.bo_codec.spans(out)["Duration"][2], m.bo_codec.T_LONG)
        self.assertEqual(m.bo_codec.decode(out)["Duration"], 7)

    def test_splice_touches_only_the_target(self):
        emptied = m.bo_codec.splice(self.BLOB, "Items", [])
        self.assertEqual(m.bo_codec.decode(emptied)["Items"], [])
        restored = m.bo_codec.splice(emptied, "Items", m.bo_codec.decode(self.BLOB)["Items"])
        self.assertEqual(restored, self.BLOB)

    def test_nested_path(self):
        out = m.bo_codec.splice(self.BLOB, "Items[0].ItemId", "0002")
        self.assertEqual(m.bo_codec.decode(out)["Items"][0]["ItemId"], "0002")

    def test_unknown_path_is_refused(self):
        with self.assertRaises(KeyError):
            m.bo_codec.splice(self.BLOB, "Nope", 1)


class JarAvailabilityTests(unittest.TestCase):
    """An external-storage db is reached as `shell` via /data/local/tmp/dbq.jar; only a private-dir
    db needs the sandbox copy. Demanding both sent every read to the (stale, whole-file) snapshot."""

    EXTERNAL = "/storage/emulated/0/Android/data/com.x/files/t/databases/t#BusinessObject.db"

    def _probe(self, sandbox: str, tmp: str):
        with patch.object(m, "_jar_state", {}), patch.object(m, "BACKEND", "auto"), \
             patch.object(m, "_in_sandbox", lambda p, c, check=True: sandbox), \
             patch.object(m, "_as_shell", lambda c, check=True: tmp):
            return (m._jar_available("com.x", self.EXTERNAL), m._jar_available("com.x", None))

    def test_external_db_needs_only_the_tmp_copy(self):
        self.assertEqual(self._probe(sandbox="no", tmp="yes"), (True, False))

    def test_private_db_needs_only_the_sandbox_copy(self):
        self.assertEqual(self._probe(sandbox="yes", tmp="no"), (False, True))

    def test_forget_jar_clears_every_copy(self):
        with patch.object(m, "_jar_state", {"com.x:a": True, "com.x:b": False, "com.y:a": True}):
            m._forget_jar("com.x")
            self.assertEqual(m._jar_state, {"com.y:a": True})


class PoolsFilterTests(unittest.TestCase):
    """pools() returned every pool with its promoted-fields block — 141 KB of context to learn one
    table name. `name` narrows it and `named_fields` is opt-in."""

    POOLS = [{"name": "Ticket", "table": "t1", "applicationId": 1, "poolId": 10, "exists": True},
             {"name": "TicketSettings", "table": "t2", "applicationId": 1, "poolId": 11, "exists": True},
             {"name": "Order", "table": "t3", "applicationId": 1, "poolId": 12, "exists": True}]

    def _pools(self, **kw):
        with patch.object(m, "_pkg", lambda p: "com.x"), \
             patch.object(m, "_bo_databases", lambda p: ["db1"]), \
             patch.object(m, "_pools_in", lambda p, d, r=False: self.POOLS), \
             patch.object(m, "_pool_counts", lambda p, d, tables: {t: 5 for t in tables}), \
             patch.object(m, "_all_named_fields", lambda p, d: {(1, 10): ["a", "b"]}):
            return m.pools(**kw)

    def test_name_is_a_case_insensitive_substring(self):
        self.assertEqual([p["name"] for p in self._pools(name="ticket")],
                         ["Ticket", "TicketSettings"])

    def test_no_name_returns_every_pool(self):
        self.assertEqual(len(self._pools()), 3)

    def test_named_fields_is_off_by_default(self):
        self.assertNotIn("named_fields", self._pools(name="Ticket")[0])

    def test_named_fields_opt_in(self):
        self.assertEqual(self._pools(name="Ticket", fields=True)[0]["named_fields"], ["a", "b"])

    def test_rows_survive_the_filter(self):
        self.assertEqual(self._pools(name="Order")[0]["rows"], 5)


if __name__ == "__main__":
    unittest.main()
