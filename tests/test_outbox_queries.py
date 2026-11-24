import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("outbox_query_tests", ROOT / "helpers/omni_knowledge.py")
KNOWLEDGE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(KNOWLEDGE)


class OutboxQueryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "knowledge.sqlite"
        self.writer = sqlite3.connect(path)
        self.addCleanup(self.writer.close)
        self.writer.execute("PRAGMA journal_mode=WAL")
        self.writer.execute("CREATE TABLE records(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        self.writer.executescript((ROOT / "helpers/omni_knowledge_schema.sql").read_text())
        self.messenger = mock.Mock()
        self.messenger.claim.side_effect = lambda receipt: {
            **receipt, "state": "sending", "attempt_id": "attempt-" + receipt["request_id"]}
        self.store = KNOWLEDGE.KnowledgeStore(roots=[], home=directory.name, read_index=path,
                                              semantics=False, messenger=self.messenger)
        self.addCleanup(self.store.close)

    def persist_receipt(self, identifier, state="queued"):
        receipt = {"request_id": identifier, "session_id": "claude:fixture", "state": state}
        self.writer.execute("INSERT INTO records VALUES(?,?)", ("receipt:" + identifier, json.dumps(receipt)))
        self.writer.commit()
        return receipt

    def test_outbox_work_is_bounded_by_receipts_not_unrelated_records(self):
        with self.writer:
            self.writer.executemany("INSERT INTO records VALUES(?,?)",
                                    (("metadata:unrelated:%05d" % index, "{}") for index in range(20000)))
        receipt = self.persist_receipt("target")
        callbacks = 0

        def budget():
            nonlocal callbacks
            callbacks += 1
            return callbacks > 10

        self.store.connection.set_progress_handler(budget, 1000)
        try:
            self.store._advance_outbox()
        finally:
            self.store.connection.set_progress_handler(None, 0)
        self.messenger.claim.assert_called_once_with(receipt)
        self.assertEqual(self.store._document_value("receipt:target")["state"], "sending")

    def test_outbox_preserves_shadows_deletions_and_acknowledged_key_order(self):
        replaced = self.persist_receipt("a")
        self.persist_receipt("b")
        self.store._document("receipt:a", {**replaced, "state": "delivered"}, emit=False)
        self.store._document("receipt:b", None, emit=False)
        waiting = {"request_id": "c", "session_id": "claude:fixture", "state": "queued"}
        self.store._document("receipt:c", waiting)
        revision = self.store._revision
        ready = self.persist_receipt("d")
        self.persist_receipt("e")

        self.store._advance_outbox()
        self.messenger.claim.assert_called_once_with(ready)
        self.assertEqual(self.store._document_value("receipt:c")["state"], "queued")
        with self.writer:
            self.writer.execute("INSERT INTO records VALUES(?,?)", ("receipt:c", json.dumps(waiting)))
        self.store.request({"op": "ack_writes", "revision": revision})
        self.store._advance_outbox()
        self.assertEqual(self.messenger.claim.call_args_list, [mock.call(ready), mock.call(waiting)])
        self.messenger.deliver.assert_not_called()


if __name__ == "__main__":
    unittest.main()
