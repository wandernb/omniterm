"""Search with the query grammar: filters, phrases, negation, time, snippets and more like this."""

import importlib.util
import math
import unittest
from pathlib import Path


def load(name, filename):
    specification = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / "helpers" / filename)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


KNOWLEDGE = load("omni_knowledge_for_grammar_tests", "omni_knowledge.py")
SEMANTICS = load("omni_semantics_for_grammar_tests", "omni_semantics.py")
QUERY = load("omni_query_for_grammar_tests", "omni_query.py")

DAY = 86400.0


class FakeEmbedding:
    available = True
    model_id = "test_grammar:d3"
    dimensions = 3

    def status(self):
        return {"available": True, "model_id": self.model_id, "dimensions": self.dimensions}

    def embed(self, text):
        words = text.lower()
        if "vacuum" in words or "sqlite" in words:
            vector = [1.0, 0.1, 0.0]
        elif "bread" in words:
            vector = [0.0, 1.0, 0.0]
        else:
            vector = [0.3, 0.3, 0.9]
        norm = math.sqrt(sum(value * value for value in vector))
        return [value / norm for value in vector]


class GrammarSearchTests(unittest.TestCase):
    def setUp(self):
        self.store = KNOWLEDGE.KnowledgeStore(roots=[], semantics=False)
        self.addCleanup(self.store.close)
        self.line = 0

    def message(self, identifier, text, provider="claude", workspace="/Users/me/OmniTerm", role="assistant",
                kind="message", timestamp="2026-11-06T10:00:00Z", updated=None):
        source_key = "source:" + identifier
        sid = provider + ":" + identifier
        stamp = QUERY.timestamp_epoch(timestamp) or 0
        self.store._put_source({"key": source_key, "session_id": sid, "provider": provider,
                                "path": "/transcripts/" + identifier + ".jsonl", "workspace": workspace,
                                "started": stamp, "updated": updated if updated is not None else stamp, "archived": 0})
        self.line += 1
        self.store._put_message({"id": identifier, "source_key": source_key, "session_id": sid,
                                 "provider": provider, "workspace": workspace,
                                 "source_path": "/transcripts/" + identifier + ".jsonl",
                                 "source_line": self.line, "part": 0, "role": role, "kind": kind, "text": text,
                                 "timestamp": timestamp})
        self.store.connection.commit()
        return sid

    def memory(self, title, body, source, memory_type="declarative"):
        session_id, message_id = source
        return self.store.upsert_memory({"title": title, "body": body, "memory_type": memory_type,
                                         "sources": [{"session_id": session_id, "message_id": message_id}]})["memory"]

    def ids(self, result, key="matches"):
        return [item["id"] for item in result[key]]

    # Transcripts, lexical

    def test_phrase_and_negation_reach_fts(self):
        self.message("a", "we ran sqlite vacuum on the cache")
        self.message("b", "vacuum then sqlite analyze", provider="codex")
        self.message("c", "sqlite vacuum from codex itself")
        result = self.store.search_transcripts({"query": '"sqlite vacuum" -codex'})
        self.assertEqual(self.ids(result), ["a"])
        self.assertIn("«sqlite vacuum»", result["matches"][0]["snippet"])

    def test_filters_from_the_grammar_and_explicit_arguments_win(self):
        self.message("a", "deploy notes", provider="claude", role="user")
        self.message("b", "deploy notes", provider="codex", role="assistant")
        self.assertEqual(self.ids(self.store.search_transcripts({"query": "deploy provider:codex"})), ["b"])
        self.assertEqual(self.ids(self.store.search_transcripts({"query": "deploy provider:codex",
                                                                  "provider": "claude"})), ["a"])
        self.assertEqual(self.ids(self.store.search_transcripts({"query": "deploy role:user"})), ["a"])
        self.assertEqual(self.ids(self.store.search_transcripts({"query": "deploy", "role": "assistant"})), ["b"])

    def test_workspace_name_matches_the_folder_in_any_case(self):
        self.message("a", "release build", workspace="/Users/me/OmniTerm")
        self.message("b", "release build", workspace="/Users/me/Other")
        self.assertEqual(self.ids(self.store.search_transcripts({"query": "release ws:omniterm"})), ["a"])
        self.assertEqual(self.ids(self.store.search_transcripts({"query": "release ws:/Users/me/Other"})), ["b"])

    def test_type_is_the_record_kind_on_transcripts(self):
        self.message("a", "error trace", kind="output", role="tool")
        self.message("b", "error trace", kind="message")
        self.assertEqual(self.ids(self.store.search_transcripts({"query": "error type:output"})), ["a"])

    def test_time_bounds_read_both_timestamp_shapes(self):
        self.message("iso_old", "backup ran", timestamp="2026-10-01T09:00:00Z")
        self.message("iso_new", "backup ran", timestamp="2026-11-05T09:00:00.120Z")
        self.message("epoch_new", "backup ran", timestamp=str(QUERY.parse_time("2026-11-07T09:00:00")))
        self.message("no_time", "backup ran", timestamp=None)
        after = self.store.search_transcripts({"query": "backup after:2026-11-01"})
        self.assertEqual(sorted(self.ids(after)), ["epoch_new", "iso_new"])
        before = self.store.search_transcripts({"query": "backup", "before": "2026-11-06"})
        self.assertEqual(sorted(self.ids(before)), ["iso_new", "iso_old"])
        with self.assertRaises(KNOWLEDGE.KnowledgeError):
            self.store.search_transcripts({"query": "backup", "after": "last week"})

    def test_filter_only_query_picks_sessions_by_update_time(self):
        self.message("old", "first", timestamp="2026-09-01T00:00:00Z")
        self.message("new", "second", timestamp="2026-11-09T00:00:00Z")
        result = self.store.search_transcripts({"query": "after:2026-11-01"})
        self.assertEqual(self.ids(result), ["new"])
        self.assertEqual(result["matches"][0]["snippet"], "second")
        with self.assertRaises(KNOWLEDGE.KnowledgeError):
            self.store.search_transcripts({"query": "-only"})

    def test_results_stay_within_the_response_cap(self):
        for index in range(30):
            self.message("big%d" % index, "needle " + "x" * 7000 + " %d" % index)
        result = self.store.search_transcripts({"query": "needle", "limit": 50})
        size = sum(len(KNOWLEDGE.json.dumps(item).encode()) for item in result["matches"])
        self.assertLessEqual(size, 384 * 1024)
        self.assertGreater(len(result["matches"]), 0)

    # Memories

    def test_memory_filters_type_time_concept_and_snippet(self):
        self.message("m1", "evidence one")
        self.message("m2", "evidence two")
        decided = self.memory("Use WAL for sqlite", "We decided to keep sqlite in WAL mode in helpers/omni_kb.py.", ("claude:m1", "m1"))
        self.memory("Bake sqlite bread", "An unrelated sqlite recipe.", ("claude:m2", "m2"), memory_type="procedural")
        result = self.store.search_memories({"query": "sqlite type:decision"})
        self.assertEqual(self.ids(result, "memories"), [decided["id"]])
        self.assertIn("«sqlite»", result["memories"][0]["snippet"])
        self.assertEqual(len(self.store.search_memories({"query": "type:procedural"})["memories"]), 1)
        self.assertEqual(self.store.search_memories({"query": "sqlite", "before": "2000-01-01"})["memories"], [])
        concept = self.store.search_memories({"query": "concept:omni_kb"})
        self.assertEqual(self.ids(concept, "memories"), [decided["id"]])
        negated = self.store.search_memories({"query": "sqlite -bread"})
        self.assertEqual(self.ids(negated, "memories"), [decided["id"]])

    def test_archived_memories_hide_by_default_when_the_field_exists(self):
        self.message("m1", "evidence")
        memory = self.memory("Archived note", "old sqlite fact", ("claude:m1", "m1"))
        original = self.store._memory
        self.store._memory = lambda row: {**original(row), "archived_at": 123.0}
        self.assertEqual(self.store.search_memories({"query": "sqlite"})["memories"], [])
        self.assertEqual(self.ids(self.store.search_memories({"query": "sqlite archived:true"}), "memories"),
                         [memory["id"]])

    # Semantic, hybrid and more like this

    def semantic(self):
        index = SEMANTICS.SemanticIndex(self.store, embedding=FakeEmbedding())
        self.store._semantics = index
        return index

    def test_semantic_mode_applies_filters_negation_and_marks_chunks(self):
        index = self.semantic()
        self.message("a", "sqlite vacuum reclaimed space", provider="claude")
        self.message("b", "sqlite vacuum on codex host", provider="codex")
        self.message("c", "vacuum sqlite pages again", provider="claude", timestamp="2026-01-01T00:00:00Z")
        index.ensure_vectors(max_items=50)
        result = self.store.search_transcripts({"query": "vacuum provider:claude after:2026-06-01", "mode": "semantic"})
        self.assertEqual(self.ids(result), ["a"])
        self.assertIn("«vacuum»", result["matches"][0]["snippet"])
        hybrid = self.store.search_transcripts({"query": "vacuum -codex", "mode": "hybrid"})
        self.assertNotIn("b", self.ids(hybrid))

    def test_more_like_this_uses_the_stored_vector_and_excludes_itself(self):
        index = self.semantic()
        self.message("a", "sqlite vacuum reclaimed space")
        self.message("b", "sqlite vacuum again later")
        self.message("c", "fresh bread recipe")
        index.ensure_vectors(max_items=50)
        result = self.store.search_transcripts({"like": "message:a", "limit": 2})
        self.assertEqual(result["like"], "message:a")
        self.assertEqual(self.ids(result), ["b", "c"])
        memory = self.memory("Vacuum sqlite weekly", "sqlite vacuum reclaims pages", ("claude:a", "a"))
        other = self.memory("Bread", "fresh bread recipe", ("claude:c", "c"))
        index.ensure_vectors(max_items=50)
        similar = self.store.search_memories({"like": "message:a"})
        self.assertEqual(self.ids(similar, "memories")[0], memory["id"])
        self.assertEqual(self.ids(self.store.search_memories({"like": "memory:" + memory["id"]}), "memories"),
                         [other["id"]])

    def test_more_like_this_falls_back_to_the_title_without_vectors(self):
        self.message("a", "sqlite vacuum reclaimed space")
        self.message("b", "the vacuum finished")
        first = self.memory("Vacuum sqlite weekly", "body one", ("claude:a", "a"))
        second = self.memory("Weekly sqlite checks", "body two", ("claude:b", "b"))
        result = self.store.search_memories({"like": "memory:" + first["id"]})
        self.assertEqual(result["mode"], "lexical")
        self.assertEqual(self.ids(result, "memories"), [second["id"]])
        self.assertEqual(self.ids(self.store.search_transcripts({"like": "message:a"})), ["b"])
        with self.assertRaises(KNOWLEDGE.KnowledgeError):
            self.store.search_memories({"like": "memory:missing"})


if __name__ == "__main__":
    unittest.main()
