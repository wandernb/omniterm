"""Filtered lexical search over the persisted index tests ranked rows on a derived
sidecar of filter columns, filled a bounded step per tick, rebuilt when the index is
replaced, and never trusted for the rows returned."""

import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("omni_knowledge_sidecar_tests", ROOT / "helpers/omni_knowledge.py")
KNOWLEDGE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(KNOWLEDGE)


def message(identifier, text, provider="claude", role="assistant", timestamp="2026-11-01T00:00:00Z"):
    return {"id": identifier, "source_key": "source:fixture", "session_id": provider + ":fixture",
            "provider": provider, "workspace": "/w", "source_path": "/fixture.jsonl", "source_line": 1,
            "part": 0, "role": role, "kind": "message", "text": text, "timestamp": timestamp}


class FilterSidecarTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.path = self.directory / "knowledge.sqlite"
        self.writer = sqlite3.connect(self.path)
        self.addCleanup(self.writer.close)
        self.writer.execute("CREATE TABLE records(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        self.writer.executescript((ROOT / "helpers/omni_knowledge_schema.sql").read_text())
        with self.writer:
            for index in range(10):
                provider = "codex" if index % 2 else "claude"
                self.writer.execute("INSERT INTO records VALUES(?,?)", ("message:m%d" % index, json.dumps(
                    message("m%d" % index, "Database unable to open, case %d." % index, provider))))

    def open_store(self):
        store = KNOWLEDGE.KnowledgeStore(roots=[], home=str(self.directory), read_index=self.path, semantics=False,
                                         filter_sidecar=True)
        self.addCleanup(store.close)
        return store

    def search(self, store, query):
        value = store.request({"op": "search_transcripts", "query": query, "limit": 20})
        self.assertTrue(value["ok"], value)
        return sorted(match["id"] for match in value["matches"])

    def test_filtered_results_are_the_same_with_and_without_the_sidecar(self):
        store = self.open_store()
        before = self.search(store, "provider:codex database unable")
        self.assertFalse(store.step_filter_sidecar(rows=4), "a bounded step leaves the rest for later ticks")
        partly = self.search(store, "provider:codex database unable")
        while not store.step_filter_sidecar(rows=4):
            pass
        after = self.search(store, "provider:codex database unable")
        self.assertEqual(before, ["m1", "m3", "m5", "m7", "m9"])
        self.assertEqual((partly, after), (before, before))
        copied = store.connection.execute("SELECT count(*) FROM filters.message_filters").fetchone()[0]
        self.assertEqual(copied, 10)

    def test_the_rows_returned_are_checked_against_the_index(self):
        store = self.open_store()
        while not store.step_filter_sidecar():
            pass
        # A stale sidecar row claims a Claude message is from Codex: it is not returned.
        store.connection.execute("UPDATE filters.message_filters SET provider='codex' WHERE id='m0'")
        self.assertNotIn("m0", self.search(store, "provider:codex database unable"))

    def test_a_replaced_index_rebuilds_the_sidecar(self):
        store = self.open_store()
        while not store.step_filter_sidecar():
            pass
        store.close()
        with self.writer:
            self.writer.execute("DELETE FROM records WHERE key LIKE 'message:%'")
            for index in range(3):
                self.writer.execute("INSERT INTO records VALUES(?,?)", ("message:n%d" % index, json.dumps(
                    message("n%d" % index, "Database unable to open again %d." % index, "codex"))))
        reopened = self.open_store()
        self.assertEqual(reopened._filter_cursor, 0, "the copied rows belonged to another index")
        while not reopened.step_filter_sidecar():
            pass
        self.assertEqual(self.search(reopened, "provider:codex database unable"), ["n0", "n1", "n2"])

    def test_rows_of_deleted_messages_are_swept_from_the_sidecar(self):
        store = self.open_store()
        while not store.step_filter_sidecar():
            pass
        with self.writer:
            self.writer.execute("DELETE FROM records WHERE key IN ('message:m1','message:m4')")
        store.step_filter_sidecar()
        kept = [row[0] for row in store.connection.execute("SELECT id FROM filters.message_filters ORDER BY rid")]
        self.assertEqual(kept, ["m0", "m2", "m3", "m5", "m6", "m7", "m8", "m9"])
        self.assertEqual(self.search(store, "provider:codex database unable"), ["m3", "m5", "m7", "m9"])

    def test_other_stores_read_the_sidecar_without_writing_it(self):
        owner = self.open_store()
        while not owner.step_filter_sidecar():
            pass
        reader = KNOWLEDGE.KnowledgeStore(roots=[], home=str(self.directory), read_index=self.path, semantics=False)
        self.addCleanup(reader.close)
        self.assertEqual(reader._filter_cursor, owner._filter_cursor)
        self.assertTrue(reader.step_filter_sidecar(), "a reader never copies rows")
        self.assertEqual(self.search(reader, "provider:codex database unable"), ["m1", "m3", "m5", "m7", "m9"])

    def test_semantic_candidates_are_rejected_on_the_sidecar(self):
        store = self.open_store()
        while not store.step_filter_sidecar():
            pass
        self.assertIs(store.transcript_filter_verdict("m1", ["m.provider=?"], ["codex"]), True)
        self.assertIs(store.transcript_filter_verdict("m0", ["m.provider=?"], ["codex"]), False)
        self.assertIsNone(store.transcript_filter_verdict("missing", ["m.provider=?"], ["codex"]))
        self.assertIsNone(store.transcript_filter_verdict("m0", [], []), "no conditions: nothing to judge")


if __name__ == "__main__":
    unittest.main()
