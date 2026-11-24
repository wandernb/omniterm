import importlib.util
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
_FIXTURES = importlib.util.spec_from_file_location("test_knowledge_for_explain", Path(__file__).with_name("test_knowledge.py"))
test_knowledge = importlib.util.module_from_spec(_FIXTURES)
_FIXTURES.loader.exec_module(test_knowledge)
_SPEC = importlib.util.spec_from_file_location("omni_explain_for_tests", ROOT / "helpers/omni_explain.py")
EXPLAIN = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(EXPLAIN)
_HIPPOCAMPUS_SPEC = importlib.util.spec_from_file_location("omni_hippocampus_for_explain_tests",
                                                          ROOT / "helpers/omni_hippocampus.py")
HIPPOCAMPUS = importlib.util.module_from_spec(_HIPPOCAMPUS_SPEC)
_HIPPOCAMPUS_SPEC.loader.exec_module(HIPPOCAMPUS)
DAY = 86400.0
_FIXTURE = vars(test_knowledge.KnowledgeTests)


def memory(**overrides):
    value = {"id": "m", "memory_type": "prospective", "base_weight": 9.0, "decay_rate": 0.1, "weight": 9.0,
             "effective_weight": 8.0, "pinned": False, "last_reinforced_at": 0.0, "expiry_candidate": False,
             "type_description": "Intentions and follow-ups", "sources": [{}], "archived": False,
             "archived_at": None, "last_used": None, "created": 0.0, "updated": 0.0}
    value.update(overrides)
    return value


class WeightStoryTests(unittest.TestCase):
    def test_expiry_is_projected_from_the_last_reinforcement(self):
        value = memory()
        archive = EXPLAIN.archive_story(value, HIPPOCAMPUS, 10 * DAY)
        story = EXPLAIN.weight_story(value, [], archive, 10 * DAY)
        # 9.0 decays 0.1 a day to the 0.5 threshold in 85 days.
        self.assertAlmostEqual(story["expires_at"], 85 * DAY)
        self.assertIn("Reaches the 0.5 expiry threshold in 75 days unless reinforced.", story["reasons"])
        self.assertEqual(story["points"][-1], {"at": 10 * DAY, "weight": 8.0, "kind": "now"})

    def test_points_come_from_creation_revisions_and_attribute_changes(self):
        events = [{"kind": "memory_created", "at": 1.0, "weight": 9.0},
                  {"kind": "memory_revised", "at": 2.0, "weight": 9.5},
                  {"kind": "memory_attributes", "at": 3.0, "changes": {"weight": [9.5, 4.0]}},
                  {"kind": "memory_attributes", "at": 4.0, "changes": {"pinned": [False, True]}}]
        story = EXPLAIN.weight_story(memory(), events, EXPLAIN.archive_story(memory(), HIPPOCAMPUS, 5.0), 5.0)
        self.assertEqual([point["weight"] for point in story["points"]], [9.0, 9.5, 4.0, 8.0])
        self.assertIn("Reinforced 2 times by revisions, new evidence or by hand.", story["reasons"])

    def test_pinned_and_well_sourced_memories_say_why_they_stay(self):
        pinned = memory(pinned=True)
        story = EXPLAIN.weight_story(pinned, [], EXPLAIN.archive_story(pinned, HIPPOCAMPUS, DAY), DAY)
        self.assertIsNone(story["expires_at"])
        self.assertIn("Pinned, so it does not decay.", story["reasons"])
        self.assertIn("Never archived: pinned.", story["reasons"])
        sourced = memory(sources=[{}, {}, {}])
        self.assertIn("3 sources", EXPLAIN.archive_story(sourced, HIPPOCAMPUS, DAY)["exempt"])

    def test_a_fading_unused_memory_reports_the_archival_step_due(self):
        fading = memory(effective_weight=0.2, expiry_candidate=True, created=DAY, updated=DAY)
        archive = EXPLAIN.archive_story(fading, HIPPOCAMPUS, 41 * DAY)
        self.assertEqual(archive["next_action"], "archive")
        self.assertEqual(archive["unused_days"], 40.0)
        story = EXPLAIN.weight_story(fading, [], archive, 41 * DAY)
        self.assertIn("Below the 0.5 expiry threshold: an expiry candidate.", story["reasons"])


class ExplainStoreTests(unittest.TestCase):
    setUp = _FIXTURE['setUp']
    write = _FIXTURE['write']
    claude_message = _FIXTURE['claude_message']
    complete_scan = _FIXTURE['complete_scan']
    request = _FIXTURE['request']

    def messages(self, count, session):
        self.write(self.claude / "workspace" / (session + ".jsonl"),
                   [self.claude_message("Durable conclusion number %d about builds. " % index * 3, "assistant")
                    for index in range(count)])
        self.complete_scan()
        return self.request("get_session", session_id="claude:" + session)["messages"]

    @staticmethod
    def source(message):
        return {"session_id": message["session_id"], "message_id": message["id"]}

    def test_explanation_gathers_weight_sources_formation_lineage_and_links(self):
        first = self.messages(3, "alpha")
        second = self.messages(2, "beta")
        self.request("upsert_memory", id="old", title="Old signing rule", body="Sign products.",
                     sources=[self.source(first[0])], tags=["build"], agent="Omni Memory", run_id="run-1",
                     reason="Seen in a build")
        self.request("upsert_memory", id="sign", title="Signing", body="Sign native products.", memory_type="procedural",
                     sources=[self.source(first[1]), self.source(second[0])], tags=["build", "signing"],
                     lineage=["old"], agent="Omni Memory", run_id="run-2", reason="Verified twice")
        self.request("upsert_memory", id="sign", title="Signing", body="Sign and verify native products.",
                     sources=[self.source(first[1]), self.source(second[0]), self.source(second[1])],
                     tags=["build", "signing"])
        before = self.store.hippocampus._stored("sign").get("last_used")
        explanation = self.request("explain_memory", id="sign")["explanation"]
        self.assertEqual(self.store.hippocampus._stored("sign").get("last_used"), before,
                         "explaining a memory is not a use")
        self.assertEqual(explanation["memory_type"], "procedural")
        weight = explanation["weight"]
        self.assertEqual([point["kind"] for point in weight["points"]], ["memory_created", "memory_revised", "now"])
        self.assertGreater(weight["points"][1]["weight"], weight["points"][0]["weight"])
        self.assertEqual(explanation["sources"]["session_count"], 2)
        self.assertEqual(explanation["sources"]["evidence_count"], 3)
        self.assertEqual(explanation["sources"]["sessions"][0]["session_id"], "claude:beta")
        self.assertEqual(explanation["sources"]["sessions"][0]["roles"], ["assistant"])
        self.assertEqual(explanation["sources"]["missing"], 0)
        self.assertEqual(explanation["formation"]["created_by"], {"run_id": "run-2", "agent": "Omni Memory"})
        self.assertEqual(explanation["formation"]["created_reason"], "Verified twice")
        self.assertEqual(explanation["formation"]["counts"], {"memory_revised": 1, "memory_created": 1})
        self.assertEqual(explanation["lineage"]["ancestors"], [{"id": "old", "title": "Old signing rule", "exists": True}])
        old = self.request("explain_memory", id="old")["explanation"]
        self.assertEqual(old["lineage"]["descendants"], [{"id": "sign", "title": "Signing", "archived": False}])
        reasons = {(link["id"], link["kind"]): link["reason"] for link in explanation["links"]["items"]}
        self.assertEqual(reasons[("old", "lineage")], "UPDATE lineage")
        self.assertEqual(reasons[("old", "tag")], "Shares tags: build")
        self.assertIn(("old", "session"), reasons)
        self.assertIn("Never archived: 3 sources", " ".join(explanation["weight"]["reasons"]))
        self.assertTrue(explanation["why"].startswith("Procedural memory from 3 evidence messages in 2 sessions"))
        self.assertIn("written by Omni Memory", explanation["why"])
        self.assertEqual(explanation["cited_by_summaries"], [])

    def test_unknown_and_invalid_ids_are_refused(self):
        missing = self.store.request({"op": "explain_memory", "id": "nope"})
        self.assertEqual((missing["ok"], missing["code"]), (False, "not_found"))
        invalid = self.store.request({"op": "explain_memory", "id": ""})
        self.assertEqual((invalid["ok"], invalid["code"]), (False, "invalid_request"))


if __name__ == "__main__":
    unittest.main()
