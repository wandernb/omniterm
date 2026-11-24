import importlib.util
import json
import math
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path


def load(name, filename):
    specification = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / "helpers" / filename)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


KNOWLEDGE = load("omni_knowledge_for_storage_tests", "omni_knowledge.py")
SEMANTICS = load("omni_semantics_for_storage_tests", "omni_semantics.py")
STORAGE = load("omni_storage_for_tests", "omni_storage.py")
DAY = 86400.0


class Embedding:
    available = True
    model_id = "test_storage:d2"
    dimensions = 2

    def __init__(self):
        self.calls = []

    def status(self):
        return {"available": True, "model_id": self.model_id, "dimensions": self.dimensions}

    def embed(self, text):
        self.calls.append(text)
        return [1 / math.sqrt(2), 1 / math.sqrt(2)]


def stamp(days_ago):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days_ago * DAY))


class MeasureTests(unittest.TestCase):
    def test_report_splits_bytes_by_category_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.sqlite"
            connection = sqlite3.connect(path)
            connection.executescript(
                "CREATE TABLE records(key TEXT PRIMARY KEY,value TEXT);"
                "CREATE TABLE messages(id TEXT PRIMARY KEY,text TEXT);"
                "CREATE INDEX message_text ON messages(text);"
                "CREATE TABLE vectors(key TEXT PRIMARY KEY,vector BLOB);"
                "CREATE VIRTUAL TABLE message_fts USING fts5(text);")
            for index in range(200):
                connection.execute("INSERT INTO records VALUES(?,?)", ("k%d" % index, "x" * 500))
                connection.execute("INSERT INTO messages VALUES(?,?)", ("m%d" % index, "y" * 300))
                connection.execute("INSERT INTO vectors VALUES(?,?)", ("v%d" % index, b"\0" * 1024))
                connection.execute("INSERT INTO message_fts VALUES(?)", ("some words %d" % index,))
            connection.commit()
            connection.close()
            before = path.read_bytes()
            report = STORAGE.measure(path)
            self.assertEqual(path.read_bytes(), before)
        self.assertNotIn("error", report)
        self.assertEqual(report["total_bytes"], report["page_size"] * report["page_count"])
        for name in ("records", "messages", "vectors", "fts", "indexes"):
            self.assertGreater(report["bytes"][name], 0, name)
        self.assertLessEqual(sum(report["bytes"].values()), report["total_bytes"])
        self.assertGreater(report["bytes"]["vectors"], report["bytes"]["messages"])

    def test_categories(self):
        self.assertEqual(STORAGE.category("message_fts_data", "table", "message_fts_data"), "fts")
        self.assertEqual(STORAGE.category("sqlite_autoindex_records_1", "index", "records"), "indexes")
        self.assertEqual(STORAGE.category("semantic_chunks", "table", "semantic_chunks"), "vectors")
        self.assertEqual(STORAGE.category("sources", "table", "sources"), "other")

    def test_a_slow_report_is_abandoned(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.sqlite"
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE records(key TEXT PRIMARY KEY,value TEXT)")
            connection.executemany("INSERT INTO records VALUES(?,?)", (("k%d" % i, "v" * 900) for i in range(3000)))
            connection.commit()
            connection.close()
            ticks = iter(range(0, 10 ** 9, 1000))
            report = STORAGE.measure(path, deadline_seconds=1, clock=lambda: next(ticks), progress_ops=50)
        self.assertIn("error", report)


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.store = KNOWLEDGE.KnowledgeStore(roots=[], semantics=False)
        self.addCleanup(self.store.close)

    def report(self, total_gb, computed_at=None):
        self.store._document(STORAGE.REPORT_DOCUMENT, {"computed_at": computed_at or time.time(),
                                                       "total_bytes": int(total_gb * STORAGE.GB)})

    def test_crossing_the_budget_notifies_once_until_it_falls_back_under(self):
        monitor = self.store.storage
        self.report(10)
        self.assertIsNone(monitor.tick())
        self.report(31)
        notify = monitor.tick()
        self.assertEqual((notify["severity"], notify["dedupe_key"]), ("attention", "knowledge-storage-budget"))
        self.assertIn("31.0 GB", notify["body"])
        self.assertIsNone(monitor.tick())
        self.assertTrue(self.store.knowledge_status()["storage"]["over_budget"])
        self.report(29)
        self.assertIsNone(monitor.tick())
        self.report(32)
        self.assertIsNotNone(monitor.tick())

    def test_budget_setting(self):
        self.report(31)
        result = self.store.request({"op": "configure_storage", "budget_gb": 50})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["storage"]["budget_gb"], 50)
        self.assertIsNone(self.store.storage.tick())
        bad = self.store.request({"op": "configure_storage", "budget_gb": 0})
        self.assertFalse(bad["ok"])
        self.assertEqual(self.store.storage.settings()["budget_gb"], 50)

    def test_a_due_report_runs_off_the_worker_and_is_collected_by_a_later_tick(self):
        calls = []
        monitor = STORAGE.StorageMonitor(self.store, measure_function=lambda path: calls.append(path) or
                                         {"computed_at": time.time(), "total_bytes": 5})
        self.store._read_index = Path("/nonexistent/index.sqlite")
        try:
            monitor.tick()
            monitor._thread.join(5)
            self.assertEqual(calls, [Path("/nonexistent/index.sqlite")])
            monitor.tick()
            self.assertEqual(self.store._document_value(STORAGE.REPORT_DOCUMENT)["total_bytes"], 5)
            monitor.tick()  # fresh: not measured again today
            self.assertEqual(len(calls), 1)
        finally:
            self.store._read_index = None


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.store = KNOWLEDGE.KnowledgeStore(roots=[], semantics=False)
        self.addCleanup(self.store.close)
        self.embedding = Embedding()
        self.index = SEMANTICS.SemanticIndex(self.store, embedding=self.embedding)
        self.store._semantics = self.index

    def message(self, identifier, role="assistant", days_ago=400, provider="claude"):
        self.store._put_source({"key": "source:" + identifier, "session_id": provider + ":" + identifier,
                                "provider": provider, "path": "/t/" + identifier + ".jsonl", "workspace": "/w",
                                "started": 1, "updated": 1, "archived": 0})
        self.store._put_message({"id": identifier, "source_key": "source:" + identifier,
                                 "session_id": provider + ":" + identifier, "provider": provider, "workspace": "/w",
                                 "source_path": "/t/" + identifier + ".jsonl", "source_line": 1, "part": 0,
                                 "role": role, "kind": "message", "text": "text of " + identifier,
                                 "timestamp": stamp(days_ago)})
        self.store.connection.commit()

    def manifest(self, identifier):
        return self.store._document_value(SEMANTICS.vector_prefix("transcript", identifier) + "manifest")

    def test_retention_drops_only_old_uncited_assistant_vectors_and_keeps_them_dropped(self):
        self.message("old-assistant")
        self.message("old-user", role="user")
        self.message("old-cited")
        self.message("old-kb", provider="kb")
        self.message("recent-assistant", days_ago=10)
        self.store.upsert_memory({"title": "Cited", "body": "Kept.",
                                  "sources": [{"session_id": "claude:old-cited", "message_id": "old-cited"}]})
        for _ in range(5):
            self.index.ensure_vectors(max_items=16)
        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) FROM semantic_chunks WHERE corpus='transcript'").fetchone()[0], 5)
        state = {"retention_running": True, "retention_cursor": ""}
        monitor = self.store.storage
        self.assertFalse(monitor.retention_step(state, time.time(), self.index))
        self.assertFalse(state["retention_running"])
        self.assertEqual(state["retention_dropped"], 1)
        kept = {row[0] for row in self.store.connection.execute(
            "SELECT record_id FROM semantic_chunks WHERE corpus='transcript'")}
        self.assertEqual(kept, {"old-user", "old-cited", "old-kb", "recent-assistant"})
        self.assertIs(self.manifest("old-assistant")["retained"], False)
        self.assertTrue(self.store.connection.execute("SELECT 1 FROM messages WHERE id='old-assistant'").fetchone())
        # The embedder does not put the dropped vectors back.
        self.embedding.calls.clear()
        self.index._cursors["transcript"] = ""
        for _ in range(5):
            self.index.ensure_vectors(max_items=16)
        self.assertEqual(self.embedding.calls, [])

    def test_over_budget_tick_runs_the_pass_once_per_report(self):
        self.message("old-assistant")
        for _ in range(3):
            self.index.ensure_vectors(max_items=16)
        self.store._document(STORAGE.REPORT_DOCUMENT, {"computed_at": time.time(), "total_bytes": 40 * STORAGE.GB})
        result = self.store.request({"op": "memory_tick"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["notify"]["dedupe_key"], "knowledge-storage-budget")
        status = self.store.knowledge_status()["storage"]
        self.assertEqual(status["retention"]["retention_dropped"], 1)
        self.assertFalse(status["retention"]["retention_running"])
        self.assertNotIn("notify", self.store.request({"op": "memory_tick"}))


if __name__ == "__main__":
    unittest.main()
