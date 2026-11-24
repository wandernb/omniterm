import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest


ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("knowledge_count_tests", ROOT / "helpers/omni_knowledge.py")
KNOWLEDGE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(KNOWLEDGE)


class KnowledgeCountTests(unittest.TestCase):
    def test_status_counts_overlay_replacements_and_deletions_without_reading_payloads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "knowledge.sqlite"
            writer = sqlite3.connect(path)
            self.addCleanup(writer.close)
            writer.execute("CREATE TABLE records(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            writer.executescript((ROOT / "helpers/omni_knowledge_schema.sql").read_text())

            def message(identifier, text):
                return {"id": identifier, "source_key": "source:fixture", "session_id": "claude:fixture",
                        "provider": "claude", "source_path": "/fixture.jsonl", "source_line": 1,
                        "part": 0, "role": "user", "kind": "message", "text": text}

            def memory(identifier):
                return {"id": identifier, "title": "Title", "body": "Memory payload", "sources": [],
                        "tags": [], "revision": 1, "created": 1, "updated": 1}

            with writer:
                for identifier in ("kept", "replaced", "deleted"):
                    writer.execute("INSERT INTO records VALUES(?,?)",
                                   ("message:" + identifier, json.dumps(message(identifier, "Persisted payload"))))
                    writer.execute("INSERT INTO records VALUES(?,?)",
                                   ("memory:" + identifier, json.dumps(memory(identifier))))
            store = KNOWLEDGE.KnowledgeStore(roots=[], home=directory, read_index=path, semantics=False)
            self.addCleanup(store.close)
            store._put_message(message("replaced", "Overlay replacement"))
            store._put_message(message("added", "Overlay addition"))
            store._document("message:deleted", None)
            store._put_memory(memory("replaced"))
            store._put_memory(memory("added"))
            store._document("memory:deleted", None)
            store.connection.commit()

            def authorize(action, table, column, database, trigger):
                # Status needs identities/counts, never transcript or memory bodies.
                if action == sqlite3.SQLITE_READ and (
                        (table in ("messages", "overlay_messages") and column == "text") or
                        (table in ("memories", "overlay_memories") and column in ("title", "body"))):
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            store.connection.set_authorizer(authorize)
            status = store.knowledge_status()
            self.assertEqual(status["messages"], 3)
            self.assertEqual(status["memories"], 3)
            self.assertEqual(status["sources"], 0)
            self.assertEqual(status["sessions"], 0)


if __name__ == "__main__":
    unittest.main()
