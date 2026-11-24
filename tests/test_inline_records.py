import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


_SPEC = importlib.util.spec_from_file_location(
    "omni_knowledge_inline_tests", Path(__file__).parents[1] / "helpers/omni_knowledge.py")
KNOWLEDGE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(KNOWLEDGE)


class InlineRecordTests(unittest.TestCase):
    """Transcripts full of large tool results must not crawl through the stream parser."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name).resolve()
        self.claude = self.home / ".claude/projects"
        self.store = KNOWLEDGE.KnowledgeStore(roots=[("claude", self.claude)], home=self.home, semantics=False,
                                              kb_roots=[])
        self.addCleanup(self.store.close)

    def transcript(self, sizes):
        path = self.claude / "workspace/big.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        for index, size in enumerate(sizes):
            text = ("record %d " % index) + "x" * size
            lines.append(json.dumps({"type": "assistant", "timestamp": "2026-11-10T09:00:00Z", "cwd": "/w",
                                     "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}))
        path.write_text("\n".join(lines) + "\n")
        return path

    def source(self, path):
        return self.store._document_value("source:" + KNOWLEDGE.digest(str(path)))

    def test_records_of_a_few_hundred_kilobytes_are_ingested_in_one_scan(self):
        path = self.transcript([120_000] * 5)
        self.assertGreater(120_000, KNOWLEDGE.STREAM_FEED_BYTES)
        result = self.store.scan(max_files=10, byte_budget=4 * 1024 * 1024)
        source = self.source(path)
        self.assertEqual(source["offset"], path.stat().st_size)
        self.assertIsNone(source.get("stream_key"))
        self.assertEqual(result["backlog_files"], 0)
        texts = [row["text"] for row in self.store.connection.execute(
            "SELECT text FROM messages ORDER BY source_line,part")]
        self.assertEqual(sum(len(text) for text in texts), sum(len("record %d " % i) + 120_000 for i in range(5)))
        self.assertTrue(all(len(text) <= KNOWLEDGE.MAX_TEXT for text in texts))

    def test_records_over_the_inline_bound_still_stream(self):
        path = self.transcript([10, KNOWLEDGE.INLINE_RECORD_BYTES + 1000])
        self.store.scan(max_files=10, byte_budget=4 * 1024 * 1024)
        source = self.source(path)
        self.assertTrue(source.get("stream_key") or source["offset"] == path.stat().st_size)
        for _ in range(50):
            if self.store.scan(max_files=10, byte_budget=4 * 1024 * 1024)["scan_complete"]:
                break
        self.assertEqual(self.source(path)["offset"], path.stat().st_size)
        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) FROM messages WHERE text LIKE 'record 1 %'").fetchone()[0], 1)

    def test_a_stream_key_without_its_job_resumes_instead_of_reindexing(self):
        path = self.transcript([200, 300, 400])
        self.store.scan(max_files=10, byte_budget=4 * 1024 * 1024)
        before = {row["id"] for row in self.store.connection.execute("SELECT id FROM messages")}
        source = self.source(path)
        source["stream_key"] = "metadata:stream_job:gone"
        self.store._put_source(source)
        with path.open("a") as handle:
            handle.write(json.dumps({"type": "assistant", "timestamp": "2026-11-10T09:01:00Z", "cwd": "/w",
                                     "message": {"role": "assistant", "content": [{"type": "text", "text": "appended"}]}}) + "\n")
        deleted = []
        original = self.store._delete_source
        self.store._delete_source = lambda key: deleted.append(key) or original(key)
        self.store.scan(max_files=10, byte_budget=4 * 1024 * 1024)
        self.assertEqual(deleted, [], "The transcript is not re-read from the start")
        after = {row["id"] for row in self.store.connection.execute("SELECT id FROM messages")}
        self.assertTrue(before < after)
        self.assertIsNone(self.source(path).get("stream_key"))
        self.assertEqual(self.source(path)["offset"], path.stat().st_size)

    def test_embedding_runs_as_its_own_request_when_the_scan_skips_it(self):
        self.transcript([100])
        calls = []
        semantics = type("FakeSemantics", (), {"ensure_vectors": lambda _, max_items: calls.append(max_items) or
                                               {"indexed_chunks": max_items}})()
        self.store._semantics = semantics
        self.store.scan(max_files=10, byte_budget=1024 * 1024, embed=False)
        self.assertEqual(calls, [])
        value = self.store.request({"op": "embed"})
        self.assertTrue(value["ok"], value)
        self.assertEqual((calls, value["embedded"]), ([KNOWLEDGE.SCAN_VECTOR_ITEMS], KNOWLEDGE.SCAN_VECTOR_ITEMS))
        self.store.scan(max_files=10, byte_budget=1024 * 1024)
        self.assertEqual(len(calls), 2, "A scan still embeds by default")


if __name__ == "__main__":
    unittest.main()
