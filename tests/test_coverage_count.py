"""Over the app's native index, semantic coverage is two read-only counts on a
thread of its own, not hours of bounded slices on the worker."""

import importlib.util
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("omni_semantics_for_count", ROOT / "helpers/omni_semantics.py")
SEMANTICS = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(SEMANTICS)
MODEL = "test-model"


def native_index(directory):
    path = Path(directory) / "knowledge.sqlite"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE records(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    connection.executescript((ROOT / "helpers/omni_knowledge_schema.sql").read_text())
    rows = [("u1", "user", "message", "Please keep tabs."), ("a1", "assistant", "message", "Done."),
            ("t1", "tool", "tool", "output"), ("x1", "assistant", "message", "[external_agent_tool_call] ls"),
            ("a2", "assistant", "message", "Another answer.")]
    for identifier, role, kind, text in rows:
        connection.execute("INSERT INTO records(key,value) VALUES(?,?)", ("message:" + identifier, json.dumps({
            "id": identifier, "source_key": "s", "session_id": "claude:s", "provider": "claude", "workspace": "/w",
            "source_path": "/p", "source_line": 1, "part": 0, "role": role, "kind": kind, "text": text,
            "timestamp": None})))
    for identifier, keys, model in (("u1", ["k"], MODEL), ("a1", [], MODEL), ("a2", ["k"], "other")):
        connection.execute("INSERT INTO records(key,value) VALUES(?,?)",
                           ("vector:transcript:" + identifier + ":manifest",
                            json.dumps({"vector_keys": keys, "model": model})))
    connection.commit()
    connection.close()
    return path


class CoverageCountTests(unittest.TestCase):
    def test_counts_embeddable_messages_and_current_manifests(self):
        with tempfile.TemporaryDirectory() as directory:
            path = native_index(directory)
            counted = SEMANTICS.count_coverage(path, MODEL)
        # u1, a1 and a2 are embeddable; tool rows and tool calls recorded as text are not.
        self.assertEqual(counted["embeddable"], 3)
        # Only u1 has vectors from the current model.
        self.assertEqual(counted["embedded"], 1)

    def test_threaded_pass_reports_when_done_and_waits_an_hour(self):
        with tempfile.TemporaryDirectory() as directory:
            path = native_index(directory)
            index = SEMANTICS.SemanticIndex.__new__(SEMANTICS.SemanticIndex)
            documents = {}
            index.store = type("Store", (), {"_document": lambda self, key, value: documents.__setitem__(key, value)})()
            index.embedding = type("Embedding", (), {"model_id": MODEL})()
            index._coverage = {"embeddable": None, "embedded": None, "counted_at": None, "embedded_since": 4,
                               "pass": None, "rejected_chunks": 0}
            self.assertTrue(index._threaded_coverage(path, now=1000.0))
            index._coverage_thread.join(10)
            self.assertFalse(index._threaded_coverage(path, now=1001.0))
            self.assertEqual((index._coverage["embeddable"], index._coverage["embedded"]), (3, 1))
            self.assertEqual(index._coverage["embedded_since"], 0)
            self.assertIn(SEMANTICS.COVERAGE_DOCUMENT, documents)
            self.assertFalse(index._threaded_coverage(path, now=1001.0 + 60), "not due again within the hour")
            self.assertIsNone(getattr(index, "_coverage_thread", None))

    def test_missing_index_counts_nothing(self):
        self.assertIsNone(SEMANTICS.count_coverage(Path(tempfile.gettempdir()) / ("absent-%d.sqlite" % time.time_ns()),
                                                   MODEL))


if __name__ == "__main__":
    unittest.main()
