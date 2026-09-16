"""Offline regression tests: real SQLite, fake ADB and deterministic embeddings."""
import asyncio
import concurrent.futures
import importlib
import json
import os
from pathlib import Path
import sqlite3
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

    def test_keyword_and_semantic_tools_after_edit(self):
        self.put()
        kwargs = {"pool": "Test", "chunk": "Codes", "db": "test.db"}
        with patch.object(m, "_embedder", return_value=FakeEmbedder()):
            first = m._TOOLS["semantic_search"]("original", **kwargs)
            self.assertEqual(first["hits"][0]["meta"]["title"], "Original")
            self.put(change=2, codes=[{"Title": "Replacement"}])
            keyword = m._TOOLS["search"]("Replacement", **kwargs)
            self.assertEqual(keyword["hits"][0]["meta"]["title"], "Replacement")
            semantic = m._TOOLS["semantic_search"]("replacement", **kwargs)
            self.assertEqual(semantic["hits"][0]["meta"]["title"], "Replacement")
            self.assertEqual(semantic["index"]["missing_vectors"], 0)

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
m._jar_available=lambda p:True
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


if __name__ == "__main__":
    unittest.main()
