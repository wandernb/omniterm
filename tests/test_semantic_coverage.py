import importlib.util
import math
import time
import unittest
from pathlib import Path
from unittest import mock


def load(name, filename):
    specification = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / "helpers" / filename)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


KNOWLEDGE = load("omni_knowledge_for_coverage_tests", "omni_knowledge.py")
SEMANTICS = load("omni_semantics_for_coverage_tests", "omni_semantics.py")


class WordEmbedding:
    """Embeds any text with letters; text without letters has no vector (like the English model)."""
    available = True
    model_id = "test_coverage:d2"
    dimensions = 2

    def __init__(self):
        self.calls = []

    def status(self):
        return {"available": True, "model_id": self.model_id, "dimensions": self.dimensions}

    def embed(self, text):
        self.calls.append(text)
        if not any(character.isalpha() for character in text):
            raise SEMANTICS.EMBEDDINGS.EmbeddingUnavailable("No local vector")
        return [1 / math.sqrt(2), 1 / math.sqrt(2)]


def stamp(days_ago):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days_ago * 86400))


class PriorityWalkTests(unittest.TestCase):
    def setUp(self):
        self.store = KNOWLEDGE.KnowledgeStore(roots=[], semantics=False)
        self.addCleanup(self.store.close)
        self.embedding = WordEmbedding()
        self.index = SEMANTICS.SemanticIndex(self.store, embedding=self.embedding)

    def message(self, identifier, text, role="assistant", part=0, days_ago=200, kind="message"):
        self.store._put_source({"key": "source:" + identifier, "session_id": "claude:" + identifier,
                                "provider": "claude", "path": "/t/" + identifier + ".jsonl", "workspace": "/w",
                                "started": 1, "updated": 1, "archived": 0})
        self.store._put_message({"id": identifier, "source_key": "source:" + identifier,
                                 "session_id": "claude:" + identifier, "provider": "claude", "workspace": "/w",
                                 "source_path": "/t/" + identifier + ".jsonl", "source_line": 1, "part": part,
                                 "role": role, "kind": kind, "text": text, "timestamp": stamp(days_ago)})
        self.store.connection.commit()

    def embed_one_at_a_time(self, index=None, steps=10):
        index = index or self.index
        for _ in range(steps):
            index.ensure_vectors(max_items=1)

    def corpus(self):
        # Inserted oldest-ingested first; ids sort opposite to the priority order.
        self.message("a-old-assistant", "old assistant answer")
        self.message("b-recent-assistant", "recent assistant answer", days_ago=2)
        self.message("c-old-user-part1", "second part of an old user turn", role="user", part=1)
        self.message("d-tool", "tool output", role="tool", kind="tool")
        self.message("e-old-user", "old user question", role="user")
        self.message("f-new-user", "new user question", role="user", days_ago=1)

    def test_user_turns_then_recent_messages_then_the_id_order_rest(self):
        self.corpus()
        self.embed_one_at_a_time()
        self.assertEqual(self.embedding.calls, [
            "new user question", "old user question",  # the person's turns, newest ingested first
            "recent assistant answer",                  # then the last 14 days
            "old assistant answer", "second part of an old user turn"])  # then id order; tool rows never

    def test_priority_walk_resumes_after_a_restart_and_picks_up_new_turns_first(self):
        self.corpus()
        self.index.ensure_vectors(max_items=1)
        self.assertEqual(self.embedding.calls, ["new user question"])
        restarted = SEMANTICS.SemanticIndex(self.store, embedding=self.embedding)
        self.assertEqual(restarted._priority["user"]["main.messages"]["below"],
                         self.index._priority["user"]["main.messages"]["below"])
        restarted.ensure_vectors(max_items=1)
        self.assertEqual(self.embedding.calls, ["new user question", "old user question"])
        self.message("g-newest-user", "newest user question", role="user")
        self.embed_one_at_a_time(restarted)
        self.assertEqual(self.embedding.calls[2], "newest user question")
        self.assertEqual(len(self.embedding.calls), 6)

    def test_soft_cap_stops_the_id_order_walk_until_vectors_are_blobs(self):
        self.corpus()
        blobs = {"blob_vectors": False}
        real = self.index.vector_storage  # the compaction migration's signal, with blob_vectors overridden
        self.index.vector_storage = lambda: dict(real(), **blobs)
        with mock.patch.object(SEMANTICS, "TRANSCRIPT_VECTOR_SOFT_CAP", 1):
            self.embed_one_at_a_time()
            # The priority walk is not capped; the id-order rest is.
            self.assertEqual(self.embedding.calls, ["new user question", "old user question",
                                                    "recent assistant answer"])
            self.assertTrue(self.index.coverage()["transcript_vector_cap"]["capped"])
            blobs["blob_vectors"] = True
            self.embed_one_at_a_time()
        self.assertEqual(len(self.embedding.calls), 5)
        self.assertFalse(self.index.coverage()["transcript_vector_cap"]["capped"])

    def test_coverage_counts_embeddable_and_embedded_records_and_rejections(self):
        self.corpus()
        self.message("h-digits", "1234 5678")  # no letters: the model has no vector for it
        self.assertFalse(self.index.step_coverage(budget_seconds=5))
        coverage = self.index.coverage()["transcript"]
        self.assertEqual((coverage["embeddable"], coverage["embedded"], coverage["ratio"]), (6, 0, 0.0))
        self.embed_one_at_a_time(steps=3)
        coverage = self.index.coverage()
        # Between recounts the embedded count grows from manifests as they are written.
        self.assertEqual(coverage["transcript"]["embedded"], 3)
        self.embed_one_at_a_time(steps=10)
        coverage = self.index.coverage()
        self.assertEqual(coverage["rejected_chunks"], 1)
        self.assertEqual(coverage["transcript"]["embedded"], 5)
        # The counters persist, and a recount is not due again for hours.
        self.index._checkpoint_scan()
        restarted = SEMANTICS.SemanticIndex(self.store, embedding=self.embedding)
        self.assertEqual(restarted.coverage()["rejected_chunks"], 1)
        self.assertFalse(restarted.step_coverage(budget_seconds=5))
        restarted._coverage["counted_at"] -= SEMANTICS.COVERAGE_RECOUNT_SECONDS
        restarted.step_coverage(budget_seconds=5)
        self.assertEqual(restarted.coverage()["transcript"]["embedded"], 5)
        self.assertEqual(restarted.coverage()["transcript"]["embeddable"], 6)

    def test_coverage_recount_is_bounded_and_resumes(self):
        for number in range(30):
            self.message(f"m{number:02d}", f"message {number}")
        with mock.patch.object(SEMANTICS, "COVERAGE_WINDOW", 4):
            self.assertTrue(self.index.step_coverage(budget_seconds=0))
            self.assertIsNone(self.index.coverage()["transcript"]["embeddable"])
            self.assertTrue(self.index.coverage()["transcript"]["counting"])
            while self.index.step_coverage(budget_seconds=0):
                pass
        self.assertEqual(self.index.coverage()["transcript"]["embeddable"], 30)


class EmbedBudgetTests(unittest.TestCase):
    def test_idle_on_power_gets_the_burst_budget(self):
        budget = KNOWLEDGE.embed_budget
        idle = {"idle_seconds": 300, "on_ac": True, "low_power": False, "thermal_state": 1}
        self.assertEqual(budget(idle), KNOWLEDGE.IDLE_EMBED_SECONDS)
        self.assertEqual(budget({**idle, "idle_seconds": 119}), KNOWLEDGE.EMBED_SECONDS)
        self.assertEqual(budget({**idle, "on_ac": False}), KNOWLEDGE.EMBED_SECONDS)
        self.assertEqual(budget({**idle, "low_power": True}), KNOWLEDGE.EMBED_SECONDS)
        self.assertEqual(budget({**idle, "thermal_state": 2}), KNOWLEDGE.EMBED_SECONDS)
        self.assertEqual(budget({}), KNOWLEDGE.EMBED_SECONDS)
        self.assertEqual(budget({"idle_seconds": True, "on_ac": True}), KNOWLEDGE.EMBED_SECONDS)
        self.assertEqual(budget({"idle_seconds": float("nan"), "on_ac": True}), KNOWLEDGE.EMBED_SECONDS)
        self.assertEqual((KNOWLEDGE.EMBED_SECONDS, KNOWLEDGE.IDLE_EMBED_SECONDS), (0.25, 2.0))

    def test_embed_op_uses_the_budget_and_reports_coverage(self):
        store = KNOWLEDGE.KnowledgeStore(roots=[], semantics=False)
        self.addCleanup(store.close)
        store._semantics = SEMANTICS.SemanticIndex(store, embedding=WordEmbedding())
        result = store.embed({"idle_seconds": 500, "on_ac": True})
        self.assertEqual(result["budget_seconds"], 2.0)
        self.assertIn("coverage", store.knowledge_status()["embedding"])
        self.assertIn("coverage", store.quick_status()["embedding"])

    def test_a_sliced_idle_burst_stays_short_and_reports_its_slices(self):
        store = KNOWLEDGE.KnowledgeStore(roots=[], semantics=False)
        self.addCleanup(store.close)
        store._semantics = SEMANTICS.SemanticIndex(store, embedding=WordEmbedding())
        store._put_source({"key": "source:s", "session_id": "claude:s", "provider": "claude", "path": "/s.jsonl",
                           "workspace": "/w", "started": 1, "updated": 1, "archived": 0})
        for index in range(400):
            store._put_message({"id": "s%d" % index, "source_key": "source:s", "session_id": "claude:s",
                                "provider": "claude", "workspace": "/w", "source_path": "/s.jsonl",
                                "source_line": index, "part": 0, "role": "assistant", "kind": "message",
                                "text": "Sliced burst sentence number %d about builds." % index,
                                "timestamp": "2026-11-01T00:00:00Z"})
        store.connection.commit()
        idle = {"idle_seconds": 500, "on_ac": True, "slice": True}
        first = store.embed(idle)
        self.assertEqual(first["budget_seconds"], KNOWLEDGE.EMBED_SECONDS)
        self.assertIn(first["slices"], (1, 8), "8 slices while work remains, 1 once it is done")
        busy = store.embed({"idle_seconds": 5, "on_ac": True, "slice": True})
        self.assertEqual((busy["budget_seconds"], busy["slices"]), (KNOWLEDGE.EMBED_SECONDS, 1))
        followup = store.embed({**idle, "followup": True})
        self.assertLessEqual(followup["budget_seconds"], KNOWLEDGE.EMBED_SECONDS)
        while store.embed({**idle, "followup": True})["embedded"]:
            pass
        self.assertEqual(store.embed(idle)["slices"], 1, "nothing left: no more slices")
        store._semantics.ensure_vectors = lambda max_items=None: {"indexed_chunks": 1, "attempted_chunks": 1}
        started = time.monotonic()
        busy_burst = store.embed(idle)
        self.assertEqual(busy_burst["slices"], round(KNOWLEDGE.IDLE_EMBED_SECONDS / KNOWLEDGE.EMBED_SECONDS))
        self.assertLess(time.monotonic() - started, KNOWLEDGE.EMBED_SECONDS + 0.5, "one slice, not the whole burst")


if __name__ == "__main__":
    unittest.main()
