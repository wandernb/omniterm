"""Lexical search ranks inside the full-text index and reads only a window of
rows; a filter that rejects most of the window widens it until enough rows
pass. Filtered memory search judges every memory once instead of per hit."""

import importlib.machinery
import importlib.util
from pathlib import Path
from unittest import mock
import unittest


_FIXTURES = importlib.util.spec_from_file_location("test_knowledge_for_window", Path(__file__).with_name("test_knowledge.py"))
test_knowledge = importlib.util.module_from_spec(_FIXTURES)
_FIXTURES.loader.exec_module(test_knowledge)
KNOWLEDGE = test_knowledge.KNOWLEDGE
_SEMANTICS = importlib.util.spec_from_file_location("omni_semantics_for_window",
                                                    Path(__file__).resolve().parents[1] / "helpers/omni_semantics.py")
SEMANTICS = importlib.util.module_from_spec(_SEMANTICS)
_SEMANTICS.loader.exec_module(SEMANTICS)
_FIXTURE = vars(test_knowledge.KnowledgeTests)


class LexicalWindowTests(unittest.TestCase):
    setUp = _FIXTURE["setUp"]
    write = _FIXTURE["write"]
    claude_message = _FIXTURE["claude_message"]
    complete_scan = _FIXTURE["complete_scan"]
    request = _FIXTURE["request"]

    def fixture(self):
        self.write(self.claude / "busy" / "noise.jsonl",
                   [self.claude_message("Widget telemetry note %d widget widget." % index, "assistant", cwd="/busy")
                    for index in range(40)])
        self.write(self.claude / "quiet" / "signal.jsonl",
                   [self.claude_message("Widget decision %d." % index, "assistant", cwd="/quiet") for index in range(3)])
        self.complete_scan()

    def test_filter_widens_the_ranked_window_until_enough_rows_pass(self):
        self.fixture()
        with mock.patch.object(KNOWLEDGE, "LEXICAL_WINDOW", 4):
            matches = self.request("search_transcripts", query="widget", workspace="/quiet", limit=3)["matches"]
        self.assertEqual(len(matches), 3)
        self.assertTrue(all(match["workspace"] == "/quiet" for match in matches))
        self.assertTrue(all("«" in match["snippet"] for match in matches))

    def test_unfiltered_ranking_matches_the_index_order(self):
        self.fixture()
        matches = self.request("search_transcripts", query="widget", limit=5)["matches"]
        self.assertEqual(len(matches), 5)
        self.assertTrue(all(match["workspace"] == "/busy" for match in matches), "the denser notes rank first")


class RelaxationTests(unittest.TestCase):
    setUp = _FIXTURE["setUp"]
    write = _FIXTURE["write"]
    claude_message = _FIXTURE["claude_message"]
    complete_scan = _FIXTURE["complete_scan"]
    request = _FIXTURE["request"]

    def fixture(self):
        self.write(self.claude / "w" / "s.jsonl", [
            self.claude_message("Release signing uses notarytool and stapler.", "assistant", cwd="/w"),
            self.claude_message("Signing needs the developer certificate.", "assistant", cwd="/w"),
            self.claude_message("Notarytool waits for Apple.", "assistant", cwd="/w"),
            self.claude_message("Docker signing is unrelated here.", "assistant", cwd="/w")])
        self.complete_scan()

    def test_too_few_all_term_matches_are_followed_by_any_term_matches(self):
        self.fixture()
        matches = self.request("search_transcripts", query="signing notarytool", limit=10)["matches"]
        self.assertIn("notarytool and stapler", matches[0]["text"], "the all-term match ranks first")
        self.assertNotIn("partial_match", matches[0])
        self.assertEqual(len(matches), 4)
        self.assertTrue(all(match.get("partial_match") for match in matches[1:]))
        self.assertTrue(all("«" in match["snippet"] for match in matches))

    def test_a_very_common_term_ranks_only_its_newest_matches(self):
        self.write(self.claude / "w" / "s.jsonl", [
            self.claude_message("signing signing signing, the oldest and densest match", "assistant", cwd="/w"),
            self.claude_message("signing step two", "assistant", cwd="/w"),
            self.claude_message("signing step three", "assistant", cwd="/w")])
        self.complete_scan()
        everything = [match["text"] for match in self.request("search_transcripts", query="signing", limit=10)["matches"]]
        self.assertEqual(len(everything), 3, "under the cap, every match is ranked")
        with mock.patch.object(KNOWLEDGE, "LEXICAL_RANK_CAP", 2):
            capped = [match["text"] for match in self.request("search_transcripts", query="signing",
                                                                 limit=10)["matches"]]
        self.assertEqual(sorted(capped), ["signing step three", "signing step two"])

    def test_relaxation_keeps_negations_and_phrases(self):
        self.fixture()
        texts = [match["text"] for match in self.request(
            "search_transcripts", query='"developer certificate" notarytool -docker', limit=10)["matches"]]
        self.assertFalse(any("Docker" in text for text in texts))
        self.assertTrue(any("developer certificate" in text for text in texts))
        self.assertFalse(any(text == "Signing needs the developer." for text in texts))

    def test_enough_all_term_matches_are_not_relaxed(self):
        self.fixture()
        matches = self.request("search_transcripts", query="signing", limit=10)["matches"]
        self.assertFalse(any(match.get("partial_match") for match in matches))

    def test_an_any_word_pass_over_budget_adds_nothing(self):
        self.fixture()
        with mock.patch.object(KNOWLEDGE, "LEXICAL_RELAX_SECONDS", -1.0), \
                mock.patch.object(KNOWLEDGE, "LEXICAL_RELAX_CHECK_STEPS", 1):
            matches = self.request("search_transcripts", query="signing notarytool", limit=10)["matches"]
        self.assertEqual(len(matches), 1, "only the all-word match: the any-word pass ran out of time")
        self.assertEqual(len(self.request("search_transcripts", query="signing notarytool", limit=10)["matches"]), 4,
                         "the connection works normally afterwards")

    def test_stopwords_never_widen_a_search(self):
        self.fixture()
        matches = self.request("search_transcripts", query="the notarytool for", limit=10)["matches"]
        self.assertTrue(all("otarytool" in match["text"] for match in matches), [m["text"] for m in matches])

    def test_a_memory_type_on_transcripts_matches_nothing_at_once(self):
        self.fixture()
        for mode in ("lexical", "hybrid"):
            self.assertEqual(self.request("search_transcripts", query="type:procedural signing", mode=mode)["matches"], [])
        self.assertTrue(self.request("search_transcripts", query="type:message signing")["matches"])

    def test_listing_memories_by_text_stays_strict(self):
        self.fixture()
        message = self.request("search_transcripts", query="stapler", limit=1)["matches"][0]
        for identifier, body in (("a", "Release signing with notarytool."), ("b", "Signing alone.")):
            self.request("upsert_memory", id=identifier, title=identifier, body=body,
                         sources=[{"session_id": message["session_id"], "message_id": message["id"]}])
        listed = {memory["id"] for memory in self.request("list_memories", query="signing notarytool")["memories"]}
        self.assertEqual(listed, {"a"})
        found = [memory["id"] for memory in self.request("search_memories", query="signing notarytool",
                                                           mode="lexical")["memories"]]
        self.assertEqual(found, ["a", "b"])


class MemoryDefaultModeTests(unittest.TestCase):
    setUp = _FIXTURE["setUp"]
    write = _FIXTURE["write"]
    claude_message = _FIXTURE["claude_message"]
    complete_scan = _FIXTURE["complete_scan"]
    request = _FIXTURE["request"]

    def memory(self):
        self.write(self.claude / "w" / "s.jsonl", [self.claude_message("Release signing notes.", "assistant", cwd="/w")])
        self.complete_scan()
        message = self.request("search_transcripts", query="signing", limit=1)["matches"][0]
        self.request("upsert_memory", id="m1", title="Signing", body="Release signing uses notarytool.",
                     sources=[{"session_id": message["session_id"], "message_id": message["id"]}])

    def test_a_semantic_default_reaches_the_semantic_index(self):
        self.memory()
        patcher = mock.patch.object(KNOWLEDGE, "MEMORY_SEARCH_DEFAULT", "hybrid")
        patcher.start()
        self.addCleanup(patcher.stop)
        seen = []

        class Semantics:
            def search_memories(self, arguments):
                seen.append(arguments["mode"])
                return {"memories": [], "mode": arguments["mode"]}

        self.store._semantics = Semantics()
        self.assertEqual(self.request("search_memories", query="signing")["mode"], "hybrid")
        self.request("search_memories", query="signing", mode="semantic")
        self.assertEqual(seen, ["hybrid", "semantic"])
        self.assertEqual(self.request("search_memories", query="signing", mode="lexical")["mode"], "lexical")

    def test_default_falls_back_to_lexical_without_embeddings(self):
        self.memory()
        patcher = mock.patch.object(KNOWLEDGE, "MEMORY_SEARCH_DEFAULT", "hybrid")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store._semantics = False
        found = self.request("search_memories", query="signing")
        self.assertEqual((found["mode"], [memory["id"] for memory in found["memories"]]), ("lexical", ["m1"]))
        failed = self.store.request({"op": "search_memories", "query": "signing", "mode": "hybrid"})
        self.assertEqual((failed["ok"], failed.get("code")), (False, "embedding_unavailable"))

    def test_default_falls_back_when_the_model_fails_at_query_time(self):
        self.memory()
        patcher = mock.patch.object(KNOWLEDGE, "MEMORY_SEARCH_DEFAULT", "hybrid")
        patcher.start()
        self.addCleanup(patcher.stop)

        class Unavailable(ValueError):
            code = "embedding_unavailable"

        class Semantics:
            def search_memories(self, arguments):
                raise Unavailable("The local model returned an empty vector")

        self.store._semantics = Semantics()
        self.assertEqual(self.request("search_memories", query="signing")["mode"], "lexical")
        failed = self.store.request({"op": "search_memories", "query": "signing", "mode": "semantic"})
        self.assertFalse(failed["ok"])

    def test_the_shipped_default_is_the_eval_winner(self):
        self.assertEqual(KNOWLEDGE.MEMORY_SEARCH_DEFAULT, "lexical")
        loader = importlib.machinery.SourceFileLoader(
            "omni_mcp_for_window", str(Path(__file__).resolve().parents[1] / "helpers/omni-mcp"))
        mcp = importlib.util.module_from_spec(importlib.util.spec_from_loader("omni_mcp_for_window", loader))
        loader.exec_module(mcp)
        tools = {tool["name"]: tool for tool in mcp.TOOLS}
        self.assertEqual(tools["search_memories"]["inputSchema"]["properties"]["mode"]["default"], "lexical")
        self.assertEqual(tools["search_transcripts"]["inputSchema"]["properties"]["mode"]["default"], "lexical")


class FilteredMemoryJudgementTests(unittest.TestCase):
    setUp = _FIXTURE["setUp"]
    write = _FIXTURE["write"]
    claude_message = _FIXTURE["claude_message"]
    complete_scan = _FIXTURE["complete_scan"]
    request = _FIXTURE["request"]

    def test_lexical_and_listed_memories_are_scoped_by_their_cited_messages(self):
        self.write(self.claude / "one" / "a.jsonl", [self.claude_message("Evidence one.", "assistant", cwd="/one")])
        self.write(self.claude / "two" / "b.jsonl", [self.claude_message("Evidence two.", "assistant", cwd="/two")])
        self.complete_scan()
        first = self.request("get_session", session_id="claude:a")["messages"][0]
        second = self.request("get_session", session_id="claude:b")["messages"][0]
        for identifier, message in (("m1", first), ("m2", second)):
            self.request("upsert_memory", id=identifier, title=identifier, body="Release notes " + identifier,
                         sources=[{"session_id": message["session_id"], "message_id": message["id"]}])
        found = self.request("search_memories", query="release notes missingword", workspace="/one", mode="lexical")
        self.assertEqual([memory["id"] for memory in found["memories"]], ["m1"])
        listed = self.request("search_memories", workspace="/two")
        self.assertEqual([memory["id"] for memory in listed["memories"]], ["m2"])
        self.request("upsert_memory", id="m3", title="m3", body="Release notes m3",
                     sources=[{"session_id": first["session_id"], "message_id": first["id"]}])
        found = self.request("search_memories", query="release notes", workspace="/one", mode="lexical")
        self.assertEqual(sorted(memory["id"] for memory in found["memories"]), ["m1", "m3"],
                         "the cached scope follows new memories")

    def test_cited_scope_copy_warms_in_bounded_steps_and_follows_new_citations(self):
        self.write(self.claude / "one" / "a.jsonl",
                   [self.claude_message("Evidence %d." % index, "assistant", cwd="/one") for index in range(3)])
        self.complete_scan()
        messages = self.request("get_session", session_id="claude:a")["messages"]
        for index, message in enumerate(messages):
            self.request("upsert_memory", id="m%d" % index, title="m", body="Notes %d" % index,
                         sources=[{"session_id": message["session_id"], "message_id": message["id"]}])
        self.assertFalse(self.store.warm_cited_scope(max_batches=0), "no batch allowed: not warm yet")
        self.assertTrue(self.store.warm_cited_scope(max_batches=1))
        copied = self.store.connection.execute("SELECT count(*),min(workspace) FROM temp.cited_scope").fetchone()
        self.assertEqual(tuple(copied), (3, "/one"))
        self.assertEqual(sorted(self.store._scoped_memory_ids("memories", ["m.workspace=?"], ["/one"])),
                         ["m0", "m1", "m2"])
        self.assertEqual(self.store._scoped_memory_ids("memories", ["m.workspace=?"], ["/two"]), [])

    def test_cited_sources_are_parsed_once_until_a_memory_changes(self):
        # Parsing every memory's sources cost ~0.2 s live, more than the any-word pass's
        # whole budget, so scoped searches that needed it returned nothing.
        self.write(self.claude / "one" / "a.jsonl", [self.claude_message("Evidence one.", "assistant", cwd="/one")])
        self.complete_scan()
        message = self.request("get_session", session_id="claude:a")["messages"][0]
        source = [{"session_id": message["session_id"], "message_id": message["id"]}]
        self.request("upsert_memory", id="m1", title="m1", body="Release notes", sources=source)
        first = self.store._cited_messages("memories")
        self.assertIs(self.store._cited_messages("memories"), first, "unchanged table: no re-parse")
        scoped = self.store._scoped_memory_ids("memories", ["m.workspace=?"], ["/one"])
        self.assertIs(self.store._scoped_memory_ids("memories", ["m.workspace=?"], ["/one"]), scoped)
        self.request("upsert_memory", id="m1", title="m1", body="Release notes, revised", sources=source)
        self.assertIsNot(self.store._cited_messages("memories"), first, "a revision invalidates it")
        self.request("upsert_memory", id="m2", title="m2", body="Release notes two", sources=source)
        self.assertEqual(sorted(self.store._scoped_memory_ids("memories", ["m.workspace=?"], ["/one"])), ["m1", "m2"])

    def test_scope_and_type_are_judged_once_for_all_memories(self):
        self.write(self.claude / "one" / "a.jsonl", [self.claude_message("Evidence one.", "assistant", cwd="/one")])
        self.write(self.claude / "two" / "b.jsonl", [self.claude_message("Evidence two.", "assistant", cwd="/two")])
        self.complete_scan()
        first = self.request("get_session", session_id="claude:a")["messages"][0]
        second = self.request("get_session", session_id="claude:b")["messages"][0]
        for identifier, message, kind in (("p1", first, "procedural"), ("d1", first, "declarative"),
                                          ("p2", second, "procedural")):
            self.request("upsert_memory", id=identifier, title=identifier, body="Release notes " + identifier,
                         memory_type=kind, sources=[{"session_id": message["session_id"], "message_id": message["id"]}])
        index = SEMANTICS.SemanticIndex.__new__(SEMANTICS.SemanticIndex)
        index.store, index.connection = self.store, self.store.connection
        allowed = index._allowed_memories({"workspace": "/one", "type": "procedural"}, self.store._memory_matches)
        self.assertEqual(allowed, {"p1"})
        self.assertEqual(index._allowed_memories({"type": "procedural"}, self.store._memory_matches), {"p1", "p2"})


if __name__ == "__main__":
    unittest.main()
