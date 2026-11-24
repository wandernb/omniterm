import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path


_SPEC = importlib.util.spec_from_file_location(
    "omni_knowledge_kb_tests", Path(__file__).parents[1] / "helpers/omni_knowledge.py")
KNOWLEDGE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(KNOWLEDGE)
_KB_SPEC = importlib.util.spec_from_file_location("omni_kb_tests", Path(__file__).parents[1] / "helpers/omni_kb.py")
KB = importlib.util.module_from_spec(_KB_SPEC)
_KB_SPEC.loader.exec_module(KB)


class KnowledgeBaseTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        self.projects = self.home / "Projects"
        self.shared = self.home / ".claude/knowledge"
        self.store = KNOWLEDGE.KnowledgeStore(roots=[], home=self.home, semantics=False,
                                              kb_roots=[("projects", self.projects), ("shared", self.shared)])
        self.addCleanup(self.store.close)

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def tick(self, rediscover=False):
        bases = self.store.knowledge_bases
        if rediscover:
            bases._files = None
        for _ in range(50):
            value = self.store.request({"op": "memory_tick"})
            self.assertTrue(value["ok"], value)
            if not bases._queue:
                return value
        self.fail("Knowledge-base ingestion did not converge")

    def memory(self, path):
        identifier = "kb-" + KB.digest(str(path.resolve() if path.exists() else path))[:32]
        return self.store.connection.execute("SELECT * FROM memories WHERE id=?", (identifier,)).fetchone()

    def test_sections_ignore_headings_in_code_fences_and_skip_frontmatter(self):
        parsed = KB.sections("---\nstatus: done\n---\n# Title\nLead\n```\n# not a heading\n```\n## Next\nBody\n")
        self.assertEqual([section["heading"] for section in parsed], ["Title", "Next"])
        self.assertIn("# not a heading", parsed[0]["body"])
        self.assertEqual(parsed[1]["line"], 9)

    def test_discovery_maps_projects_reads_only_head_worktree_and_skips_noise(self):
        self.write(self.projects / "lumen/knowledge/api-deployment.md", "# Deploy\nHow we deploy.\n")
        self.write(self.projects / "basalt/core/@head/knowledge/plans/STATUS.md", "# Status\nGreen.\n")
        self.write(self.projects / "basalt/core/@production/knowledge/plans/STATUS.md", "# Status\nOld.\n")
        self.write(self.projects / "basalt/core/feature-x/knowledge/x.md", "# X\nDup.\n")
        self.write(self.projects / "app/node_modules/pkg/knowledge/readme.md", "# Noise\n")
        self.write(self.projects / ".gmail-backup-2026/knowledge/old.md", "# Old\n")
        self.write(self.projects / "a/b/c/d/e/knowledge/deep.md", "# Too deep\n")
        found = {(project, label) for _, project, _, label in KB.discover([("projects", self.projects)])}
        self.assertEqual(found, {("lumen", "lumen"), ("basalt", "basalt/core")})

    def test_extra_roots_come_from_the_owner_list_with_labels(self):
        self.assertEqual(KB.default_roots(self.home), [("projects", self.projects), ("shared", self.shared)],
                         "Without a list only the projects and the shared Claude folder are read")
        self.write(self.home / KB.EXTRA_ROOTS_FILE,
                   "# extra knowledge\n\nteam = ~/.team/knowledge\n/opt/notes/knowledge\n"
                   "projects=/ignored\nshared=/ignored\nempty=\n")
        self.assertEqual(KB.extra_roots(self.home),
                         [("team", self.home / ".team/knowledge"), ("notes", Path("/opt/notes/knowledge"))])
        self.assertEqual(KB.default_roots(self.home)[2:], KB.extra_roots(self.home))

    def test_extra_root_is_discovered_under_its_label(self):
        extra = self.home / ".team/knowledge"
        self.write(extra / "rules.md", "# Rules\nShip small.\n")
        found = KB.discover([("shared", self.shared), ("team", extra)])
        self.assertEqual([(project, label) for _, project, _, label in found], [("team", "team")])

    def test_ingest_indexes_sections_creates_project_memory_and_is_searchable(self):
        path = self.write(self.projects / "lumen/knowledge/api-deployment.md",
                          "---\nstatus: active\n---\n# API deployment\nThe API runs on Fly.\n\n"
                          "## Rollback\nUse fly releases rollback.\n")
        self.tick()
        rows = self.store.connection.execute(
            "SELECT * FROM messages WHERE provider='kb' ORDER BY source_line").fetchall()
        self.assertEqual([row["source_line"] for row in rows], [4, 7])
        self.assertEqual({row["role"] for row in rows}, {"document"})
        self.assertEqual(rows[0]["workspace"], str((self.projects / "lumen").resolve()))
        memory = self.memory(path)
        self.assertIsNotNone(memory)
        self.assertEqual(memory["title"], "lumen · API deployment")
        self.assertIn("Rollback: Use fly releases rollback.", memory["body"])
        self.assertIn("Status: active", memory["body"])
        self.assertEqual(KNOWLEDGE.json.loads(memory["tags"]), ["knowledge-base", "lumen"])
        hits = self.store.request({"op": "search_transcripts", "query": "fly releases rollback"})
        self.assertTrue(hits["ok"], hits)
        self.assertTrue(any(hit["session_id"].startswith("kb:lumen/") for hit in hits["matches"]), hits)
        found = self.store.request({"op": "search_memories", "query": "API"})
        self.assertEqual([item["id"] for item in found["memories"]], [memory["id"]])
        evidence = found["memories"][0]["sources"]
        self.assertTrue(all(item["available"] for item in evidence))
        overview = self.store.request({"op": "memory_overview"})
        self.assertTrue(overview["ok"], overview)

    def test_unchanged_files_are_not_rewritten_and_edits_revise_in_place(self):
        path = self.write(self.projects / "wren/knowledge/GUIDELINES.md", "# Guidelines\nAlways audit.\n## Keep\nSame.\n")
        self.tick()
        first = self.memory(path)
        self.assertEqual(self.store.hippocampus_module.attributes(
            self.store._document_value("memory:" + first["id"]))["memory_type"], "procedural")
        keep_id = self.store.connection.execute(
            "SELECT id FROM messages WHERE provider='kb' AND text LIKE '## Keep%'").fetchone()["id"]
        self.tick(rediscover=True)
        self.assertEqual(self.memory(path)["revision"], first["revision"])
        self.write(path, "# Guidelines\nAlways audit twice.\n## Keep\nSame.\n")
        os.utime(path, ns=(1, 2_000_000_000_000_000_000))
        self.tick(rediscover=True)
        revised = self.memory(path)
        self.assertEqual(revised["revision"], first["revision"] + 1)
        self.assertIn("twice", revised["body"])
        self.assertTrue(self.store.connection.execute("SELECT 1 FROM messages WHERE id=?", (keep_id,)).fetchone(),
                        "An untouched section keeps its id, so memory evidence stays valid")

    def test_deleted_file_retires_its_records_and_memory(self):
        path = self.write(self.shared / "pattern.md", "# Pattern\nUse it.\n")
        self.tick()
        resolved = path.resolve()
        self.assertIsNotNone(self.memory(resolved))
        path.unlink()
        self.tick(rediscover=True)
        self.assertIsNone(self.memory(resolved))
        self.assertIsNone(self.store.connection.execute("SELECT 1 FROM messages WHERE provider='kb'").fetchone())
        self.assertIsNone(self.store.connection.execute("SELECT 1 FROM sources WHERE provider='kb'").fetchone())

    def test_owner_retype_survives_reingestion(self):
        path = self.write(self.projects / "fjord/knowledge/notes.md", "# Notes\nA fact.\n")
        self.tick()
        identifier = self.memory(path)["id"]
        self.assertTrue(self.store.request({"op": "update_memory", "id": identifier, "memory_type": "spatial"})["ok"])
        self.write(path, "# Notes\nA changed fact.\n")
        os.utime(path, ns=(1, 3_000_000_000_000_000_000))
        self.tick(rediscover=True)
        stored = self.store._document_value("memory:" + identifier)
        self.assertEqual(self.store.hippocampus_module.attributes(stored)["memory_type"], "spatial")

    def test_transcript_evidence_added_by_an_update_survives_reingestion(self):
        path = self.write(self.projects / "fjord/knowledge/rules.md", "# Rules\nShip small.\n")
        self.tick()
        identifier = self.memory(path)["id"]
        self.store._put_source({"key": "source:chat", "session_id": "claude:chat", "provider": "claude",
                                "path": "/chat.jsonl", "workspace": "/w", "started": 1, "updated": 1, "archived": 0})
        self.store._put_message({"id": "said", "source_key": "source:chat", "session_id": "claude:chat",
                                 "provider": "claude", "workspace": "/w", "source_path": "/chat.jsonl",
                                 "source_line": 1, "part": 0, "role": "user", "kind": "message",
                                 "text": "Always ship small.", "timestamp": "2026-11-11T00:00:00Z"})
        self.store.upsert_memory({"id": identifier, "title": "fjord · Rules", "body": "Ship small, always.",
                                  "keep_sources": True, "sources": [{"session_id": "claude:chat", "message_id": "said"}]})
        self.write(path, "# Rules\nShip small and often.\n")
        os.utime(path, ns=(1, 4_000_000_000_000_000_000))
        self.tick(rediscover=True)
        memory = self.memory(path)
        self.assertIn("often", memory["body"], "the file remains the source of the text")
        self.assertIn({"session_id": "claude:chat", "message_id": "said"},
                      json.loads(memory["sources"]))

    def test_status_reports_progress_and_writes_are_emitted_for_native_persistence(self):
        self.write(self.projects / "cobalt/@head/knowledge/a.md", "# A\nOne.\n")
        value = self.tick()
        self.assertEqual(value["knowledge_bases"]["pending"], 0)
        keys = [write["key"] for write in value["_writes"]] + list(self.store._pending)
        self.assertTrue(any(key.startswith("source:kb:") for key in keys))
        status = self.store.request({"op": "knowledge_status"})["status"]["knowledge_bases"]
        self.assertEqual((status["knowledge_bases"], status["files"], status["pending"]), (1, 1, 0))

    def test_memory_reads_follow_creation_revision_and_deletion_through_the_cache(self):
        path = self.write(self.projects / "kesef/knowledge/a.md", "# First\nOne.\n")
        self.tick()

        def titles():
            value = self.store.request({"op": "list_memories"})
            self.assertTrue(value["ok"], value)
            return [memory["title"] for memory in value["memories"]]
        self.assertEqual(titles(), ["kesef · First"])
        self.write(path, "# Second\nTwo.\n")
        os.utime(path, ns=(1, 4_000_000_000_000_000_000))
        self.tick(rediscover=True)
        self.assertEqual(titles(), ["kesef · Second"])
        graph = self.store.request({"op": "memory_graph", "edges": ["workspace"]})
        self.assertIn("kesef", [node["label"] for node in graph["nodes"] if node["kind"] == "workspace"])
        path.unlink()
        self.tick(rediscover=True)
        self.assertEqual(titles(), [])


if __name__ == "__main__":
    unittest.main()
