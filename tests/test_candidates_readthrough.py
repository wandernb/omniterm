import importlib.util
import json
import sqlite3
import tempfile
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("omni_knowledge_candidate_tests", ROOT / "helpers/omni_knowledge.py")
KNOWLEDGE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(KNOWLEDGE)


def message(identifier, text, role="assistant"):
    return {"id": identifier, "source_key": "source:fixture", "session_id": "claude:fixture",
            "provider": "claude", "workspace": "/w", "source_path": "/fixture.jsonl", "source_line": 1,
            "part": 0, "role": role, "kind": "message", "text": text, "timestamp": "2026-11-01T00:00:00Z"}


class ReadthroughCandidateTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "knowledge.sqlite"
        writer = sqlite3.connect(path)
        self.addCleanup(writer.close)
        writer.execute("CREATE TABLE records(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        writer.executescript((ROOT / "helpers/omni_knowledge_schema.sql").read_text())
        with writer:
            writer.execute("INSERT INTO records VALUES(?,?)", ("source:fixture", json.dumps(
                {"key": "source:fixture", "session_id": "claude:fixture", "provider": "claude", "path": "/fixture.jsonl"})))
            for identifier in ("old", "considered", "user", "replaced", "deleted", "newest"):
                role = "user" if identifier == "user" else "assistant"
                writer.execute("INSERT INTO records VALUES(?,?)",
                               ("message:" + identifier, json.dumps(message(identifier, "Persisted " + identifier, role))))
            writer.execute("INSERT INTO records VALUES(?,?)", ("metadata:memory_intake:considered", json.dumps(
                {"message_id": "considered", "reason": "done"})))
        self.store = KNOWLEDGE.KnowledgeStore(roots=[], home=directory.name, read_index=path, semantics=False)
        self.addCleanup(self.store.close)
        self.store._put_message(message("replaced", "Overlay replacement"))
        self.store._put_message(message("added", "Overlay addition"))
        self.store._document("message:deleted", None)
        self.store.connection.commit()

    def test_candidates_are_newest_first_once_each_and_skip_considered_and_shadowed(self):
        value = self.store.request({"op": "memory_candidates"})
        self.assertTrue(value["ok"], value)
        self.assertEqual([(item["message_id"], item["text"]) for item in value["candidates"]],
                         [("added", "Overlay addition"), ("replaced", "Overlay replacement"),
                          ("newest", "Persisted newest"), ("old", "Persisted old")])
        limited = self.store.request({"op": "memory_candidates", "limit": 3})
        self.assertEqual([item["message_id"] for item in limited["candidates"]], ["added", "replaced", "newest"])
        self.assertEqual(self.store.hippocampus.pending_candidates(cap=2), 3, "the count stops after cap + 1")

    def test_user_turns_with_a_cue_are_offered_redacted_and_markup_free(self):
        secret = "sk-" + "Ab3dE5gH7jK9mN1pQ3sT5vW7yZ9bC1dF3hJ5"
        self.store._put_message(message("preference", "<system-reminder>Injected context</system-reminder>\n"
                                                      "Don't hardcode " + secret + "; always read the key from the "
                                                      "keychain instead.", role="user"))
        self.store._put_message(message("chatter", "Thanks, looks good to me", role="user"))
        self.store._put_message(message("pasted", "Never mind:\n" + "\n".join(
            "2026-11-01T00:00:%02dZ ERROR worker: failed" % second for second in range(6)), role="user"))
        self.store.connection.commit()
        candidates = self.store.request({"op": "memory_candidates"})["candidates"]
        identifiers = [item["message_id"] for item in candidates]
        self.assertEqual(identifiers[0], "preference", "newest first, like assistant candidates")
        self.assertNotIn("chatter", identifiers, "no correction, decision or preference cue")
        self.assertNotIn("pasted", identifiers, "a pasted log is not the person's words")
        self.assertNotIn("user", identifiers, "the persisted fixture turn has no cue")
        preference = candidates[0]
        self.assertEqual(preference["role"], "user")
        self.assertNotIn(secret, preference["text"])
        self.assertNotIn("system-reminder", preference["text"])
        self.assertTrue(preference["text"].startswith("Don't hardcode [redacted]; always read the key"))
        self.assertEqual(self.store.hippocampus.pending_candidates(cap=50), 5)

    def test_the_persons_turns_come_before_newer_assistant_work_and_tool_text_is_skipped(self):
        self.store._put_message(message("decision", "We decided: never commit without asking first.", role="user"))
        for index in range(25):
            self.store._put_message(message("conclusion-%02d" % index, "Verified: the build passes. " * 8))
        self.store._put_message(message("tool-text", "[external_agent_tool_result] exit 0 " + "x" * 200))
        self.store.connection.commit()
        candidates = self.store.request({"op": "memory_candidates"})["candidates"]
        identifiers = [item["message_id"] for item in candidates]
        self.assertEqual(identifiers[0], "decision", "an older user decision is not buried under newer work")
        self.assertEqual(len(identifiers), 20)
        self.assertNotIn("tool-text", identifiers)

    def test_the_walk_for_the_persons_turns_resumes_further_back_across_calls(self):
        self.addCleanup(setattr, KNOWLEDGE, "USER_CANDIDATE_WINDOW", KNOWLEDGE.USER_CANDIDATE_WINDOW)
        KNOWLEDGE.USER_CANDIDATE_WINDOW = 3
        self.store._put_message(message("older-rule", "Always run the native gate before release.", role="user"))
        for index in range(7):
            self.store._put_message(message("work-%d" % index, "Assistant work %d" % index))
        self.store.connection.commit()
        first = self.store._user_candidate_rows(*self.store._candidate_conditions(), 1, budget_seconds=0)
        self.assertEqual(first, [], "the first bounded step reads only the newest window")
        found = []
        for _ in range(10):
            found = self.store._user_candidate_rows(*self.store._candidate_conditions(), 1, budget_seconds=0)
            if found:
                break
        self.assertEqual([row["id"] for row in found], ["older-rule"], "later calls reach older turns")

    def test_an_update_with_keep_sources_adds_evidence_without_resending_it(self):
        self.store.upsert_memory({"id": "kept", "title": "Rule", "body": "Persisted old",
                                  "sources": [{"session_id": "claude:fixture", "message_id": "old"}]})
        revised = self.store.upsert_memory({"id": "kept", "title": "Rule", "body": "Persisted old, confirmed",
                                            "keep_sources": True,
                                            "sources": [{"session_id": "claude:fixture", "message_id": "newest"}]})
        self.assertEqual([item["message_id"] for item in revised["memory"]["sources"]], ["newest", "old"])
        replaced = self.store.upsert_memory({"id": "kept", "title": "Rule", "body": "Only newest",
                                             "sources": [{"session_id": "claude:fixture", "message_id": "newest"}]})
        self.assertEqual([item["message_id"] for item in replaced["memory"]["sources"]], ["newest"],
                         "without keep_sources the sources are replaced, as before")

    def test_candidates_carry_their_nearest_memories_with_bounded_redacted_excerpts(self):
        body = "Read the signing key from the keychain. token=" + "q" * 40 + " " + "detail " * 200
        created = self.store.request({"op": "upsert_memory", "title": "Signing key lives in the keychain", "body": body,
                                      "sources": [{"session_id": "claude:fixture", "message_id": "old"}]})
        self.assertTrue(created["ok"], created)
        self.store.request({"op": "upsert_memory", "title": "Unrelated release cadence", "body": "Ship on Fridays.",
                            "sources": [{"session_id": "claude:fixture", "message_id": "old"}]})
        self.store._put_message(message("keychain", "The signing key now comes from the keychain."))
        self.store.connection.commit()
        candidates = self.store.request({"op": "memory_candidates"})["candidates"]
        keychain = next(item for item in candidates if item["message_id"] == "keychain")
        self.assertEqual([item["title"] for item in keychain["similar"]], ["Signing key lives in the keychain"])
        similar = keychain["similar"][0]
        self.assertEqual(similar["id"], created["memory"]["id"])
        self.assertLessEqual(len(similar["excerpt"]), 300)
        self.assertNotIn("q" * 40, similar["excerpt"])
        self.assertEqual(similar["source_count"], 1)
        self.assertNotIn("sources", similar, "an update keeps evidence with keep_sources; batches stay small")
        self.assertIsInstance(similar["score"], float)
        newest = next(item for item in candidates if item["message_id"] == "newest")
        self.assertEqual(newest["similar"], [], "no shared distinctive term, no similar memory")

    def test_candidate_walk_never_sorts_the_persisted_corpus(self):
        conditions, bindings = self.store._candidate_conditions()
        plan = " ".join(row[-1] for row in self.store.connection.execute(
            "EXPLAIN QUERY PLAN SELECT m.id FROM persisted.messages m WHERE " + " AND ".join(conditions) +
            " ORDER BY m.rowid DESC LIMIT 20", bindings))
        self.assertNotIn("TEMP B-TREE FOR ORDER BY", plan)

    def test_overview_never_counts_the_native_index_on_the_request_path(self):
        calls = []
        original = self.store.quick_status
        self.store.quick_status = lambda: calls.append(1) or original()
        first = self.store.request({"op": "memory_overview"})["overview"]["pipeline"]
        self.assertEqual(calls, [])
        self.assertTrue(first["counts_warming"])
        self.assertIsNone(first["messages"])
        self.assertTrue(self.store.request({"op": "memory_tick"})["ok"])
        warmed = self.store.request({"op": "memory_overview"})["overview"]["pipeline"]
        self.assertFalse(warmed["counts_warming"])

        self.assertGreaterEqual(warmed["messages"], self.store.knowledge_status()["messages"])
        self.assertEqual(warmed["messages"], 6, "Counted exactly, in slices")
        self.assertEqual(warmed["candidates_pending"], 4)

    def test_graph_leaves_vector_loading_to_the_background_tick(self):
        hippocampus = self.store.hippocampus
        self.store._semantics = object()  # embeddings on, as in the app
        hippocampus._load_memory_vectors = lambda *args, **kwargs: self.fail("the request loaded vectors")
        graph = self.store.request({"op": "memory_graph", "edges": ["similarity"]})
        self.assertTrue(graph["ok"], graph)
        self.assertIsNone(graph["similarity_available"], "Similarity is warming, not unavailable")
        hippocampus._vector_cache = {}
        self.assertTrue(self.store.request({"op": "memory_graph", "edges": ["similarity"]})["similarity_available"])

    def test_row_counts_advance_a_slice_per_call_and_match_the_exact_count(self):
        calls = 0
        while not self.store.step_row_counts(budget_seconds=0):
            calls += 1
            self.assertLess(calls, 10)
        self.assertEqual(calls, 1, "messages and vectors are each counted in their own slice")
        self.assertEqual(self.store._counted_rows("messages"), self.store.knowledge_status()["messages"])
        self.assertTrue(self.store.step_row_counts(budget_seconds=0), "Fresh counts are reused")

    def test_status_never_counts_every_message_in_one_call(self):
        exact = self.store._count_projection_rows("messages")
        self.store._count_projection_rows = lambda table: self.fail("counted messages in one call") \
            if table == "messages" else KNOWLEDGE.KnowledgeStore._count_projection_rows(self.store, table)
        with mock.patch.object(self.store, "step_row_counts", return_value=False):
            warming = self.store.knowledge_status()
        self.assertTrue(warming["counts_warming"])
        self.assertIsNone(warming["messages"], "Still counting, in bounded steps")
        status = self.store.knowledge_status()
        self.assertFalse(status["counts_warming"])
        self.assertEqual(status["messages"], exact)
        self.assertIsNotNone(status["counted_at"])

    def test_pending_count_walks_windows_across_ticks_and_matches_the_walk(self):
        hippocampus = self.store.hippocampus
        calls = 1
        while not hippocampus.step_pending(now=50.0, budget_seconds=0, window=2):
            calls += 1
        self.assertGreater(calls, 1, "the walk resumed across calls")
        conditions, bindings = self.store._candidate_conditions()
        exact = self.store._newest_candidate_rows(conditions, bindings, 501, count_only=True)
        self.assertEqual(hippocampus._pending_cache, (50.0, exact))
        self.assertEqual(hippocampus.pending_candidates(now=60.0), exact)


if __name__ == "__main__":
    unittest.main()
