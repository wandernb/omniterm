import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace


ROOT = Path(__file__).parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "helpers" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


KNOWLEDGE = load("pruning_knowledge_tests", "omni_knowledge.py")
SEMANTICS = load("pruning_semantics_tests", "omni_semantics.py")


class SemanticPruningTests(unittest.TestCase):
    def setUp(self):
        self.store = KNOWLEDGE.KnowledgeStore(roots=[], semantics=False)
        self.addCleanup(self.store.close)
        self.index = SEMANTICS.SemanticIndex(self.store, embedding=SimpleNamespace(model_id="fixture"))
        self.writer = self.store.connection
        self.documents = "documents"

    def seed_healthy_parent(self):
        if self.documents == "records":
            source = {"key": "source:fixture", "session_id": "claude:fixture", "provider": "claude",
                      "path": "/fixture.jsonl"}
            message = {"id": "healthy", "source_key": "source:fixture", "session_id": "claude:fixture",
                       "provider": "claude", "source_path": "/fixture.jsonl", "source_line": 1,
                       "part": 0, "role": "assistant", "kind": "message", "text": "Healthy"}
            self.writer.executemany("INSERT INTO records VALUES(?,?)",
                                    (("source:fixture", json.dumps(source)), ("message:healthy", json.dumps(message))))
            return
        self.writer.execute("INSERT INTO sources(key,session_id,provider,path) VALUES('source:fixture','claude:fixture','claude','/fixture.jsonl')")
        self.writer.execute("INSERT INTO messages(id,source_key,session_id,provider,source_path,source_line,part,role,kind,text) "
                            "VALUES('healthy','source:fixture','claude:fixture','claude','/fixture.jsonl',1,0,'assistant','message','Healthy')")

    def bounded_prune(self):
        callbacks = 0

        def budget():
            nonlocal callbacks
            callbacks += 1
            return callbacks > 50

        self.store.connection.set_progress_handler(budget, 1000)
        try:
            self.index._prune()
        finally:
            self.store.connection.set_progress_handler(None, 0)

    def seed_vectors(self, count):
        self.seed_healthy_parent()
        for number in range(count):
            prefix = "vector:transcript:%064x:" % number
            parent = "absent" if number == count - 1 else "healthy"
            chunk = {"kind": "chunk", "corpus": "transcript", "record_id": parent,
                     "model": "fixture", "dimensions": 1, "vector": [1.0], "content_hash": "fixture",
                     "chunk_index": 0, "chunk": "Healthy"}
            manifest = {"kind": "manifest", "corpus": "transcript", "record_id": parent}
            if self.documents == "documents":
                self.index.restore_vector(prefix + "0", chunk)
            for suffix, value in (("0", chunk), ("manifest", manifest)):
                self.writer.execute("INSERT INTO " + self.documents + " VALUES(?,?)",
                                    (prefix + suffix, json.dumps(value)))
        self.writer.commit()

    def restart_index(self):
        self.store.connection.commit()
        self.index = SEMANTICS.SemanticIndex(self.store, embedding=SimpleNamespace(model_id="fixture"))

    def test_restart_progress_eventually_removes_late_orphans_and_wraps_for_new_deletions(self):
        self.seed_vectors(256)
        orphan = "vector:transcript:%064x:" % 255
        self.bounded_prune()
        self.assertIsNotNone(self.store._document_value(orphan + "manifest"))
        for _ in range(12):
            self.restart_index()
            self.bounded_prune()
        self.assertIsNone(self.store._document_value(orphan + "manifest"))
        self.assertIsNone(self.store.connection.execute("SELECT 1 FROM semantic_chunks WHERE key=?",
                                                       (orphan + "0",)).fetchone())
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM semantic_chunks").fetchone()[0], 255)

        if self.store.readthrough:
            self.store._document("message:healthy", None)
        else:
            self.store.connection.execute("DELETE FROM messages WHERE id='healthy'")
        for _ in range(20):
            self.restart_index()
            self.bounded_prune()
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM semantic_chunks").fetchone()[0], 0)
        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) FROM documents WHERE key>'vector:' AND key<'vector;'").fetchone()[0], 0)

    def test_prune_checkpoint_follows_deletions_in_native_write_order(self):
        self.seed_vectors(64)
        self.index._prune()
        writes = self.store._batch()["_writes"]
        deleted = [position for position, write in enumerate(writes) if write["value"] is None]
        checkpoint = next(position for position, write in enumerate(writes)
                          if write["key"] == "metadata:semantic_prune:v1")
        self.assertTrue(deleted)
        self.assertGreater(checkpoint, max(deleted))

    def test_memory_vectors_respect_current_parent_existence(self):
        memory = {"id": "kept", "title": "Title", "body": "Body", "sources": [], "tags": [],
                  "revision": 1, "created": 1, "updated": 1}
        if self.store.readthrough:
            self.writer.execute("INSERT INTO records VALUES(?,?)", ("memory:kept", json.dumps(memory)))
        else:
            self.store._put_memory(memory)
        for identifier in ("kept", "deleted"):
            prefix = SEMANTICS.vector_prefix("memory", identifier)
            chunk = {"kind": "chunk", "corpus": "memory", "record_id": identifier,
                     "model": "fixture", "dimensions": 1, "vector": [1.0], "content_hash": "fixture",
                     "chunk_index": 0, "chunk": "Body"}
            if not self.store.readthrough:
                self.index.restore_vector(prefix + "0", chunk)
            for suffix, value in (("0", chunk), ("manifest", {"kind": "manifest", "corpus": "memory", "record_id": identifier})):
                self.writer.execute("INSERT INTO " + self.documents + " VALUES(?,?)", (prefix + suffix, json.dumps(value)))
        self.writer.commit()
        self.bounded_prune()
        self.assertIsNotNone(self.store._document_value(SEMANTICS.vector_prefix("memory", "kept") + "manifest"))
        self.assertIsNone(self.store._document_value(SEMANTICS.vector_prefix("memory", "deleted") + "manifest"))
        self.assertEqual([row[0] for row in self.store.connection.execute("SELECT record_id FROM semantic_chunks")], ["kept"])

    def test_healthy_chunks_do_not_require_a_full_corpus_prune_scan(self):
        self.seed_healthy_parent()
        self.writer.executemany("INSERT INTO semantic_chunks VALUES(?,?,?,?,?)",
                                (("vector:transcript:%064x:0" % number, "transcript", "healthy", number, "Healthy")
                                 for number in range(10000)))
        self.writer.commit()
        self.bounded_prune()
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM semantic_chunks").fetchone()[0], 10000)

    def test_healthy_manifests_do_not_require_a_full_document_prune_scan(self):
        self.seed_healthy_parent()
        value = json.dumps({"kind": "manifest", "corpus": "transcript", "record_id": "healthy"})
        self.writer.executemany("INSERT INTO " + self.documents + " VALUES(?,?)",
                                (("vector:transcript:%064x:manifest" % number, value) for number in range(10000)))
        self.writer.commit()
        self.bounded_prune()
        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) FROM documents WHERE key>'vector:' AND key<'vector;'").fetchone()[0], 10000)


class NativeSemanticPruningTests(SemanticPruningTests):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "knowledge.sqlite"
        self.path = path
        self.home = directory.name
        self.writer = sqlite3.connect(path)
        self.addCleanup(self.writer.close)
        self.writer.execute("PRAGMA journal_mode=WAL")
        self.writer.execute("CREATE TABLE records(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        self.writer.executescript((ROOT / "helpers/omni_knowledge_schema.sql").read_text())
        self.store = KNOWLEDGE.KnowledgeStore(roots=[], home=directory.name, read_index=path, semantics=False)
        self.addCleanup(self.store.close)
        self.index = SEMANTICS.SemanticIndex(self.store, embedding=SimpleNamespace(model_id="fixture"))
        self.documents = "records"

    def restart_index(self):
        batch = self.store._batch()
        with self.writer:
            for record in batch["_writes"]:
                if record["value"] is None:
                    self.writer.execute("DELETE FROM records WHERE key=?", (record["key"],))
                else:
                    self.writer.execute("INSERT OR REPLACE INTO records VALUES(?,?)",
                                        (record["key"], json.dumps(record["value"])))
        self.store.request({"op": "ack_writes", "revision": batch["_revision"]})
        self.store.close()
        self.store = KNOWLEDGE.KnowledgeStore(roots=[], home=self.home, read_index=self.path, semantics=False)
        self.addCleanup(self.store.close)
        self.index = SEMANTICS.SemanticIndex(self.store, embedding=SimpleNamespace(model_id="fixture"))

    def test_shadowed_prefix_still_has_bounded_physical_work(self):
        self.seed_healthy_parent()
        self.writer.executemany("INSERT INTO semantic_chunks VALUES(?,?,?,?,?)",
                                (("vector:transcript:%064x:0" % number, "transcript", "healthy", number, "Healthy")
                                 for number in range(10000)))
        self.writer.commit()
        self.store.connection.executemany("INSERT INTO shadow_documents VALUES(?)",
                                         (("vector:transcript:%064x:0" % number,) for number in range(10000)))
        self.bounded_prune()
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM overlay_semantic_chunks").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
