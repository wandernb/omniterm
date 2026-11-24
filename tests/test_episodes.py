import importlib.util
import json
import tempfile
import time
import unittest
from pathlib import Path


_SPEC = importlib.util.spec_from_file_location(
    "omni_knowledge_episode_tests", Path(__file__).parents[1] / "helpers/omni_knowledge.py")
KNOWLEDGE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(KNOWLEDGE)
_EPISODES_SPEC = importlib.util.spec_from_file_location(
    "omni_episodes_tests", Path(__file__).parents[1] / "helpers/omni_episodes.py")
EPISODES = importlib.util.module_from_spec(_EPISODES_SPEC)
_EPISODES_SPEC.loader.exec_module(EPISODES)


class SessionEpisodeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name).resolve()
        self.claude = self.home / ".claude/projects"
        self.store = KNOWLEDGE.KnowledgeStore(roots=[("claude", self.claude)], home=self.home, semantics=False,
                                              kb_roots=[])
        self.addCleanup(self.store.close)
        self.now = time.time()
        self.stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.now - 3600))

    def message(self, role, text, cwd):
        return {"type": role, "timestamp": self.stamp, "cwd": cwd,
                "message": {"role": role, "content": [{"type": "text", "text": text}]}}

    def session(self, name, cwd, turns, folder="workspace"):
        path = self.claude / folder / (name + ".jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(self.message(role, text, str(cwd))) + "\n" for role, text in turns))
        return path

    def index(self):
        for _ in range(100):
            if self.store.scan()["scan_complete"]:
                break
        self.assertTrue(self.store.request({"op": "memory_tick"})["ok"])

    def episodes(self):
        return self.store.connection.execute("SELECT * FROM memories WHERE id LIKE 'episode-%' ORDER BY title").fetchall()

    def test_request_text_prefers_command_and_arguments_and_skips_markup(self):
        self.assertEqual(EPISODES.request_text("<command-message>x</command-message><command-name>/kit:goal</command-name>"
                                               "<command-args>make memory fast</command-args>"),
                         "/kit:goal make memory fast")
        self.assertIsNone(EPISODES.request_text("<system-reminder>noise</system-reminder>"))
        self.assertIsNone(EPISODES.request_text("ok"))
        self.assertEqual(EPISODES.project_of("/p/Projects/basalt/core/@head", Path("/p/Projects")), "basalt")
        self.assertIsNone(EPISODES.project_of("/tmp/x", Path("/p/Projects")))

    def test_one_episode_per_project_and_day_with_linked_evidence(self):
        lumen = self.home / "Projects/lumen"
        self.session("a", lumen, [("user", "<system-reminder>ignore</system-reminder>"),
                                   ("user", "Deploy the API to staging please"),
                                   ("assistant", "Working on it"), ("assistant", "Deployed to staging; smoke tests green.")])
        self.session("b", lumen / "repositories/lumen-os/@head",
                     [("user", "Fix the flaky login test in lumen-os"), ("assistant", "Fixed: race in setup.")])
        self.session("c", self.home / "Projects/basalt/core/@head",
                     [("user", "Audit the dataroom permissions"), ("assistant", "Two leaks found and closed.")])
        self.session("d", self.home / "elsewhere", [("user", "Unrelated scratch work here"), ("assistant", "Done.")])
        self.index()
        memories = self.episodes()
        self.assertEqual(len(memories), 2)
        basalt, lumen_memory = memories
        self.assertTrue(lumen_memory["title"].startswith("lumen · "), lumen_memory["title"])
        self.assertTrue(lumen_memory["title"].endswith("— 2 sessions"))
        self.assertIn("Deploy the API to staging please → Deployed to staging; smoke tests green.", lumen_memory["body"])
        self.assertIn("Fix the flaky login test", lumen_memory["body"])
        self.assertNotIn("ignore", lumen_memory["body"])
        self.assertEqual(json.loads(lumen_memory["tags"]), ["episode", "lumen"])
        self.assertIn("basalt", basalt["title"])
        found = self.store.request({"op": "get_memory", "id": lumen_memory["id"]})
        self.assertTrue(found["ok"], found)
        self.assertEqual(len(found["memory"]["sources"]), 4)
        self.assertTrue(all(item["available"] for item in found["memory"]["sources"]))
        stored = self.store._document_value("memory:" + lumen_memory["id"])
        self.assertEqual(self.store.hippocampus_module.attributes(stored)["memory_type"], "episodic")

    def test_unchanged_bucket_is_not_rewritten_and_consolidator_sessions_are_excluded(self):
        self.session("a", self.home / "Projects/wren", [("user", "Run the daily wren audit"), ("assistant", "Clean.")])
        self.session("memory-run", self.home / "Projects/wren",
                     [("user", "Consolidate memory candidates now"), ("assistant", "Upserted 3.")])
        self.store.request({"op": "update_memory_sessions", "session_ids": ["claude:memory-run"]})
        self.index()
        (memory,) = self.episodes()
        self.assertNotIn("Consolidate", memory["body"])
        self.store.episodes._discovered_at = None
        self.assertTrue(self.store.request({"op": "memory_tick"})["ok"])
        self.assertEqual(self.episodes()[0]["revision"], memory["revision"])
        status = self.store.request({"op": "knowledge_status"})["status"]["episodes"]
        self.assertEqual((status["buckets"], status["pending"]), (1, 0))


if __name__ == "__main__":
    unittest.main()
