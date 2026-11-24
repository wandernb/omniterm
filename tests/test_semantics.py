import importlib.util
import json
import math
import sqlite3
import tempfile
import unittest
from unittest import mock
from pathlib import Path


def load(name, filename):
    specification = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / "helpers" / filename)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


KNOWLEDGE = load("omni_knowledge_for_semantics_tests", "omni_knowledge.py")
SEMANTICS = load("omni_semantics_for_tests", "omni_semantics.py")


class FakeEmbedding:
    available = True

    def __init__(self, revision=1, dimensions=3):
        self.model_id = f"test_real_boundary:revision{revision}:d{dimensions}"
        self.dimensions = dimensions
        self.calls = []

    def status(self):
        return {"available": self.available, "model_id": self.model_id, "dimensions": self.dimensions}

    def embed(self, text):
        self.calls.append(text)
        if not self.available or not any(character.isalpha() for character in text):
            raise SEMANTICS.EMBEDDINGS.EmbeddingUnavailable("No local vector")
        words = text.lower()
        if any(word in words for word in ("database", "records", "link")):
            vector = [1.0, 0.0, 0.0]
        elif "bread" in words:
            vector = [0.0, 1.0, 0.0]
        else:
            vector = [0.5, 0.5, 0.7071067811865476]
        vector = vector[:self.dimensions]
        norm = math.sqrt(sum(value * value for value in vector))
        return [value / norm for value in vector]


class SemanticIndexTests(unittest.TestCase):
    def setUp(self):
        self.store = KNOWLEDGE.KnowledgeStore(roots=[], semantics=False)
        self.addCleanup(self.store.close)
        self.embedding = FakeEmbedding()
        self.index = SEMANTICS.SemanticIndex(self.store, embedding=self.embedding)

    def message(self, identifier, text, provider="claude", workspace="/workspace", kind="message"):
        source_key = "source:" + identifier
        sid = provider + ":" + identifier
        self.store._put_source({"key": source_key, "session_id": sid, "provider": provider,
                                "path": "/transcripts/" + identifier + ".jsonl", "workspace": workspace,
                                "started": 1, "updated": 1, "archived": 0})
        self.store._put_message({"id": identifier, "source_key": source_key, "session_id": sid,
                                 "provider": provider, "workspace": workspace,
                                 "source_path": "/transcripts/" + identifier + ".jsonl",
                                 "source_line": 42, "part": 0, "role": "tool" if kind == "output" else "assistant",
                                 "kind": kind, "text": text, "timestamp": "2026-11-06T10:00:00Z"})
        self.store.connection.commit()
        return sid

    def test_paraphrase_retrieval_keeps_the_original_message_source(self):
        self.message("answer", "A lost link prevented access to stored records")
        self.message("bread", "Fresh bread tastes delicious", provider="codex")
        self.index.ensure_vectors()
        result = self.index.search("database connection interrupted")
        match = result["matches"][0]
        self.assertEqual(match["message_id"], "answer")
        self.assertEqual(match["source_path"], "/transcripts/answer.jsonl")
        self.assertEqual(match["source_line"], 42)
        self.assertEqual((match["role"], match["kind"]), ("assistant", "message"))
        self.assertGreater(match["semantic_score"], result["matches"][1]["semantic_score"])
        self.assertFalse(result["partial_index"])

    def test_tool_output_is_found_lexically_but_not_vector_indexed(self):
        # Tool output is most of the corpus and rarely what anyone means; lexical search still finds it.
        self.message("answer", "A lost link prevented access to stored records", kind="output")
        self.index.ensure_vectors()
        self.assertEqual(self.index.search("database connection interrupted")["matches"], [])
        lexical = self.store.search_transcripts({"query": "stored records", "mode": "lexical"})["matches"]
        self.assertEqual([item["id"] for item in lexical], ["answer"])

    def test_harness_written_turns_are_not_vector_indexed(self):
        review = "The following is the Codex agent history added since your last approval assessment. database"
        self.message("review", review)
        self.message("reminder", "<system-reminder>\ndatabase records</system-reminder>")
        self.message("answer", "A lost link prevented access to stored records")
        self.index.ensure_vectors()
        indexed = {row[0] for row in self.store.connection.execute(
            "SELECT DISTINCT record_id FROM semantic_chunks WHERE corpus='transcript'")}
        self.assertEqual(indexed, {"answer"})
        lexical = self.store.search_transcripts({"query": "approval assessment", "mode": "lexical"})["matches"]
        self.assertEqual([item["id"] for item in lexical], ["review"], "still lexically searchable")

    def test_harness_turn_embedded_before_the_rule_is_skipped_and_dropped_from_the_matrix(self):
        self.message("review", "The following is the Codex agent history: database records")
        self.message("answer", "A lost link prevented access to stored records")
        original = SEMANTICS.MACHINE_PREFIXES
        SEMANTICS.MACHINE_PREFIXES = (SEMANTICS.TOOL_TEXT_PREFIX,)  # an index built before the rule
        try:
            self.index.ensure_vectors()
        finally:
            SEMANTICS.MACHINE_PREFIXES = original
        matrix = self.index._current_matrices()["transcript"]
        self.assertIn("review", matrix.record_slot)
        rows = len(matrix)
        matches = self.index.search("database")["matches"]
        self.assertEqual([item["id"] for item in matches], ["answer"])
        slot = matrix.record_slot["review"]
        self.assertNotIn(slot, list(matrix.row_record), "its rows leave the search without a compaction")
        self.assertEqual(len(matrix), rows)

    def test_time_window_scores_only_records_inside_it(self):
        self.message("old", "A lost link prevented access to stored records")
        self.message("new", "database connection dropped again")
        self.store.connection.execute("UPDATE messages SET timestamp='2026-06-01T10:00:00Z' WHERE id='old'")
        self.store.connection.commit()
        self.index.ensure_vectors()
        judged = []
        original = self.index._row

        def counting(corpus, identifier, *rest):
            judged.append(identifier)
            return original(corpus, identifier, *rest)
        self.index._row = counting
        before = SEMANTICS.VECTORS.VectorMatrix.epoch("2026-07-01")
        result = self.index.search("database", {"before": before})
        self.assertEqual([item["id"] for item in result["matches"]], ["old"])
        self.assertNotIn("new", judged, "a record outside the window is never read")
        matrix = self.index._current_matrices()["transcript"]
        self.assertEqual(matrix.slots_in_time(None, before), {matrix.record_slot["old"]})

    def test_records_restored_without_a_time_get_it_from_one_batched_lookup(self):
        matrix = SEMANTICS.VECTORS.VectorMatrix(3)
        matrix.upsert("a:0", "a", 0, "h", [1.0, 0.0, 0.0], {"role": "user"})
        matrix.upsert("b:0", "b", 0, "h", [1.0, 0.0, 0.0], {"role": "user", "timestamp": "1790000000"})
        asked = []

        def lookup(identifiers):
            asked.append(list(identifiers))
            return {"a": "2026-01-01T00:00:00Z"}
        cutoff = SEMANTICS.VECTORS.VectorMatrix.epoch("2026-02-01")
        self.assertEqual(matrix.slots_in_time(None, cutoff, lookup), {matrix.record_slot["a"]})
        self.assertEqual(matrix.slots_in_time(cutoff, None, lookup), {matrix.record_slot["b"]})
        self.assertEqual(asked, [["a"]], "looked up once, then kept")

    def test_memory_search_is_mean_centred_but_intake_similarity_is_raw_cosine(self):
        matrix = SEMANTICS.VECTORS.VectorMatrix(3)
        matrix.center_alpha = 1.0
        # Many records near one common direction (a hub), one off to the side.
        for index in range(20):
            matrix.upsert("hub%d" % index, "hub%d" % index, 0, "h", [1.0, 0.05 * (index % 3), 0.0])
        matrix.upsert("side", "side", 0, "h", [0.6, 0.0, 0.8])
        query = [0.8, 0.0, 0.6]
        raw = matrix.top(query, 1, centred=False)
        centred = matrix.top(query, 1)
        self.assertEqual(raw[0][1], "side")
        self.assertEqual(centred[0][1], "side")
        # Against a query on the hub direction, raw cosine prefers a hub; centring
        # removes the shared direction, so the distinctive record wins.
        hub_query = [0.9, 0.0, 0.44]
        self.assertTrue(matrix.top(hub_query, 1, centred=False)[0][1].startswith("hub"))
        self.assertEqual(matrix.top(hub_query, 1)[0][1], "side")
        self.assertEqual(SEMANTICS.CENTER_ALPHA["transcript"], 0.0)
        self.assertGreater(SEMANTICS.CENTER_ALPHA["memory"], 0)
        self.assertGreater(self.index._current_matrices()["memory"].center_alpha, 0)

    def test_mirrored_codex_record_is_one_semantic_match(self):
        self.message("jsonl", "database issue", provider="codex")
        self.store._put_source({"key": "source:mirror", "session_id": "codex:jsonl", "provider": "codex",
                                "path": "/state.sqlite#thread=jsonl", "workspace": "/workspace",
                                "started": 1, "updated": 1, "archived": 0})
        self.store._put_message({"id": "mirror", "source_key": "source:mirror", "session_id": "codex:jsonl",
                                 "provider": "codex", "workspace": "/workspace", "source_path": "/state.sqlite",
                                 "source_line": 42, "part": 0, "role": "assistant", "kind": "message",
                                 "text": "database issue", "timestamp": "2026-11-06T10:00:00Z"})
        self.message("other", "database issue", provider="claude")
        self.store.connection.commit()
        self.index.ensure_vectors()
        matches = self.index.search("database issue")["matches"]
        self.assertEqual(sorted(item["id"] for item in matches), ["jsonl", "other"])

    def test_semantic_filters_cannot_return_other_provider_or_workspace(self):
        self.message("first", "database issue", provider="claude", workspace="/first")
        sid = self.message("second", "database issue", provider="codex", workspace="/second")
        self.index.ensure_vectors()
        result = self.index.search("records", {"provider": "codex", "workspace": "/second", "session_id": sid})
        self.assertEqual([item["id"] for item in result["matches"]], ["second"])

    def test_hybrid_combines_real_semantics_and_normalized_lexical_rank(self):
        self.message("lexical", "database failure")
        self.message("paraphrase", "lost link to stored records")
        self.index.ensure_vectors()
        result = self.index.search("database", mode="hybrid")
        self.assertEqual(result["matches"][0]["id"], "lexical")
        self.assertGreater(result["matches"][0]["lexical_score"], 0)
        self.assertGreater(result["matches"][0]["score"], result["matches"][1]["score"])
        self.assertTrue(all(item["embedding_model"] == self.embedding.model_id for item in result["matches"]))

    def test_changed_record_excludes_stale_vectors_until_reindexed(self):
        self.message("same", "database connection")
        self.index.ensure_vectors()
        self.store.connection.execute("UPDATE messages SET text='fresh bread' WHERE id='same'")
        stale = self.index.search("database")
        self.assertEqual(stale["matches"], [], "a vector of the old text never matches the new text")
        self.assertTrue(stale["partial_index"])
        for _ in range(3):  # background indexing comes back round to the changed record
            self.index.ensure_vectors()
        result = self.index.search("database")
        self.assertEqual(result["matches"][0]["text"], "fresh bread")
        self.assertAlmostEqual(result["matches"][0]["semantic_score"], 0)

    def test_unchanged_documents_do_not_repeat_inference(self):
        self.message("same", "database connection")
        self.index.ensure_vectors()
        count = len(self.embedding.calls)
        self.index.ensure_vectors()
        self.assertEqual(len(self.embedding.calls), count)

    def test_native_snapshot_restore_preserves_vectors_without_reencoding(self):
        self.message("same", "database connection")
        self.index.ensure_vectors()
        snapshot = self.store.snapshot()
        restored_store = KNOWLEDGE.KnowledgeStore(roots=[], semantics=False)
        self.addCleanup(restored_store.close)
        restored_store.restore(snapshot, final=True)
        embedding = FakeEmbedding()
        index = SEMANTICS.SemanticIndex(restored_store, embedding=embedding)
        for item in snapshot:
            if item["key"].startswith("vector:"):
                index.restore_vector(item["key"], item["value"])
        index.ensure_vectors()
        self.assertEqual(embedding.calls, [])
        result = index.search("records")
        self.assertEqual(result["matches"][0]["id"], "same")
        self.assertEqual(embedding.calls, ["records"])

    def test_restart_does_not_skip_an_unfinished_multichunk_record(self):
        self.message("a-large", "database " * 5000)
        self.message("z-later", "fresh bread")
        self.index.ensure_vectors(max_items=1)
        snapshot = self.store.snapshot()
        restored = KNOWLEDGE.KnowledgeStore(roots=[], semantics=False)
        self.addCleanup(restored.close)
        restored.restore(snapshot, final=True)
        embedding = FakeEmbedding()
        index = SEMANTICS.SemanticIndex(restored, embedding=embedding)
        for item in snapshot:
            if item["key"].startswith("vector:"):
                index.restore_vector(item["key"], item["value"])
        result = index.ensure_vectors(max_items=1)
        self.assertEqual(result["attempted_chunks"], 1)
        self.assertEqual(len(embedding.calls), 1)
        self.assertIn("database", embedding.calls[0])
        self.assertGreater(result["pending_records"], 0)

    def test_changed_pending_record_is_retried_after_restart(self):
        self.message("a-large", "database " * 5000)
        self.index.ensure_vectors(max_items=1)
        self.message("a-large", "fresh bread")
        self.index.ensure_vectors(max_items=1)
        snapshot = self.store.snapshot()
        restored = KNOWLEDGE.KnowledgeStore(roots=[], semantics=False)
        self.addCleanup(restored.close)
        restored.restore(snapshot, final=True)
        embedding = FakeEmbedding()
        index = SEMANTICS.SemanticIndex(restored, embedding=embedding)
        for item in snapshot:
            if item["key"].startswith("vector:"):
                index.restore_vector(item["key"], item["value"])
        index.ensure_vectors(max_items=1)
        self.assertEqual(embedding.calls, ["fresh bread"])

    def test_model_revision_and_dimensions_trigger_reindex_without_mixing(self):
        self.message("same", "database connection")
        self.index.ensure_vectors()
        snapshot = self.store.snapshot()
        self.store._document(SEMANTICS.SCAN_CHECKPOINT, {
            "model": self.embedding.model_id, "cursors": {"transcript": "z-after-existing-record", "memory": ""}})
        next_model = FakeEmbedding(revision=2, dimensions=2)
        next_index = SEMANTICS.SemanticIndex(self.store, embedding=next_model)
        for item in snapshot:
            if item["key"].startswith("vector:"):
                next_index.restore_vector(item["key"], item["value"])
        next_index.ensure_vectors()
        result = next_index.search("records")
        self.assertEqual(result["matches"][0]["embedding_model"], next_model.model_id)
        self.assertEqual(result["embedding"]["dimensions"], 2)
        vector = self.store.connection.execute("SELECT model,dimensions FROM vectors").fetchone()
        self.assertEqual(tuple(vector), (next_model.model_id, 2))

    def test_scan_wrap_revisits_new_records_before_a_saved_frontier(self):
        self.message("a-new-record", "database connection")
        self.store._document(SEMANTICS.SCAN_CHECKPOINT, {
            "model": self.embedding.model_id, "cursors": {"transcript": "z-previous-frontier", "memory": ""}})
        index = SEMANTICS.SemanticIndex(self.store, embedding=self.embedding)
        index.ensure_vectors(max_items=1)
        restarted = SEMANTICS.SemanticIndex(self.store, embedding=self.embedding)
        restarted.ensure_vectors(max_items=1)
        self.assertEqual(self.embedding.calls, ["database connection"])

    def test_restart_at_scan_end_preserves_pending_work_and_new_earlier_records(self):
        self.message("m-existing", "fresh bread")
        self.index.ensure_vectors(max_items=1)
        self.message("z-large", "database " * 5000)
        self.index.ensure_vectors(max_items=1)
        self.message("a-new", "database retry")
        self.index.ensure_vectors(max_items=2)
        snapshot = self.store.snapshot()
        restored = KNOWLEDGE.KnowledgeStore(roots=[], semantics=False)
        self.addCleanup(restored.close)
        restored.restore(snapshot, final=True)
        embedding = FakeEmbedding()
        index = SEMANTICS.SemanticIndex(restored, embedding=embedding)
        for item in snapshot:
            if item["key"].startswith("vector:"):
                index.restore_vector(item["key"], item["value"])
        for _ in range(4):
            index.ensure_vectors(max_items=128)
        result = index.search("database", limit=3)
        self.assertEqual({match["id"] for match in result["matches"]}, {"a-new", "m-existing", "z-large"})
        self.assertFalse(result["partial_index"])

    def test_deleting_source_prunes_vectors_but_keeps_authored_memory(self):
        sid = self.message("evidence", "database connection")
        self.store.upsert_memory({"id": "durable", "title": "Database lesson", "body": "Check the lost link",
                                  "sources": [{"session_id": sid, "message_id": "evidence"}]})
        self.index.ensure_vectors()
        self.store._delete_source("source:evidence")
        self.index.ensure_vectors()
        self.assertEqual(self.index.search("records")["matches"], [])
        memories = self.index.search("records", corpus="memory")["memories"]
        self.assertEqual(memories[0]["id"], "durable")
        self.assertFalse(memories[0]["sources"][0]["available"])

    def test_memory_title_has_its_own_vector_that_search_uses_and_similarity_ignores(self):
        sid = self.message("evidence", "database connection")
        self.store.upsert_memory({"id": "starter", "title": "Bread starter",
                                  "body": "Stored records of every database link",
                                  "sources": [{"session_id": sid, "message_id": "evidence"}]})
        self.index.ensure_vectors()
        keys = {row[0] for row in self.store.connection.execute(
            "SELECT key FROM semantic_chunks WHERE corpus='memory' AND record_id='starter'")}
        title_key = SEMANTICS.vector_prefix("memory", "starter") + "title"
        self.assertIn(title_key, keys)
        self.assertEqual(self.store.connection.execute("SELECT chunk_index,chunk FROM semantic_chunks WHERE key=?",
                                                       (title_key,)).fetchone()[:], (SEMANTICS.TITLE_CHUNK, "Bread starter"))
        # The whole text (title + body) is database-like; only the title vector is about bread.
        match = self.index.search("bread", corpus="memory")["memories"][0]
        self.assertEqual(match["id"], "starter")
        self.assertAlmostEqual(match["semantic_score"], 1.0)
        self.assertEqual(match["chunk"], "")
        self.assertIn("database", match["snippet"].lower(), "the snippet shows the body, not the title")
        # The graph's per-memory vector averages the body only: the database direction.
        self.store._semantics = self.index
        vectors = self.store.hippocampus._load_memory_vectors({"starter"}, by_record=True)
        self.assertAlmostEqual(vectors["starter"][0], 1.0)

    def test_memory_embedded_before_title_vectors_is_requeued_once(self):
        sid = self.message("evidence", "database connection")
        self.store.upsert_memory({"id": "old", "title": "Bread starter", "body": "database records",
                                  "sources": [{"session_id": sid, "message_id": "evidence"}]})
        self.index.ensure_vectors()
        prefix = SEMANTICS.vector_prefix("memory", "old")
        manifest = self.store._document_value(prefix + "manifest")
        self.assertEqual(manifest["chunking"], SEMANTICS.MEMORY_CHUNKING)
        # A manifest from before title vectors: no chunking field, no title vector.
        self.index._delete_vector(prefix + "title")
        manifest.pop("chunking")
        manifest["vector_keys"] = [key for key in manifest["vector_keys"] if not key.endswith("title")]
        self.store._document(prefix + "manifest", manifest)
        self.store.connection.commit()
        self.index = SEMANTICS.SemanticIndex(self.store, embedding=self.embedding)
        self.embedding.calls.clear()
        for _ in range(2):  # the memory walk comes back round from its saved cursor
            self.index.ensure_vectors()
        self.assertEqual(self.embedding.calls, ["Bread starter"], "only the new title is embedded; body vectors are kept")
        manifest = self.store._document_value(prefix + "manifest")
        self.assertEqual(manifest["chunking"], SEMANTICS.MEMORY_CHUNKING)
        self.assertIn(prefix + "0", manifest["vector_keys"])
        self.assertIn(prefix + "title", manifest["vector_keys"])
        self.assertIsNotNone(self.store.connection.execute(
            "SELECT 1 FROM semantic_chunks WHERE key=?", (prefix + "title",)).fetchone())
        self.embedding.calls.clear()
        for _ in range(2):
            self.index.ensure_vectors()
        self.assertEqual(self.embedding.calls, [], "re-embedded once, then current")

    def test_memory_graph_vectors_read_the_helpers_own_float32_vectors(self):
        sid = self.message("evidence", "database connection")
        for identifier, title in (("one", "Database lesson"), ("two", "Stored records")):
            self.store.upsert_memory({"id": identifier, "title": title, "body": "Check the lost link",
                                      "sources": [{"session_id": sid, "message_id": "evidence"}]})
        self.index.ensure_vectors()
        stored = self.store.connection.execute("SELECT vector FROM vectors WHERE key GLOB 'vector:memory:*'").fetchone()
        self.assertIsInstance(stored["vector"], bytes, "the helper stores float32 blobs")
        self.store._semantics = self.index
        vectors = self.store.hippocampus._load_memory_vectors(None)
        self.assertEqual(set(vectors), {"one", "two"})
        self.assertAlmostEqual(sum(value * value for value in vectors["one"]), 1.0, places=5)

    def test_identical_chunks_are_embedded_once(self):
        self.message("first", "database connection lost")
        self.message("mirror", "database   connection lost")
        self.index.ensure_vectors()
        self.assertEqual(len([call for call in self.embedding.calls
                              if " ".join(call.split()) == "database connection lost"]), 1)
        self.assertEqual({item["id"] for item in self.index.search("database")["matches"]}, {"first", "mirror"})

    def test_memories_embed_before_transcripts_and_tool_rows_never_embed(self):
        sid = self.message("evidence", "database connection")
        self.message("output", "database dump", kind="output")
        self.store.upsert_memory({"id": "durable", "title": "Database lesson", "body": "Check the lost link",
                                  "sources": [{"session_id": sid, "message_id": "evidence"}]})
        self.index.ensure_vectors(max_items=1)
        self.assertEqual(self.embedding.calls, ["Database lesson\nCheck the lost link"])
        self.index.ensure_vectors()
        self.assertNotIn("database dump", self.embedding.calls)

    def test_tool_traffic_recorded_as_assistant_text_is_not_vector_indexed(self):
        self.message("pseudo", "[external_agent_tool_result] database connection refused")
        self.message("real", "The database connection was refused by the firewall")
        self.index.ensure_vectors()
        self.assertEqual([item["id"] for item in self.index.search("database")["matches"]], ["real"])
        self.assertNotIn("[external_agent_tool_result] database connection refused", self.embedding.calls)

    def test_pruning_sweeps_away_vectors_of_tool_rows(self):
        self.message("output", "A lost link prevented access to stored records", kind="output")
        self.store.connection.execute("UPDATE messages SET role='assistant', kind='message' WHERE id='output'")
        self.index.ensure_vectors()  # embedded under an older rule
        self.store.connection.execute("UPDATE messages SET role='tool', kind='output' WHERE id='output'")
        self.assertEqual(self.index.status()["vectors"], 1)
        while self.index.prune():
            pass
        self.index.prune()
        self.assertEqual(self.index.status()["vectors"], 0)
        self.assertEqual(self.index.search("database")["matches"], [])

    def test_a_paraphrase_finds_its_similar_memory_without_a_shared_word(self):
        sid = self.message("evidence", "database connection")
        self.store.upsert_memory({"id": "durable", "title": "Database lesson", "body": "Check stored records",
                                  "sources": [{"session_id": sid, "message_id": "evidence"}]})
        self.index.ensure_vectors()
        self.store._semantics = self.index
        similar = self.store._similar_memories("A lost link")
        self.assertEqual([(item["id"], item["match"]) for item in similar], [("durable", "semantic")])
        self.assertEqual(similar[0]["source_count"], 1)
        self.assertEqual(self.store._similar_memories("fresh bread"), [], "a distant memory is not similar")

    def test_memory_search_filters_follow_its_evidence(self):
        sid = self.message("evidence", "database connection", provider="codex", workspace="/other")
        self.store.upsert_memory({"id": "durable", "title": "Database lesson", "body": "Check stored records",
                                  "sources": [{"session_id": sid, "message_id": "evidence"}]})
        self.index.ensure_vectors()
        self.assertEqual(self.index.search("records", {"provider": "claude"}, corpus="memory")["memories"], [])
        result = self.index.search("records", {"provider": "codex"}, corpus="memory")
        self.assertEqual(result["memories"][0]["id"], "durable")

    def test_background_inference_is_bounded_and_reports_partial_coverage(self):
        self.message("large", "database " * 5000)
        result = self.index.ensure_vectors(max_items=2)
        self.assertLessEqual(result["attempted_chunks"], 2)
        self.assertGreater(result["pending_records"], 0)
        result = self.index.search("records")
        self.assertTrue(result["partial_index"])

    def test_oov_chunks_do_not_create_fake_vectors(self):
        self.message("numbers", "123456789")
        self.index.ensure_vectors()
        self.assertEqual(self.index.status()["vectors"], 0)
        self.assertTrue(self.index.status()["errors"])

    def test_unavailable_model_is_an_explicit_error(self):
        self.embedding.available = False
        self.message("same", "database")
        self.assertEqual(self.index.ensure_vectors()["indexed_chunks"], 0)
        with self.assertRaises(SEMANTICS.EMBEDDINGS.EmbeddingUnavailable):
            self.index.search("records")


class TableEmbedding:
    """Vectors chosen per text, for ranking tests."""
    available = True
    model_id = "table:v1:d3"
    dimensions = 3

    def __init__(self, table):
        self.table = table

    def status(self):
        return {"available": True, "model_id": self.model_id, "dimensions": 3}

    def embed(self, text):
        for word, vector in self.table.items():
            if word in text:
                return vector
        return [0.0, 0.0, 1.0]


class RankingTests(unittest.TestCase):
    def setUp(self):
        self.store = KNOWLEDGE.KnowledgeStore(roots=[], semantics=False)
        self.addCleanup(self.store.close)
        self.index = SEMANTICS.SemanticIndex(self.store, embedding=TableEmbedding({
            "first": [1.0, 0.0, 0.0], "twin": [0.99, 0.141, 0.0], "other": [0.8, 0.0, 0.6],
            "durable": [0.8, 0.6, 0.0], "fleeting": [0.6, 0.8, 0.0], "alpha": [1.0, 0.0, 0.0]}))
        self.helper = SemanticIndexTests("run")
        self.helper.store = self.store
        # These tests pin the fusion mechanics with equal weights; the shipped
        # weights were chosen by scripts/memory-eval.py.
        weights = dict(SEMANTICS.RRF_WEIGHTS)
        self.addCleanup(SEMANTICS.RRF_WEIGHTS.update, weights)
        SEMANTICS.RRF_WEIGHTS.update(semantic_rank=1.0, lexical_rank=1.0, weight_rank=1.0)

    def test_shipped_weights_favour_the_lexical_list(self):
        self.addCleanup(SEMANTICS.RRF_WEIGHTS.update, dict(SEMANTICS.RRF_WEIGHTS))
        SEMANTICS.RRF_WEIGHTS.update(semantic_rank=0.5, lexical_rank=1.0)
        self.helper.message("dense", "other words entirely")
        self.helper.message("lexical", "alpha lexical only")
        self.index.ensure_vectors()
        hybrid = self.index.search("alpha", mode="hybrid")["matches"]
        self.assertEqual(hybrid[0]["id"], "lexical")

    def test_hybrid_fuses_ranks_and_lets_a_near_duplicate_give_way(self):
        for identifier in ("first", "twin", "other"):
            self.helper.message(identifier, "alpha " + identifier)
        self.index.ensure_vectors()
        semantic = [item["id"] for item in self.index.search("alpha")["matches"]]
        self.assertEqual(semantic, ["first", "twin", "other"])
        hybrid = self.index.search("alpha", mode="hybrid")["matches"]
        self.assertEqual([item["id"] for item in hybrid], ["first", "other", "twin"],
                         "the twin of the best match drops below a distinct result")
        self.assertTrue(all(item["lexical_score"] > 0 for item in hybrid))
        self.assertAlmostEqual(hybrid[0]["score"], 2 / (SEMANTICS.RRF_K + 1))

    def test_a_result_in_only_one_list_ranks_below_one_in_both(self):
        self.helper.message("both", "alpha first")
        self.helper.message("dense", "other words entirely")
        self.index.ensure_vectors()
        hybrid = self.index.search("alpha", mode="hybrid")["matches"]
        self.assertEqual([item["id"] for item in hybrid], ["both", "dense"])

    def test_weight_prior_is_opt_in_for_memory_search(self):
        sid = self.helper.message("evidence", "alpha evidence")
        source = [{"session_id": sid, "message_id": "evidence"}]
        self.store.upsert_memory({"id": "a-fleeting", "title": "alpha fleeting", "body": "x", "sources": source,
                                  "memory_type": "episodic", "weight": 1})
        self.store.upsert_memory({"id": "b-durable", "title": "alpha durable", "body": "x", "sources": source,
                                  "memory_type": "autobiographical", "weight": 9})
        self.index.ensure_vectors()
        # Semantics favour durable, lexical rank favours fleeting (equal BM25, id order): a tie.
        plain = self.index.search("alpha", mode="hybrid", corpus="memory")["memories"]
        self.assertEqual([item["id"] for item in plain], ["a-fleeting", "b-durable"])
        self.assertEqual(plain[0]["score"], plain[1]["score"])
        weighted = self.index.search("alpha", {"weight_prior": True}, mode="hybrid", corpus="memory")["memories"]
        self.assertEqual([item["id"] for item in weighted], ["b-durable", "a-fleeting"])


class ReadthroughSemanticTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "knowledge.sqlite"
        self.writer = sqlite3.connect(self.path)
        self.addCleanup(self.writer.close)
        self.writer.execute("CREATE TABLE records(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        self.writer.executescript((Path(__file__).parents[1] / "helpers/omni_knowledge_schema.sql").read_text())
        self.embedding = FakeEmbedding()
        self.count = 1200
        self.messages = {}
        self.vectors = {}
        with self.writer:
            for number in range(self.count):
                identifier = f"record-{number:05d}"
                text = "database connection" if number == self.count - 1 else "fresh bread"
                message = {"id": identifier, "source_key": "source:fixture", "session_id": "codex:fixture",
                           "provider": "codex", "workspace": "/fixture", "source_path": "/fixture/transcript.jsonl",
                           "source_line": number + 1, "part": 0, "role": "assistant", "kind": "message", "text": text,
                           "timestamp": "2026-11-06T10:00:00Z"}
                prefix = SEMANTICS.vector_prefix("transcript", identifier)
                vector = {"key": prefix + "0", "kind": "chunk", "corpus": "transcript", "record_id": identifier,
                          "chunk_index": 0, "chunk": text, "content_hash": SEMANTICS.content_hash(text),
                          "model": self.embedding.model_id, "dimensions": 3, "vector": self.embedding.embed(text)}
                manifest = {**vector, "key": prefix + "manifest", "kind": "manifest", "vector_keys": [vector["key"]]}
                manifest.pop("vector")
                self.messages[identifier], self.vectors[identifier] = message, vector
                self.writer.executemany("INSERT INTO records VALUES(?,?)", [
                    ("message:" + identifier, json.dumps(message)),
                    (vector["key"], json.dumps(vector)), (manifest["key"], json.dumps(manifest))])
        self.store = KNOWLEDGE.KnowledgeStore(roots=[], semantics=False, read_index=self.path)
        self.addCleanup(self.store.close)
        self.index = SEMANTICS.SemanticIndex(self.store, embedding=self.embedding)
        self.index.load_matrices(None)  # the app loads in background slices

    def persist(self):
        while True:
            batch = self.store.request({"op": "drain_writes"})
            with self.writer:
                for record in batch["_writes"]:
                    if record["value"] is None:
                        self.writer.execute("DELETE FROM records WHERE key=?", (record["key"],))
                    else:
                        self.writer.execute("INSERT OR REPLACE INTO records VALUES(?,?)",
                                            (record["key"], json.dumps(record["value"])))
            self.store.request({"op": "ack_writes", "revision": batch["_revision"]})
            if not batch["_more_writes"]:
                break

    def persist_and_restart(self):
        self.persist()
        self.store.close()
        self.store = KNOWLEDGE.KnowledgeStore(roots=[], semantics=False, read_index=self.path)
        self.addCleanup(self.store.close)
        self.index = SEMANTICS.SemanticIndex(self.store, embedding=self.embedding)
        self.index.load_matrices(None)  # the app loads in background slices

    def test_scoped_memories_are_judged_up_front_on_the_readthrough_view(self):
        # `memories` is a view here (no rowid); the scope cache must still work, or every
        # scoped semantic search judges candidates one by one (seconds live).
        memory = {"id": "scoped", "title": "Database lesson", "body": "Retry the lost link",
                  "sources": [{"session_id": "codex:fixture", "message_id": "record-00001"}],
                  "tags": [], "revision": 1, "created": 1, "updated": 1}
        with self.writer:
            self.writer.execute("INSERT INTO records VALUES(?,?)", ("memory:scoped", json.dumps(memory)))
        self.assertEqual(self.store._scoped_memory_ids("memories", ["m.workspace=?"], ["/fixture"]), ["scoped"])
        allowed = self.index._allowed_memories({"query": "database", "workspace": "/fixture", "negations": ["x"]},
                                               self.store._memory_matches)
        self.assertEqual(allowed, {"scoped"})

    def test_a_saved_snapshot_warms_a_restart_and_loads_only_newer_rows(self):
        self.assertTrue(self.index.maybe_save_snapshot(now=0.0))
        self.assertFalse(self.index.maybe_save_snapshot(now=10.0), "unchanged and recent: not saved again")
        identifier = "z-after-snapshot"
        message = {**self.messages["record-00000"], "id": identifier, "text": "database retry"}
        vector = {**self.vectors["record-00000"], "key": SEMANTICS.vector_prefix("transcript", identifier) + "0",
                  "record_id": identifier, "chunk": "database retry",
                  "content_hash": SEMANTICS.content_hash("database retry"), "vector": self.embedding.embed("database retry")}
        with self.writer:
            self.writer.execute("INSERT INTO records VALUES(?,?)", ("message:" + identifier, json.dumps(message)))
            self.writer.execute("INSERT INTO records VALUES(?,?)", (vector["key"], json.dumps(vector)))
            # A record deleted after the snapshot was saved:
            self.writer.execute("DELETE FROM records WHERE key=?", ("message:record-01199",))
        restarted = SEMANTICS.SemanticIndex(self.store, embedding=self.embedding)
        decoded = []
        original = restarted.decode_vector
        restarted.decode_vector = lambda raw, dimensions: decoded.append(1) or original(raw, dimensions)
        self.assertFalse(restarted.load_matrices(None))
        self.assertEqual(len(decoded), 1, "only the row added after the snapshot is read from the index")
        self.assertEqual(len(restarted._matrices["transcript"]), self.count + 1)
        matches = [item["id"] for item in restarted.search("database", limit=3)["matches"]]
        self.assertEqual(matches[0], identifier)
        self.assertNotIn("record-01199", matches, "a snapshot row whose record is gone never surfaces")

    def test_a_snapshot_of_another_index_is_ignored(self):
        self.assertTrue(self.index.maybe_save_snapshot(now=0.0))
        json_path = self.index._snapshot_paths("transcript")[0]
        metadata = json.loads(json_path.read_text())
        metadata["identity"]["inode"] = -1
        json_path.write_text(json.dumps(metadata))
        restarted = SEMANTICS.SemanticIndex(self.store, embedding=self.embedding)
        restarted.load_matrices(None)
        self.assertEqual(len(restarted._matrices["transcript"]), self.count, "loaded from the index instead")

    def test_restarts_advance_past_indexed_history_to_a_new_tail_record(self):
        identifier = "z-new-record"
        message = {**self.messages["record-00000"], "id": identifier, "text": "database retry"}
        with self.writer:
            self.writer.execute("INSERT INTO records VALUES(?,?)", ("message:" + identifier, json.dumps(message)))
        self.embedding.calls.clear()
        for _ in range(6):
            result = self.index.ensure_vectors(max_items=1)
            self.assertLessEqual(result["attempted_chunks"], 1)
            self.persist_and_restart()
        self.assertEqual(self.embedding.calls, ["database retry"])
        self.index.load_matrices(None)
        result = self.index.search("database", limit=2)
        self.assertIn(identifier, [match["id"] for match in result["matches"]])

    def test_multichunk_backlog_checkpoints_completed_batches_before_corpus_finishes(self):
        # The id-order frontier on its own: the priority walk is covered in test_semantic_coverage.
        priority = mock.patch.object(SEMANTICS, "PRIORITY_PHASES", ())
        priority.start()
        self.addCleanup(priority.stop)
        with self.writer:
            for number in range(12):
                identifier = f"z-record-{number:02d}"
                text = (f"record{number:02d} database " * 500).strip()
                message = {**self.messages["record-00000"], "id": identifier, "text": text}
                self.writer.execute("INSERT INTO records VALUES(?,?)", ("message:" + identifier, json.dumps(message)))
            checkpoint = {"model": self.embedding.model_id,
                          "cursors": {"transcript": f"record-{self.count-1:05d}", "memory": ""}}
            self.writer.execute("INSERT INTO records VALUES(?,?)", (SEMANTICS.SCAN_CHECKPOINT, json.dumps(checkpoint)))
        self.index = SEMANTICS.SemanticIndex(self.store, embedding=self.embedding)
        # Two records form the first batch, and each turn may encode two chunks.
        for _ in range(len(SEMANTICS.EMBEDDINGS.chunk_text(text))):
            self.index.ensure_vectors(max_items=2)
            self.persist()
        checkpoint = json.loads(self.writer.execute("SELECT value FROM records WHERE key=?",
                                                   (SEMANTICS.SCAN_CHECKPOINT,)).fetchone()[0])
        self.assertEqual(checkpoint["cursors"]["transcript"], "z-record-01")
        completed = self.writer.execute("SELECT COUNT(*) FROM records WHERE key GLOB 'vector:transcript:*:manifest'").fetchone()[0]
        self.assertEqual(completed, self.count + 2)
        self.persist_and_restart()
        self.embedding.calls.clear()
        result = self.index.ensure_vectors(max_items=2)
        self.assertEqual(result["attempted_chunks"], 2)
        self.assertTrue(all("record02" in text for text in self.embedding.calls))

    def test_large_indexed_history_does_not_delay_new_memory_inference(self):
        memory = {"id": "new-memory", "title": "Database lesson", "body": "Retry the lost link",
                  "sources": [], "tags": [], "revision": 1, "created": 1, "updated": 1}
        with self.writer:
            self.writer.execute("INSERT INTO records VALUES(?,?)", ("memory:new-memory", json.dumps(memory)))
        self.embedding.calls.clear()
        result = self.index.ensure_vectors(max_items=1)
        self.assertEqual(result["attempted_chunks"], 1)
        self.assertEqual(self.embedding.calls, ["Database lesson\nRetry the lost link"])

    def test_full_persisted_corpus_search_does_not_require_temporary_whole_corpus_indexes(self):
        # Removing automatic indexes exposes repeated corpus scans while still
        # allowing the native schema's permanent indexes and exact retrieval.
        self.store.connection.execute("PRAGMA automatic_index=OFF")
        callbacks = 0

        def budget():
            nonlocal callbacks
            callbacks += 1
            return callbacks > self.count // 2

        self.store.connection.set_progress_handler(budget, 1000)
        try:
            result = self.index.search("records", {"provider": "codex", "workspace": "/fixture"}, limit=1)
        finally:
            self.store.connection.set_progress_handler(None, 0)
        self.assertEqual(result["matches"][0]["id"], f"record-{self.count-1:05d}")
        self.assertEqual(result["indexed_records"], self.count)
        self.assertFalse(result["partial_index"])

    def test_overlay_replacement_and_tombstone_hide_persisted_vectors_and_preserve_filters(self):
        first, last = "record-00000", f"record-{self.count-1:05d}"
        self.store._shadow_document("message:" + last)
        self.index._delete_vector(self.vectors[last]["key"])
        changed = {**self.messages[first], "text": "database connection", "provider": "claude",
                   "session_id": "claude:replacement", "workspace": "/replacement"}
        self.store._put_message(changed)
        vector = {**self.vectors[first], "chunk": changed["text"],
                  "content_hash": SEMANTICS.content_hash(changed["text"]), "vector": [1.0, 0.0, 0.0]}
        self.index.restore_vector(vector["key"], vector)
        self.store._document(vector["key"], vector)
        self.store.connection.commit()
        result = self.index.search("records", limit=2)
        self.assertEqual(result["matches"][0]["id"], first)
        self.assertEqual(result["matches"][0]["provider"], "claude")
        self.assertNotIn(last, [item["id"] for item in result["matches"]])
        rows = list(self.index._vector_rows("transcript", self.embedding.model_id, 3))
        self.assertEqual(len(rows), self.count - 1)
        self.assertEqual(len({row["key"] for row in rows}), self.count - 1)
        filtered = self.index.search("records", {"provider": "codex", "workspace": "/fixture"}, limit=2)
        self.assertTrue(all(item["id"] not in (first, last) for item in filtered["matches"]))
        self.assertEqual(self.index.search("records", {"session_id": "claude:replacement"})["matches"][0]["id"], first)


if __name__ == "__main__":
    unittest.main()
