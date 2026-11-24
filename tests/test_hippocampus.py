import importlib.util
import time
import unittest
from pathlib import Path
from unittest import mock



ROOT = Path(__file__).resolve().parents[1]
_FIXTURES = importlib.util.spec_from_file_location("test_knowledge_for_hippocampus", Path(__file__).with_name("test_knowledge.py"))
test_knowledge = importlib.util.module_from_spec(_FIXTURES)
_FIXTURES.loader.exec_module(test_knowledge)
_SPEC = importlib.util.spec_from_file_location("omni_hippocampus_for_tests", ROOT / "helpers/omni_hippocampus.py")
HIPPOCAMPUS = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(HIPPOCAMPUS)
DAY = 86400.0


class HippocampusModelTests(unittest.TestCase):
    def test_legacy_memory_defaults_to_declarative_base_weight(self):
        value = HIPPOCAMPUS.attributes({"sources": [{}, {}], "updated": 1000.0}, now=1000.0)
        self.assertEqual(value["memory_type"], "declarative")
        self.assertEqual(value["weight"], 7.0)
        self.assertEqual(value["evidence_count"], 2)
        self.assertFalse(value["expiry_candidate"])

    def test_weight_decays_per_day_until_it_becomes_an_expiry_candidate(self):
        stored = {"memory_type": "prospective", "weight": 1.0, "last_reinforced_at": 0.0}
        self.assertEqual(HIPPOCAMPUS.attributes(stored, now=5 * DAY)["effective_weight"], 0.5)
        self.assertFalse(HIPPOCAMPUS.attributes(stored, now=5 * DAY)["expiry_candidate"])
        late = HIPPOCAMPUS.attributes(stored, now=6 * DAY)
        self.assertAlmostEqual(late["effective_weight"], 0.4)
        self.assertTrue(late["expiry_candidate"])
        self.assertEqual(HIPPOCAMPUS.attributes(stored, now=100 * DAY)["effective_weight"], 0.0)

    def test_workspace_hub_label_names_the_repository_of_a_worktree(self):
        self.assertEqual(HIPPOCAMPUS.workspace_label("/Users/me/Projects/basalt/core/@head"), "core")
        self.assertEqual(HIPPOCAMPUS.workspace_label("/Users/me/Projects/lumen/"), "lumen")
        self.assertEqual(HIPPOCAMPUS.workspace_label("/@head"), "@head")

    def test_pinned_memory_does_not_decay(self):
        stored = {"memory_type": "episodic", "weight": 1.0, "last_reinforced_at": 0.0, "pinned": True}
        value = HIPPOCAMPUS.attributes(stored, now=365 * DAY)
        self.assertEqual(value["effective_weight"], 1.0)
        self.assertFalse(value["expiry_candidate"])

    def test_revision_with_evidence_reinforces_from_the_decayed_weight(self):
        existing = {"memory_type": "episodic", "weight": 5.0, "last_reinforced_at": 0.0, "evidence_count": 1}
        reinforced = HIPPOCAMPUS.next_attributes(existing, {}, 2, True, 10 * DAY)
        self.assertEqual(reinforced["weight"], 5.5)  # 5.0 - 10 days × 0.05 + 2 new sources × 0.5
        self.assertEqual(reinforced["evidence_count"], 3)
        self.assertEqual(reinforced["last_reinforced_at"], 10 * DAY)
        unchanged = HIPPOCAMPUS.next_attributes(existing, {}, 0, False, 10 * DAY)
        self.assertEqual(unchanged["weight"], 5.0)
        self.assertEqual(unchanged["last_reinforced_at"], 0.0)

    def test_invalid_attributes_are_rejected(self):
        for arguments in ({"memory_type": "semantic"}, {"weight": 11}, {"weight": True}, {"lineage": "x"}):
            with self.assertRaises(HIPPOCAMPUS.HippocampusError):
                HIPPOCAMPUS.next_attributes(None, arguments, 1, True, 0.0)

    def test_digest_is_grouped_by_type_and_capped(self):
        memories = [{"id": "m%03d" % index, "title": "Title %d" % index, "body": "Body sentence. More text.",
                     "memory_type": "procedural" if index % 2 else "declarative",
                     "effective_weight": float(index % 10), "evidence_count": 1} for index in range(400)]
        markdown, included = HIPPOCAMPUS.Hippocampus.render_digest(memories, "/workspace", 0.0)
        self.assertLessEqual(len(markdown.encode("utf-8")), HIPPOCAMPUS.DIGEST_MAX_BYTES + 1)
        self.assertIn("## Declarative", markdown)
        self.assertLess(markdown.index("## Declarative"), markdown.index("## Procedural"))
        self.assertLess(len(included), 400)
        self.assertIn("— Body sentence.", markdown)
        self.assertNotIn("More text", markdown)

    def test_digest_opens_with_topics_within_its_budget(self):
        memories = [{"id": "m%d" % index, "title": "Title %d" % index, "body": "Body.", "memory_type": "declarative",
                     "effective_weight": 5.0, "evidence_count": 1} for index in range(4)]
        topics = [{"concepts": ["file:helpers/omni_kb.py", "command:omni knowledge"], "members": ["m0", "m1"]},
                  {"concepts": ["error:SQLITE_BUSY"], "members": ["m2", "m3"]}]
        markdown, _ = HIPPOCAMPUS.Hippocampus.render_digest(memories, None, 0.0, topics)
        self.assertLess(markdown.index("## Topics"), markdown.index("## Declarative"))
        self.assertIn("— 2 memories: Title 0; Title 1", markdown)
        self.assertNotIn("## Topics", HIPPOCAMPUS.Hippocampus.render_digest(memories, None, 0.0)[0])

    def test_mindmap_nests_type_tag_memory(self):
        memories = [{"id": "a", "title": "A", "memory_type": "procedural", "tags": ["build"], "effective_weight": 7.0,
                     "expiry_candidate": False},
                    {"id": "b", "title": "B", "memory_type": "procedural", "tags": [], "effective_weight": 6.0,
                     "expiry_candidate": False},
                    {"id": "c", "title": "C", "memory_type": "declarative", "tags": ["build"], "effective_weight": 6.0,
                     "expiry_candidate": True}]
        tree = HIPPOCAMPUS.Hippocampus.mindmap(memories)
        self.assertEqual([branch["label"] for branch in tree["children"]], ["declarative", "procedural"])
        procedural = tree["children"][1]
        self.assertEqual({group["label"] for group in procedural["children"]}, {"build", "untagged"})
        self.assertTrue(tree["children"][0]["children"][0]["children"][0]["expiry_candidate"])


_FIXTURE = vars(test_knowledge.KnowledgeTests)


class HippocampusStoreTests(unittest.TestCase):
    setUp = _FIXTURE['setUp']
    write = _FIXTURE['write']
    claude_message = _FIXTURE['claude_message']
    complete_scan = _FIXTURE['complete_scan']
    request = _FIXTURE['request']

    def messages(self, count, session="session", workspace="/workspace"):
        self.write(self.claude / "workspace" / (session + ".jsonl"),
                   [self.claude_message("Durable conclusion number %d about builds. " % index * 3, "assistant",
                                        cwd=workspace) for index in range(count)])
        self.complete_scan()
        return self.request("get_session", session_id="claude:" + session)["messages"]

    @staticmethod
    def source(message):
        return {"session_id": message["session_id"], "message_id": message["id"]}

    def test_upsert_records_type_weight_and_journal(self):
        messages = self.messages(3)
        created = self.request("upsert_memory", id="sign", title="Signing", body="Sign native products.",
                               sources=[self.source(messages[0])], memory_type="procedural", tags=["build"],
                               reason="Verified twice", run_id="run-1", agent="Omni Memory")["memory"]
        self.assertEqual((created["memory_type"], created["weight"], created["evidence_count"]), ("procedural", 7.0, 1))
        revised = self.request("upsert_memory", id="sign", title="Signing", body="Sign and verify native products.",
                               sources=[self.source(messages[0]), self.source(messages[1])],
                               tags=["build"])["memory"]
        self.assertEqual(revised["revision"], 2)
        self.assertEqual(revised["memory_type"], "procedural")
        self.assertEqual(revised["evidence_count"], 2)
        self.assertGreater(revised["weight"], 7.0)
        events = self.request("memory_events", memory_id="sign")["events"]
        self.assertEqual([event["kind"] for event in events], ["memory_revised", "memory_created"])
        self.assertEqual(events[1]["actor"], {"run_id": "run-1", "agent": "Omni Memory"})
        self.assertEqual(events[1]["reason"], "Verified twice")
        self.assertEqual(events[0]["changed"], ["body", "sources"])
        self.assertEqual(events[0]["message_ids"], [messages[1]["id"]])
        searched = self.request("search_memories", query="verify")["memories"][0]
        self.assertEqual(searched["memory_type"], "procedural")

    def archival_fixture(self):
        messages = self.messages(4)
        fading = {"memory_type": "prospective", "weight": 1.0}  # an expiry candidate within days
        self.request("upsert_memory", id="fade", title="Fading reminder", body="Rotate the staging token.",
                     sources=[self.source(messages[0])], **fading)
        self.request("upsert_memory", id="pin", title="Pinned reminder", body="Keep this.",
                     sources=[self.source(messages[0])], pinned=True, **fading)
        self.request("upsert_memory", id="well", title="Well sourced", body="Three sources.",
                     sources=[self.source(message) for message in messages[:3]], **fading)
        self.request("upsert_memory", id="used", title="Recently opened", body="Opened lately.",
                     sources=[self.source(messages[1])], **fading)
        return time.time()

    def test_archival_takes_fading_unused_memories_with_few_sources(self):
        start = self.archival_fixture()
        hippocampus = self.store.hippocampus
        hippocampus.touch("used", now=start + 20 * DAY)
        self.assertEqual(hippocampus.maybe_archive(now=start + 29 * DAY, force=True), {"archived": 0, "deleted": 0})
        self.assertEqual(hippocampus.maybe_archive(now=start + 31 * DAY, force=True), {"archived": 1, "deleted": 0})
        # Not due again until tomorrow.
        self.assertIsNone(hippocampus.maybe_archive(now=start + 31 * DAY + 60))
        archived = self.request("get_memory", id="fade")["memory"]
        self.assertTrue(archived["archived"])
        self.assertEqual(archived["archived_at"], start + 31 * DAY)
        event = self.request("memory_events", kind="memory_archived")["events"][0]
        self.assertEqual(event["memory_id"], "fade")
        # Another 90 days unused after archival: deleted, with a journal event.
        self.assertEqual(hippocampus.maybe_archive(now=start + 120 * DAY, force=True)["deleted"], 0)
        self.assertEqual(hippocampus.maybe_archive(now=start + 122 * DAY, force=True)["deleted"], 1)
        self.assertFalse(self.store.connection.execute("SELECT 1 FROM memories WHERE id='fade'").fetchone())
        deleted = self.request("memory_events", kind="memory_deleted")["events"][0]
        self.assertEqual((deleted["memory_id"], deleted["reason"]), ("fade", "archived_unused"))
        remaining = {memory["id"] for memory in self.request("list_memories")["memories"]}
        self.assertTrue({"pin", "well"} <= remaining)

    def test_archived_memories_are_hidden_by_default_and_restorable(self):
        start = self.archival_fixture()
        self.store.hippocampus.maybe_archive(now=start + 31 * DAY, force=True)
        listed = self.request("list_memories")
        ids = {memory["id"] for memory in listed["memories"]}
        self.assertNotIn("fade", ids)
        self.assertEqual(listed["archived_total"], 2)  # fade and used, both unused
        only = self.request("list_memories", archived=True)["memories"]
        self.assertEqual({memory["id"] for memory in only}, {"fade", "used"})
        self.assertNotIn("fade", {node["id"] for node in self.request("memory_graph")["nodes"]})
        self.assertIn("fade", {node["id"] for node in self.request("memory_graph", archived=True)["nodes"]})
        self.assertNotIn("fade", {memory["id"] for memory in self.request("search_memories", query="staging")["memories"]})
        self.assertIn("fade", {memory["id"] for memory in
                               self.request("search_memories", query="staging", archived=True)["memories"]})
        self.assertEqual(self.request("memory_overview")["overview"]["archived"], 2)
        # Intake still sees archived memories, so it revises instead of duplicating.
        similar = self.store._similar_memories("Rotate the staging token")
        self.assertIn({"id": "fade", "archived": True}, [{"id": item["id"], "archived": item.get("archived")}
                                                        for item in similar])
        restored = self.request("update_memory", id="fade", archived=False)
        self.assertTrue(restored["changed"])
        self.assertFalse(restored["memory"]["archived"])
        self.assertIn("fade", {memory["id"] for memory in self.request("list_memories")["memories"]})
        self.assertEqual(self.request("memory_events", kind="memory_restored")["events"][0]["memory_id"], "fade")
        # Restoring is a use: the next sweep leaves it alone.
        self.store.hippocampus.maybe_archive(now=time.time() + DAY, force=True)
        self.assertFalse(self.request("get_memory", id="fade")["memory"]["archived"])

    def test_archived_memories_stay_offered_to_intake_and_leave_the_digest(self):
        start = self.archival_fixture()
        self.store.hippocampus.maybe_archive(now=start + 31 * DAY, force=True)
        self.assertTrue(self.request("get_memory", id="fade")["memory"]["archived"])
        similar = self.store._similar_memories("Rotate the staging token every week.")
        fade = [item for item in similar if item["id"] == "fade"]
        self.assertEqual(len(fade), 1, "intake still sees it, so it revises rather than duplicates")
        self.assertTrue(fade[0].get("archived"))
        digest = self.request("memory_digest", regenerate=True)["digest"]["markdown"]
        self.assertNotIn("Fading reminder", digest)

    def test_revising_an_archived_memory_restores_it(self):
        start = self.archival_fixture()
        self.store.hippocampus.maybe_archive(now=start + 31 * DAY, force=True)
        message = self.messages(1, session="later")[0]
        revised = self.request("upsert_memory", id="fade", title="Fading reminder", body="Rotate the token weekly.",
                               sources=[self.source(message)], keep_sources=True)["memory"]
        self.assertFalse(revised["archived"])
        self.assertEqual(self.request("memory_events", kind="memory_restored")["events"][0]["reason"], "Revised")

    def test_opening_a_memory_marks_it_used_at_most_hourly(self):
        self.archival_fixture()
        hippocampus = self.store.hippocampus
        self.assertTrue(hippocampus.touch("pin", now=time.time() + 2 * 3600))
        first = self.request("get_memory", id="pin")["memory"]["last_used"]
        self.assertFalse(hippocampus.touch("pin", now=first + 600))
        self.assertTrue(hippocampus.touch("pin", now=first + 3700))
        self.assertEqual(self.request("get_memory", id="pin")["memory"]["last_used"], first + 3700)
        # Weight changes keep the use and archival fields.
        self.request("update_memory", id="pin", weight=5.0)
        self.assertEqual(self.request("get_memory", id="pin")["memory"]["last_used"], first + 3700)

    def test_type_change_alone_is_a_revision(self):
        message = self.messages(1)[0]
        values = {"id": "x", "title": "T", "body": "B.", "sources": [self.source(message)]}
        self.request("upsert_memory", **values)
        self.assertFalse(self.request("upsert_memory", **values)["changed"])
        changed = self.request("upsert_memory", **values, memory_type="spatial")
        self.assertTrue(changed["changed"])
        self.assertEqual(changed["memory"]["memory_type"], "spatial")

    def test_candidates_and_dismissals_are_journaled_and_counted(self):
        messages = self.messages(3)
        offered = self.request("memory_candidates")["candidates"]
        self.assertEqual(len(offered), 3)
        self.request("dismiss_memory_candidate", message_id=offered[0]["message_id"], reason="Transient status")
        self.request("upsert_memory", title="Builds", body="A conclusion.", sources=[self.source(messages[1])])
        kinds = [event["kind"] for event in self.request("memory_events")["events"]]
        self.assertEqual(kinds, ["memory_created", "candidate_dismissed", "candidates_offered"])
        dismissed = self.request("memory_events", kind="candidate_dismissed")["events"][0]
        self.assertEqual(dismissed["reason"], "Transient status")
        self.warm_counts()
        pipeline = self.request("memory_overview")["overview"]["pipeline"]
        self.assertEqual(pipeline["candidates_pending"], 1)
        self.assertEqual(pipeline["candidates_considered"], 2)
        self.assertEqual(pipeline["candidates_cited"], 1)
        self.assertEqual(pipeline["candidates_dismissed"], 1)

    def test_batch_dismissal_validates_everything_first(self):
        messages = self.messages(3)
        bad = self.store.request({"op": "dismiss_memory_candidates", "items": [
            {"message_id": messages[0]["id"], "reason": "Status"}, {"message_id": "missing", "reason": "x"}]})
        self.assertFalse(bad["ok"])
        self.assertEqual(len(self.request("memory_candidates")["candidates"]), 3)
        done = self.request("dismiss_memory_candidates", run_id="run-9", agent="Omni Memory", items=[
            {"message_id": messages[0]["id"], "reason": "Status"}, {"message_id": messages[1]["id"], "reason": "Noise"}])
        self.assertEqual(done["dismissed"], [messages[0]["id"], messages[1]["id"]])
        self.assertEqual([c["message_id"] for c in self.request("memory_candidates")["candidates"]], [messages[2]["id"]])
        dismissed = self.request("memory_events", kind="candidate_dismissed")["events"]
        self.assertEqual([event["reason"] for event in dismissed], ["Noise", "Status"])
        self.assertEqual(dismissed[0]["actor"], {"run_id": "run-9", "agent": "Omni Memory"})
        self.assertFalse(self.store.request({"op": "dismiss_memory_candidates", "items": []})["ok"])

    def test_update_memory_attributes_its_actor(self):
        message = self.messages(1)[0]
        self.request("upsert_memory", id="a", title="A", body="A.", sources=[self.source(message)])
        self.request("update_memory", id="a", pinned=True, run_id="run-3", agent="Omni Memory",
                     actor={"run_id": "forged"})
        event = self.request("memory_events", kind="memory_attributes")["events"][0]
        self.assertEqual(event["actor"], {"run_id": "run-3", "agent": "Omni Memory"})

    def test_candidates_scoped_to_a_session_keep_the_consolidator_exclusion(self):
        self.messages(2)
        other = self.messages(2, session="consolidator")
        scoped = self.request("memory_candidates", session_id="claude:consolidator")["candidates"]
        self.assertEqual(len(scoped), 2)
        self.request("update_memory_sessions", session_ids=["claude:consolidator"])
        self.assertEqual(self.request("memory_candidates", session_id="claude:consolidator")["candidates"], [])
        self.assertEqual(len(self.request("memory_candidates", session_id="claude:session")["candidates"]), 2)
        self.assertTrue(other)

    def test_a_mirrored_message_is_offered_once(self):
        text = "The same durable conclusion from two sources. " * 3
        self.write(self.claude / "workspace/mirror.jsonl", [self.claude_message(text, "assistant")])
        self.complete_scan()
        original = self.request("memory_candidates")["candidates"]
        self.assertEqual(len(original), 1)
        mirror = dict(self.store.connection.execute("SELECT * FROM messages").fetchone())
        for key in ("source:zz-mirror", "source:zz-other"):
            source = dict(self.store._document_value(mirror["source_key"]), key=key, path="/mirror/" + key)
            self.store._put_source(source)
        mirror.update(id="mirror-id", source_key="source:zz-mirror", provider="codex")
        self.store.connection.execute("UPDATE " + self.store.write_table("messages") + " SET provider='codex'")
        self.store._put_message(mirror)
        self.store.connection.commit()
        offered = self.request("memory_candidates")["candidates"]
        self.assertEqual([item["message_id"] for item in offered], [original[0]["message_id"]])
        other = dict(mirror, id="other-session", session_id="claude:elsewhere", source_key="source:zz-other")
        self.store._put_message(other)
        self.store.connection.commit()
        self.assertEqual(len(self.request("memory_candidates")["candidates"]), 2, "Only the same session is deduplicated")

    def test_status_counts_orphan_threads_and_caches_them(self):
        import sqlite3
        from contextlib import closing
        codex = self.home / ".codex"
        codex.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(codex / "state_5.sqlite")) as connection, connection:
            connection.execute("CREATE TABLE threads(id TEXT PRIMARY KEY)")
            connection.execute("INSERT INTO threads VALUES('known')")
        with closing(sqlite3.connect(codex / "thread_history_1.sqlite")) as connection, connection:
            connection.execute("CREATE TABLE thread_items(thread_id TEXT, rollout_ordinal INTEGER)")
            connection.executemany("INSERT INTO thread_items VALUES(?,?)",
                                   [("known", 1), ("known", 2), ("gone", 1), ("lost", 1), ("lost", 2)])
        status = self.request("knowledge_status")["status"]
        self.assertEqual(status["orphan_threads"], 2)
        self.assertEqual((status["stale_native_sources"], status["mirrored_sessions"]), (0, 0))
        with closing(sqlite3.connect(codex / "thread_history_1.sqlite")) as connection, connection:
            connection.execute("INSERT INTO thread_items VALUES('third', 1)")
        self.assertEqual(self.request("knowledge_status")["status"]["orphan_threads"], 2, "Cached for half an hour")
        self.store._orphan_cache = None
        self.assertEqual(self.request("knowledge_status")["status"]["orphan_threads"], 3)
        self.warm_counts()
        self.assertEqual(self.request("memory_overview")["overview"]["pipeline"]["orphan_threads"], 3)

    def test_events_paginate_newest_first(self):
        message = self.messages(1)[0]
        for index in range(5):
            self.request("upsert_memory", id="m%d" % index, title="T%d" % index, body="B.", sources=[self.source(message)])
        first = self.request("memory_events", limit=2)
        self.assertEqual([event["memory_id"] for event in first["events"]], ["m4", "m3"])
        second = self.request("memory_events", limit=2, before=first["next"])
        self.assertEqual([event["memory_id"] for event in second["events"]], ["m2", "m1"])
        last = self.request("memory_events", limit=2, before=second["next"])
        self.assertEqual([event["memory_id"] for event in last["events"]], ["m0"])
        self.assertIsNone(last["next"])
        self.assertFalse(self.store.request({"op": "memory_events", "kind": "nope"})["ok"])

    def test_journal_is_bounded(self):
        with mock.patch.object(self.store.hippocampus_module, "EVENT_RETENTION", 4), \
                mock.patch.object(self.store.hippocampus_module, "EVENT_PRUNE_BATCH", 2):
            for _ in range(9):
                self.store.hippocampus.record("ingest_snapshot")
            self.assertLessEqual(self.store.hippocampus._event_count(), 6)

    def test_update_memory_pins_reinforces_and_retypes(self):
        message = self.messages(1)[0]
        self.request("upsert_memory", id="p", title="Plan", body="Do it later.", sources=[self.source(message)],
                     memory_type="prospective")
        pinned = self.request("update_memory", id="p", pinned=True)["memory"]
        self.assertTrue(pinned["pinned"])
        retyped = self.request("update_memory", id="p", memory_type="episodic")["memory"]
        self.assertEqual((retyped["memory_type"], retyped["weight"]), ("episodic", 5.0))
        boosted = self.request("update_memory", id="p", reinforce=True)["memory"]
        self.assertEqual(boosted["weight"], 5.5)
        self.assertFalse(self.request("update_memory", id="p", pinned=True)["changed"])
        self.assertEqual(self.request("get_memory", id="p")["memory"]["revision"], 1)
        changes = [event["changes"] for event in self.request("memory_events", kind="memory_attributes")["events"]]
        self.assertEqual(changes[-1], {"pinned": [False, True]})
        self.assertFalse(self.store.request({"op": "update_memory", "id": "missing"})["ok"])

    def test_list_filters_and_sorts(self):
        messages = self.messages(2)
        other = self.messages(1, session="other", workspace="/elsewhere")
        self.request("upsert_memory", id="a", title="Alpha", body="A.", sources=[self.source(messages[0])],
                     memory_type="procedural", tags=["build"], weight=3.0)
        self.request("upsert_memory", id="b", title="Beta", body="B.", sources=[self.source(messages[1])], tags=["ops"])
        self.request("upsert_memory", id="c", title="Gamma", body="C.", sources=[self.source(other[0])],
                     memory_type="procedural", weight=9.0)
        listed = self.request("list_memories")
        self.assertEqual([memory["id"] for memory in listed["memories"]], ["c", "b", "a"])
        self.assertEqual(listed["total"], 3)
        self.assertEqual([m["id"] for m in self.request("list_memories", memory_type="procedural")["memories"]], ["c", "a"])
        self.assertEqual([m["id"] for m in self.request("list_memories", tag="build")["memories"]], ["a"])
        self.assertEqual([m["id"] for m in self.request("list_memories", workspace="/elsewhere")["memories"]], ["c"])
        self.assertEqual([m["id"] for m in self.request("list_memories", sort="title")["memories"]], ["a", "b", "c"])
        self.assertEqual([m["id"] for m in self.request("list_memories", query="Gamma")["memories"]], ["c"])
        self.assertEqual([m["id"] for m in self.request("list_memories", limit=1, offset=1)["memories"]], ["b"])
        self.assertIn("procedural", listed["types"])

    def test_get_memory_returns_evidence_revisions_events_and_relations(self):
        messages = self.messages(2)
        self.request("upsert_memory", id="a", title="Alpha", body="A.", sources=[self.source(messages[0])], tags=["build"])
        self.request("upsert_memory", id="a", title="Alpha", body="A revised.", sources=[self.source(messages[0])],
                     tags=["build"])
        self.request("upsert_memory", id="b", title="Beta", body="B.", sources=[self.source(messages[1])], tags=["build"])
        memory = self.request("get_memory", id="a")["memory"]
        self.assertEqual(memory["evidence"][0]["role"], "assistant")
        self.assertIn("Durable conclusion number 0", memory["evidence"][0]["excerpt"])
        self.assertEqual(memory["workspaces"], ["/workspace"])
        self.assertEqual([revision["revision"] for revision in memory["revisions"]], [2, 1])
        self.assertEqual(len(memory["events"]), 2)
        self.assertEqual({edge["kind"] for edge in memory["related"]}, {"session", "tag"})
        self.assertFalse(self.store.request({"op": "get_memory", "id": "missing"})["ok"])

    def test_worktree_checkouts_share_one_workspace_hub_and_filter(self):
        head = self.messages(1, session="head", workspace="/p/cobalt/@head")
        tree = self.messages(1, session="tree", workspace="/p/cobalt-wt/fix")
        other = self.messages(1, session="other", workspace="/p/fjord")
        for identifier, message in (("h", head[0]), ("t", tree[0]), ("o", other[0])):
            self.request("upsert_memory", id=identifier, title=identifier, body=identifier + ".",
                         sources=[self.source(message)])
        graph = self.request("memory_graph", edges=["workspace"])
        hubs = {node["id"]: node["label"] for node in graph["nodes"] if node["kind"] == "workspace"}
        self.assertEqual(hubs, {"workspace:/p/cobalt": "cobalt", "workspace:/p/fjord": "fjord"})
        linked = {edge["source"] for edge in graph["edges"] if edge["target"] == "workspace:/p/cobalt"}
        self.assertEqual(linked, {"h", "t"})
        filtered = self.request("memory_graph", workspace="/p/cobalt")
        self.assertEqual({node["id"] for node in filtered["nodes"] if node["kind"] == "memory"}, {"h", "t"})

    def test_graph_connects_sessions_tags_lineage_and_workspaces(self):
        messages = self.messages(3)
        other = self.messages(1, session="other", workspace="/elsewhere")
        self.request("upsert_memory", id="a", title="A", body="A.", sources=[self.source(messages[0])], tags=["build", "x"])
        self.request("upsert_memory", id="b", title="B", body="B.", sources=[self.source(messages[1])], tags=["build"])
        self.request("upsert_memory", id="c", title="C", body="C.", sources=[self.source(other[0])], lineage=["a"])
        graph = self.request("memory_graph", edges=["session", "tag", "lineage", "workspace"], mindmap=True)
        kinds = {(edge["source"], edge["target"], edge["kind"]) for edge in graph["edges"]}
        self.assertIn(("a", "b", "session"), kinds)
        self.assertIn(("a", "b", "tag"), kinds)
        self.assertIn(("a", "c", "lineage"), kinds)
        self.assertIn(("a", "workspace:/workspace", "workspace"), kinds)
        tag = next(edge for edge in graph["edges"] if edge["kind"] == "tag")
        self.assertEqual((tag["strength"], tag["label"]), (0.5, "build"))
        self.assertEqual({node["kind"] for node in graph["nodes"]}, {"memory", "workspace"})
        self.assertEqual(graph["mindmap"]["children"][0]["label"], "declarative")
        self.assertEqual(graph, self.request("memory_graph", edges=["session", "tag", "lineage", "workspace"], mindmap=True))
        capped = self.request("memory_graph", limit=1, focus="c")
        self.assertEqual([node["id"] for node in capped["nodes"]], ["c"])
        self.assertTrue(capped["truncated"])
        self.assertFalse(capped["similarity_available"])
        self.assertFalse(self.store.request({"op": "memory_graph", "edges": ["bogus"]})["ok"])

    def test_graph_links_memories_through_shared_concepts_and_maps_topics(self):
        messages = self.messages(3)
        self.request("upsert_memory", id="a", title="Index lock", sources=[self.source(messages[0])],
                     body="Opening /workspace/helpers/omni_kb.py:40 failed with ENOENT.")
        self.request("upsert_memory", id="b", title="KB reader", sources=[self.source(messages[1])],
                     body="`omni_kb.py` raises ENOENT when the folder is gone.")
        self.request("upsert_memory", id="c", title="Unrelated", sources=[self.source(messages[2])],
                     body="Only `lonely_symbol` lives here.")
        module = self.store.hippocampus_module
        with mock.patch.object(module.CONCEPTS, "extract", wraps=module.CONCEPTS.extract) as extract:
            graph = self.request("memory_graph", edges=["concept"], mindmap=True)
            hubs = {node["id"]: node for node in graph["nodes"] if node["kind"] == "concept"}
            self.assertEqual(set(hubs), {"concept:file:helpers/omni_kb.py", "concept:error:ENOENT"})
            self.assertEqual(hubs["concept:error:ENOENT"]["label"], "ENOENT")
            edges = {(edge["source"], edge["target"], edge["kind"]) for edge in graph["edges"]}
            self.assertEqual(edges, {(memory, hub, "concept") for memory in "ab" for hub in hubs})
            topics = next(branch for branch in graph["mindmap"]["children"] if branch["id"] == "topics")
            self.assertEqual(topics["children"][0]["label"], "ENOENT, helpers/omni_kb.py")
            self.assertEqual([leaf["memory_id"] for leaf in topics["children"][0]["children"]], ["a", "b"])
            self.assertEqual(topics["children"][0]["children"][0]["id"], "topic:1:a")
            self.assertEqual(graph, self.request("memory_graph", edges=["concept"], mindmap=True))
            self.assertEqual(extract.call_count, 3, "Concepts are extracted once per memory revision")
            self.request("upsert_memory", id="c", title="Unrelated", sources=[self.source(messages[2])],
                         body="Now it hits ENOENT too.")
            graph = self.request("memory_graph", edges=["concept"])
            self.assertEqual(extract.call_count, 4, "Only the revised memory is extracted again")
            self.store.hippocampus._memory_cache = None
            self.request("memory_graph", edges=["concept"])
            self.assertEqual(extract.call_count, 4, "A full rebuild reuses unchanged memories' concepts")
        members = {edge["source"] for edge in graph["edges"] if edge["target"] == "concept:error:ENOENT"}
        self.assertEqual(members, {"a", "b", "c"})
        self.assertNotIn("concept", {edge["kind"] for edge in self.request("memory_graph")["edges"]},
                         "Concept hubs are asked for, not a default")

    def test_stable_topics_are_named_and_shown_in_graph_mindmap_and_digest(self):
        messages = self.messages(4)
        for identifier, body in (("k1", "Fix helpers/omni_kb.py when SQLITE_BUSY appears."),
                                 ("k2", "Retry in helpers/omni_kb.py on SQLITE_BUSY."),
                                 ("k3", "helpers/omni_kb.py walks knowledge folders; SQLITE_BUSY is retried.")):
            self.request("upsert_memory", id=identifier, title="KB " + identifier, body=body,
                         sources=[self.source(messages[0])])
        offered = self.request("memory_topics")
        self.assertEqual(len(offered["topics"]), 1)
        topic = offered["topics"][0]
        self.assertTrue(topic["id"].startswith("topic:"))
        self.assertEqual(topic["size"], 3)
        self.assertEqual(len(topic["examples"]), 3)
        named = self.request("name_topic", id=topic["id"], name="Knowledge base ingest",
                             summary="How KB folders are read.", run_id="run-9", agent="Omni Memory")
        self.assertEqual(named["topic"]["name"], "Knowledge base ingest")
        self.assertEqual(self.request("memory_topics")["topics"], [], "a named, unchanged topic is not offered again")
        graph = self.request("memory_graph", mindmap=True)
        communities = {node.get("community", {}).get("label") for node in graph["nodes"] if node["kind"] == "memory"}
        self.assertEqual(communities, {"Knowledge base ingest"})
        topics_branch = [child for child in graph["mindmap"]["children"] if child["id"] == "topics"][0]
        self.assertEqual(topics_branch["children"][0]["id"], topic["id"])
        self.assertEqual(topics_branch["children"][0]["label"], "Knowledge base ingest")
        digest = self.request("memory_digest", regenerate=True)["digest"]["markdown"]
        self.assertIn("**Knowledge base ingest** — 3 memories", digest)
        # A recompute keeps the id and the name while membership overlaps.
        self.request("upsert_memory", id="k4", title="KB k4", body="helpers/omni_kb.py and SQLITE_BUSY again.",
                     sources=[self.source(messages[1])])
        recomputed = self.store.hippocampus.stable_topics(refresh=True)
        self.assertEqual([(item["id"], item["name"]) for item in recomputed], [(topic["id"], "Knowledge base ingest")])
        self.assertEqual(self.request("memory_events", kind="topic_named")["events"][0]["actor"],
                         {"run_id": "run-9", "agent": "Omni Memory"})
        with self.assertRaises(AssertionError):
            self.request("name_topic", id="topic:missing", name="X")

    def test_memories_outside_stable_topics_keep_their_graph_community(self):
        messages = self.messages(2)
        for identifier in ("k1", "k2", "k3"):
            self.request("upsert_memory", id=identifier, title="KB " + identifier,
                         body="helpers/omni_kb.py retries SQLITE_BUSY.", sources=[self.source(messages[0])])
        topic = self.request("memory_topics")["topics"][0]
        self.request("name_topic", id=topic["id"], name="Knowledge base ingest")
        # Added after the day's topics were computed: in no stable topic yet.
        for identifier in ("d1", "d2", "d3"):
            self.request("upsert_memory", id=identifier, title="Docker " + identifier,
                         body="docker compose fails with EADDRINUSE on port 5432.", sources=[self.source(messages[1])])
        nodes = {node["id"]: node.get("community", {}) for node in self.request("memory_graph")["nodes"]
                 if node["kind"] == "memory"}
        self.assertEqual({nodes[key]["label"] for key in ("k1", "k2", "k3")}, {"Knowledge base ingest"})
        docker = {nodes[key].get("id") for key in ("d1", "d2", "d3")}
        self.assertEqual(len(docker), 1)
        self.assertNotIn(None, docker)
        self.assertNotEqual(docker, {topic["id"]})

    def test_topic_list_stays_within_its_response_budget(self):
        messages = self.messages(2)
        with mock.patch.object(self.store.hippocampus_module, "TOPIC_RESPONSE_BYTES", 300):
            for identifier in ("k1", "k2", "k3"):
                self.request("upsert_memory", id=identifier, title="KB " + identifier,
                             body="helpers/omni_kb.py retries SQLITE_BUSY. " + "long " * 200,
                             sources=[self.source(messages[0])])
            for identifier in ("d1", "d2", "d3"):
                self.request("upsert_memory", id=identifier, title="Docker " + identifier,
                             body="docker compose fails with EADDRINUSE on port 5432. " + "long " * 200,
                             sources=[self.source(messages[1])])
            offered = self.request("memory_topics")
        self.assertEqual(len(offered["topics"]), 1, "one topic always fits; the rest wait")
        self.assertEqual(offered["remaining"], 1)
        self.assertTrue(all(len(example["excerpt"]) <= 120 for example in offered["topics"][0]["examples"]))

    def test_graph_choice_near_the_cut_ignores_tiny_weight_drift(self):
        messages = self.messages(1)
        for identifier, weight in (("b", 5.04), ("a", 5.01), ("c", 3.0)):
            self.request("upsert_memory", id=identifier, title=identifier, body=identifier + ".",
                         sources=[self.source(messages[0])], weight=weight)
        chosen = [node["id"] for node in self.request("memory_graph", limit=1)["nodes"] if node["kind"] == "memory"]
        # 5.01 and 5.04 share a half-point step, so the ID decides, not the drift.
        self.assertEqual(chosen, ["a"])

    def test_merge_candidates_and_decisions(self):
        messages = self.messages(4)
        for identifier, message in zip("abcd", messages):
            self.request("upsert_memory", id=identifier, title=identifier.upper(), body=identifier + " fact.",
                         sources=[self.source(message)])
        hippocampus = self.store.hippocampus
        hippocampus._vector_cache = {"a": [1.0, 0.0], "b": [0.99, 0.141], "c": [0.0, 1.0], "d": [0.1, 0.995]}
        hippocampus._vector_cache_at = time.monotonic()
        pairs = self.request("merge_candidates")["pairs"]
        self.assertEqual([[item["id"] for item in pair["memories"]] for pair in pairs], [["c", "d"], ["a", "b"]])
        merged = self.request("decide_merge", keep="a", drop="b", decision="merge", reason="Same fact",
                              title="A and B", body="The combined fact.")
        self.assertEqual(merged["decision"], "merge")
        kept = self.request("get_memory", id="a")["memory"]
        self.assertEqual(kept["title"], "A and B")
        self.assertEqual({item["message_id"] for item in kept["sources"]}, {messages[0]["id"], messages[1]["id"]})
        self.assertTrue(self.request("get_memory", id="b")["memory"]["archived"])
        self.request("decide_merge", keep="d", drop="c", decision="keep", reason="Different facts")
        self.assertFalse(self.request("get_memory", id="c")["memory"]["archived"])
        self.assertEqual(self.request("merge_candidates")["pairs"], [], "decided pairs are never offered twice")
        events = self.request("memory_events", kind="merge_decided")["events"]
        self.assertEqual({event["decision"] for event in events}, {"merge", "keep"})
        with self.assertRaises(AssertionError):
            self.request("decide_merge", keep="a", drop="a", decision="merge", reason="x")
        with self.assertRaises(AssertionError):
            self.request("decide_merge", keep="a", drop="c", decision="fuse", reason="x")

    def test_graph_similarity_uses_cached_vectors_and_pairs(self):
        messages = self.messages(3)
        for identifier, message in zip("abc", messages):
            self.request("upsert_memory", id=identifier, title=identifier.upper(), body=identifier + ".",
                         sources=[self.source(message)])
        hippocampus = self.store.hippocampus
        hippocampus._vector_cache = {"a": [1.0, 0.0], "b": [0.96, 0.28], "c": [0.0, 1.0]}
        hippocampus._vector_cache_at = time.monotonic()
        with mock.patch.object(hippocampus, "_load_memory_vectors", side_effect=AssertionError("reloaded vectors")):
            first = self.request("memory_graph", edges=["similarity"])
            self.assertEqual([(edge["source"], edge["target"]) for edge in first["edges"]], [("a", "b")])
            self.assertAlmostEqual(first["edges"][0]["strength"], 0.96)
            with mock.patch.object(HIPPOCAMPUS.operator, "mul", side_effect=AssertionError("recomputed pairs")):
                again = self.request("memory_graph", edges=["similarity"])
            self.assertEqual(again["edges"], first["edges"])
            self.assertFalse(hippocampus.maybe_refresh_vectors(), "Fresh vectors are not reloaded")

    def test_vector_cache_rereads_only_changed_memories(self):
        hippocampus = self.store.hippocampus
        hippocampus._vector_cache = {"a": [1.0, 0.0], "b": [0.0, 1.0]}
        hippocampus._vector_cache_at = time.monotonic()
        self.store._document("vector:memory:x:0", {"record_id": "b", "kind": "chunk"})
        self.store._document("vector:memory:" + HIPPOCAMPUS.hashlib.sha256(b"a").hexdigest() + ":0", None)
        loads = []

        def load(identifiers, by_record=False):
            loads.append((set(identifiers), by_record))
            return {"b": [0.6, 0.8]}
        with mock.patch.object(hippocampus, "_load_memory_vectors", side_effect=load), \
                mock.patch.object(hippocampus, "refresh_vectors", side_effect=AssertionError("full reload")):
            self.assertFalse(hippocampus.maybe_refresh_vectors())
        self.assertEqual(loads, [({"a", "b"}, True)])
        self.assertEqual(hippocampus._vector_cache, {"b": [0.6, 0.8]})
        self.assertEqual(self.store._memory_vector_changes, set())

    def test_full_vector_reload_is_read_in_slices_across_ticks(self):
        messages = self.messages(1)
        for index in range(120):
            self.request("upsert_memory", id="m%03d" % index, title="M%d" % index, body="b",
                         sources=[self.source(messages[0])])
        hippocampus = self.store.hippocampus
        calls = []

        def load(identifiers, by_record=False):
            calls.append(len(identifiers))
            return {identifier: [1.0, 0.0] for identifier in identifiers}
        with mock.patch.object(hippocampus, "_load_memory_vectors", side_effect=load):
            self.assertTrue(hippocampus.maybe_refresh_vectors(budget_seconds=0))  # starts
            ticks = 0
            while hippocampus.maybe_refresh_vectors(budget_seconds=0):
                ticks += 1
        self.assertEqual(calls, [50, 50, 20])
        self.assertEqual(ticks, 3, "one slice per tick; the third completes the cache")
        self.assertEqual(len(hippocampus._vector_cache), 120)

    def test_graph_reports_similarity_unavailable_without_a_model(self):
        message = self.messages(1)[0]
        self.request("upsert_memory", id="a", title="A", body="A.", sources=[self.source(message)])
        self.assertFalse(self.request("memory_graph")["similarity_available"])

    def test_graph_order_is_by_id_and_survives_weight_drift(self):
        messages = self.messages(3)
        for identifier, weight in (("c", 9.0), ("a", 3.0), ("b", 6.0)):
            self.request("upsert_memory", id=identifier, title=identifier.upper(), body=identifier + ".",
                         sources=[self.source(messages[0])], tags=["build"], weight=weight)
        arguments = {"edges": ["session", "tag", "workspace"]}
        first = self.store.hippocampus.graph(dict(arguments))
        drifted = self.store.hippocampus.graph(dict(arguments), now=time.time() + 3 * DAY)
        ids = [node["id"] for node in first["nodes"]]
        self.assertEqual(ids, ["a", "b", "c", "workspace:/workspace"], "memories by id, then hubs")
        self.assertEqual(ids, [node["id"] for node in drifted["nodes"]])
        self.assertNotEqual([node["effective_weight"] for node in first["nodes"][:3]],
                            [node["effective_weight"] for node in drifted["nodes"][:3]])
        self.assertEqual(first["edges"], drifted["edges"])
        keys = [(edge["kind"], edge["source"], edge["target"]) for edge in first["edges"]]
        self.assertEqual(keys, sorted(keys))

    def test_graph_focus_returns_the_ego_network_within_hops_and_cap(self):
        messages = self.messages(4)
        # A chain a–b–c–d by lineage; e stands alone.
        self.request("upsert_memory", id="a", title="A", body="A.", sources=[self.source(messages[0])])
        for identifier, parent, message in (("b", "a", 1), ("c", "b", 2), ("d", "c", 3)):
            self.request("upsert_memory", id=identifier, title=identifier.upper(), body=identifier + ".",
                         sources=[self.source(messages[message])], lineage=[parent])
        self.request("upsert_memory", id="e", title="E", body="E.", sources=[self.source(messages[3])])
        one = self.request("memory_graph", edges=["lineage"], focus="b", hops=1)
        self.assertEqual([node["id"] for node in one["nodes"]], ["a", "b", "c"])
        self.assertEqual((one["focus"], one["hops"]), ("b", 1))
        self.assertEqual({(edge["source"], edge["target"]) for edge in one["edges"]}, {("a", "b"), ("b", "c")})
        two = self.request("memory_graph", edges=["lineage"], focus="b", hops=2)
        self.assertEqual([node["id"] for node in two["nodes"]], ["a", "b", "c", "d"])
        capped = self.request("memory_graph", edges=["lineage"], focus="b", hops=2, limit=2)
        self.assertEqual(len(capped["nodes"]), 2)
        self.assertIn("b", [node["id"] for node in capped["nodes"]])
        self.assertTrue(capped["truncated"])
        self.assertEqual(self.request("memory_graph", edges=["lineage"], focus="missing", hops=1)["nodes"], [])
        self.assertFalse(self.store.request({"op": "memory_graph", "hops": 1})["ok"], "hops needs focus")
        self.assertFalse(self.store.request({"op": "memory_graph", "focus": "b", "hops": 3})["ok"])

    def test_ego_network_does_not_walk_through_hubs(self):
        nodes = [{"id": "a", "kind": "memory"}, {"id": "b", "kind": "memory"}, {"id": "w", "kind": "workspace"}]
        edges = [{"source": "a", "target": "w", "kind": "workspace", "strength": 0.5},
                 {"source": "b", "target": "w", "kind": "workspace", "strength": 0.5}]
        kept, links = HIPPOCAMPUS.Hippocampus._ego_network(nodes, edges, "a", 2, 120)
        self.assertEqual([node["id"] for node in kept], ["a", "w"])
        self.assertEqual(len(links), 1)

    def test_graph_filters_by_workspace_time_and_weight(self):
        here = self.messages(1)
        there = self.messages(1, session="other", workspace="/elsewhere")
        self.request("upsert_memory", id="a", title="A", body="A.", sources=[self.source(here[0])], weight=9.0)
        self.request("upsert_memory", id="b", title="B", body="B.", sources=[self.source(there[0])], weight=2.0)

        def ids(**arguments):
            return [node["id"] for node in self.request("memory_graph", edges=["lineage"], **arguments)["nodes"]]
        self.assertEqual(ids(workspace="/workspace"), ["a"])
        self.assertEqual(ids(workspace="/elsewhere"), ["b"])
        self.assertEqual(ids(min_weight=5), ["a"])
        self.assertEqual(ids(after=time.time() - 3600), ["a", "b"])
        self.assertEqual(ids(after=time.time() + 3600), [])
        for bad in ({"workspace": ""}, {"after": -1}, {"after": True}, {"min_weight": 11}, {"min_weight": "5"}):
            self.assertFalse(self.store.request(dict({"op": "memory_graph"}, **bad))["ok"], bad)

    def test_graph_nodes_name_their_community_and_mindmap_counts_children(self):
        messages = self.messages(3)
        for identifier, message in zip("ab", messages):
            self.request("upsert_memory", id=identifier, title="Index " + identifier, sources=[self.source(message)],
                         body="Opening /workspace/helpers/omni_kb.py:40 failed with ENOENT.")
        self.request("upsert_memory", id="c", title="Unrelated", sources=[self.source(messages[2])],
                     body="Only `lonely_symbol` lives here.")
        graph = self.request("memory_graph", edges=["lineage"], mindmap=True)
        communities = {node["id"]: node.get("community") for node in graph["nodes"]}
        self.assertEqual(communities["a"], {"id": "community:a", "label": "ENOENT, helpers/omni_kb.py"})
        self.assertEqual(communities["b"], communities["a"])
        self.assertIsNone(communities["c"])
        root = graph["mindmap"]
        self.assertEqual(root["children_count"], len(root["children"]))
        topics = next(branch for branch in root["children"] if branch["id"] == "topics")
        self.assertEqual(topics["children"][0]["children_count"], 2)
        self.assertNotIn("children_count", topics["children"][0]["children"][0], "a leaf has no children")

    def test_digest_regenerates_and_detects_staleness(self):
        messages = self.messages(2)
        self.request("upsert_memory", id="a", title="Alpha", body="First. Second.", sources=[self.source(messages[0])])
        digest = self.request("memory_digest", workspace="/workspace")["digest"]
        self.assertIn("**Alpha** — First.", digest["markdown"])
        self.assertEqual(digest["memory_ids"], ["a"])
        self.assertFalse(self.request("memory_digest", workspace="/workspace")["digest"]["stale"])
        self.request("upsert_memory", id="b", title="Beta", body="B.", sources=[self.source(messages[1])])
        self.assertTrue(self.request("memory_digest", workspace="/workspace")["digest"]["stale"])
        fresh = self.request("memory_digest", workspace="/workspace", regenerate=True)["digest"]
        self.assertEqual(sorted(fresh["memory_ids"]), ["a", "b"])
        self.assertEqual(self.request("memory_events", kind="digest_regenerated")["events"][0]["count"], 2)
        self.assertEqual(self.request("memory_digest")["digest"]["workspace"], None)

    def test_memory_tick_snapshots_ingestion_at_most_every_ten_minutes(self):
        self.messages(1)
        # The first tick only builds the memory cache, so a launch never stacks
        # that cold build and the corpus counts in one worker call.
        self.assertFalse(self.request("memory_tick")["snapshot"])
        self.assertTrue(self.request("memory_tick")["snapshot"])
        self.assertFalse(self.request("memory_tick")["snapshot"])
        snapshot = self.request("memory_events", kind="ingest_snapshot")["events"][0]
        self.assertEqual(snapshot["messages"], 1)
        with mock.patch.object(time, "time", return_value=time.time() + 601):
            self.assertTrue(self.request("memory_tick")["snapshot"])

    def test_digest_reflex_regenerates_a_stale_digest_at_most_hourly(self):
        hippocampus = self.store.hippocampus
        self.assertIsNone(hippocampus.maybe_refresh_digest(now=1000.0), "Nothing to digest yet")
        messages = self.messages(2)
        self.request("upsert_memory", id="a", title="Alpha", body="A.", sources=[self.source(messages[0])])
        first = hippocampus.maybe_refresh_digest(now=time.time())
        self.assertEqual(first["memory_ids"], ["a"])
        self.assertIsNone(hippocampus.maybe_refresh_digest(now=time.time() + 60), "Not again within the hour")
        self.request("upsert_memory", id="b", title="Beta", body="B.", sources=[self.source(messages[1])])
        self.assertIsNone(hippocampus.maybe_refresh_digest(now=time.time() + 600))
        later = hippocampus.maybe_refresh_digest(now=time.time() + 3700)
        self.assertEqual(sorted(later["memory_ids"]), ["a", "b"])
        self.assertIsNone(hippocampus.maybe_refresh_digest(now=time.time() + 7500), "An unchanged digest stays")
        self.assertEqual(len(self.request("memory_events", kind="digest_regenerated")["events"]), 2)

    def test_overview_counts_types_tags_and_expiry(self):
        message = self.messages(1)[0]
        self.request("upsert_memory", id="a", title="A", body="A.", sources=[self.source(message)],
                     memory_type="prospective", tags=["later"], weight=0.2)
        self.request("upsert_memory", id="b", title="B", body="B.", sources=[self.source(message)], tags=["later"])
        overview = self.request("memory_overview")["overview"]
        self.assertEqual(overview["memories"], 2)
        self.assertEqual(overview["types"]["prospective"]["count"], 1)
        self.assertEqual(overview["expiry_candidates"], 1)
        self.assertEqual(overview["tags"], [{"tag": "later", "count": 2}])
        self.assertEqual(overview["events_24h"]["memory_created"], 2)

    def test_pending_candidate_count_is_cached_until_intake_changes(self):
        messages = self.messages(3)
        hippocampus = self.store.hippocampus
        self.assertEqual(self.count_pending(1000.0), 3)
        self.write(self.claude / "workspace/more.jsonl", [self.claude_message("Another durable conclusion. " * 3, "assistant")])
        self.complete_scan()
        self.assertEqual(self.count_pending(1030.0), 3)
        self.assertEqual(self.count_pending(1601.0), 4)
        self.request("dismiss_memory_candidate", message_id=messages[0]["id"], reason="Status")
        self.assertEqual(self.count_pending(1602.0), 3)

    def test_overview_reuses_corpus_counts_and_the_tick_warms_them(self):
        self.messages(2)
        hippocampus = self.store.hippocampus
        calls = []
        original = self.store.quick_status
        self.store.quick_status = lambda: calls.append(1) or original()
        try:
            self.warm_counts(now=1000.0)
            cache, warm = HIPPOCAMPUS.STATUS_CACHE_SECONDS, HIPPOCAMPUS.STATUS_WARM_SECONDS
            self.assertEqual(hippocampus.overview(now=1000.0)["overview"]["pipeline"]["messages"], 2)
            hippocampus.overview(now=1000.0 + cache - 1)
            self.assertEqual(len(calls), 1, "Message counts are reused until the cache expires")
            self.assertIsNone(hippocampus.maybe_warm_status(now=1000.0 + 100), "Fresh counts are not recomputed")
            self.assertIsNotNone(hippocampus.maybe_warm_status(now=1000.0 + warm + 10), "The background tick refreshes them first")
            self.assertEqual(len(calls), 2)
            overview = hippocampus.overview(now=1000.0 + cache + 100)["overview"]
            self.assertEqual(len(calls), 2, "The page reads the warmed counts")
            self.assertEqual(overview["pipeline"]["status_checked_at"], 1000.0 + warm + 10)
            counted = hippocampus._pending_cache[0]
            self.assertGreater(counted + HIPPOCAMPUS.PENDING_CACHE_SECONDS, 1000.0 + warm + 10, "The pending count is fresh")
            hippocampus.maybe_warm_status(now=counted + 545)
            self.assertEqual(hippocampus._pending_cache[0], counted + 545,
                             "The tick refreshes the pending count before it expires")
            self.assertEqual(len(calls), 2, "without recounting the corpus")
        finally:
            self.store.quick_status = original

    def warm_counts(self, now=None):
        """An in-memory store counts on demand; the native store's tick warms counts first."""

    def count_pending(self, now):
        return self.store.hippocampus.pending_candidates(now=now)

    def test_memories_store_their_workspaces_and_older_ones_are_backfilled(self):
        messages = self.messages(3)
        self.request("upsert_memory", id="a", title="Alpha", body="A.",
                     sources=[self.source(message) for message in messages])
        hippocampus = self.store.hippocampus
        stored = self.store._document_value("memory:a")
        expected = hippocampus._workspaces(self.store._memory(self.store.connection.execute(
            "SELECT * FROM memories WHERE id='a'").fetchone())["sources"])
        self.assertEqual(stored["workspaces"], expected)
        # A memory written before workspaces were stored gets them in the background.
        self.store._document("memory:a", {key: value for key, value in stored.items() if key != "workspaces"})
        hippocampus._memory_cache = None
        listed = self.request("list_memories")["memories"][0]
        self.assertEqual(listed["workspaces"], expected)
        self.assertNotIn("available", listed["sources"][0], "Lists leave availability to the detail view")
        self.assertEqual(hippocampus._workspace_backfill, {"a"})
        self.assertEqual(hippocampus.backfill_workspaces(), 1)
        self.assertEqual(self.store._document_value("memory:a")["workspaces"], expected)
        self.assertTrue(self.request("get_memory", id="a")["memory"]["sources"][0]["available"])

    def test_delete_is_journaled(self):
        message = self.messages(1)[0]
        self.request("upsert_memory", id="a", title="Alpha", body="A.", sources=[self.source(message)])
        self.request("delete_memory", id="a")
        deleted = self.request("memory_events", kind="memory_deleted")["events"][0]
        self.assertEqual((deleted["memory_id"], deleted["title"]), ("a", "Alpha"))


class NativeHippocampusTests(HippocampusStoreTests):
    """The same behaviour when the native app persists the writes."""

    def setUp(self):
        HippocampusStoreTests.setUp(self)
        self.store.close()
        self._native_path = self.home / "knowledge.sqlite"
        self.writer = test_knowledge.sqlite3.connect(self._native_path)
        self.addCleanup(self.writer.close)
        self.writer.execute("PRAGMA journal_mode=WAL")
        self.writer.execute("CREATE TABLE records(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        self.writer.executescript((ROOT / "helpers/omni_knowledge_schema.sql").read_text())
        self.store = test_knowledge.KNOWLEDGE.KnowledgeStore(roots=self.roots, home=self.home, semantics=False,
                                                             read_index=self._native_path)
        self.addCleanup(self.store.close)

    acknowledge_all = vars(test_knowledge.NativeProjectionTests)["acknowledge_all"]

    def warm_counts(self, now=None):
        # The Memory page never counts the native index itself (see
        # test_candidates_readthrough); the background tick does it first.
        self.store.hippocampus.maybe_warm_status(now=now)

    def count_pending(self, now):
        # On the native index a request shows the last count; the background
        # tick walks the corpus in windows when the count is stale.
        hippocampus = self.store.hippocampus
        cached = hippocampus._pending_cache
        if cached is None or now - cached[0] >= HIPPOCAMPUS.PENDING_CACHE_SECONDS:
            while not hippocampus.step_pending(now, budget_seconds=0):
                pass
        return hippocampus.pending_candidates(now=now)

    def test_attributes_survive_native_persistence_and_restart(self):
        message = self.messages(1)[0]
        self.request("upsert_memory", id="a", title="Alpha", body="A.", sources=[self.source(message)],
                     memory_type="autobiographical", pinned=True)
        self.acknowledge_all()
        self.store.close()
        restored = test_knowledge.KNOWLEDGE.KnowledgeStore(roots=self.roots, home=self.home, semantics=False,
                                                           read_index=self._native_path)
        self.addCleanup(restored.close)
        memory = restored.request({"op": "get_memory", "id": "a"})["memory"]
        self.assertEqual((memory["memory_type"], memory["pinned"], memory["weight"]), ("autobiographical", True, 8.0))
        self.assertEqual(restored.request({"op": "memory_events"})["events"][0]["kind"], "memory_created")


if __name__ == "__main__":
    unittest.main()
