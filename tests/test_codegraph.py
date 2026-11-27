import importlib.util
import tempfile
import unittest
from pathlib import Path


_SPEC = importlib.util.spec_from_file_location(
    "omni_knowledge_codegraph_tests", Path(__file__).parents[1] / "helpers/omni_knowledge.py")
KNOWLEDGE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(KNOWLEDGE)


class CodeGraphTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        self.project = self.home / "Projects/app"
        self.store = KNOWLEDGE.KnowledgeStore(roots=[], home=self.home, semantics=False,
                                              kb_roots=[("projects", self.home / "Projects")],
                                              code_roots=[("app", "app", self.project)])
        self.addCleanup(self.store.close)

    def write(self, relative, text):
        path = self.project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def tick(self):
        for _ in range(20):
            value = self.store.request({"op": "memory_tick"})
            self.assertTrue(value["ok"], value)
            if not self.store.codegraph.pending() and not self.store.knowledge_bases.pending():
                return
        self.fail("Code graph indexing did not converge")

    def graph(self, **arguments):
        value = self.store.request({"op": "code_graph", **arguments})
        self.assertTrue(value["ok"], value)
        return value

    def test_callers_follow_edits_and_a_broken_file_does_not_stop_indexing(self):
        self.write("lib/store.py", "class Store:\n    def save(self):\n        return persist_rows()\n\n"
                                   "def persist_rows():\n    return 1\n")
        self.write("app.py", "from lib.store import Store\n\ndef handle():\n    Store().save()\n")
        self.write("broken.py", "def oops(:\n")
        self.tick()

        explained = self.graph(mode="explain", name="Store.save")
        self.assertEqual(explained["node"]["path"], "lib/store.py")
        self.assertIn(("app.handle", "inferred"), [(c["from"], c["confidence"]) for c in explained["callers"]])
        rows = self.graph(mode="explain", name="persist_rows")
        self.assertEqual([(c["from"], c["confidence"]) for c in rows["callers"]],
                         [("lib.store.Store.save", "extracted")])
        importers = self.graph(mode="explain", name="Store")["importers"]
        self.assertEqual([(i["path"], i["confidence"]) for i in importers], [("app.py", "extracted")])
        affected = self.graph(mode="affected", name="persist_rows")
        self.assertEqual(affected["files"], ["app.py", "lib/store.py"])
        broken = self.graph(mode="stats")["file_errors"]
        self.assertEqual([(e["path"], e["error"]) for e in broken], [("broken.py", "syntax error at line 1")])

        # Only a project forge lists has a page to link to.
        self.assertIsNone(explained.get("link"))
        cache = self.home / "Library/Application Support/OmniTerm/forge-projects.json"
        cache.parent.mkdir(parents=True)
        cache.write_text('{"projects": [{"root": "app"}]}')
        self.store.codegraph.__dict__.pop("_forge_roots_cache", None)
        self.assertEqual(self.graph(mode="explain", name="Store.save")["link"],
                         "omniterm://projectapp?code=lib.store.Store.save&file=lib/store.py")
        self.assertEqual([r["kind"] for r in self.graph(mode="query", name="Store", kind="class")["results"]], ["class"])

        # The project a query names is brought up to date before it is answered.
        self.store.codegraph.db.execute("UPDATE projects SET listed_at=listed_at-60")
        self.write("app.py", "from lib.store import Store\n\ndef handle():\n    return Store\n\n\n")
        explained = self.graph(mode="explain", name="Store.save", project=str(self.project / "lib"))
        self.assertEqual(explained["callers"], [])

    def test_explain_and_the_memory_graph_link_memories_that_name_the_code(self):
        self.write("lib/store.py", "def persist_rows():\n    return 1\n")
        self.write("knowledge/storage.md", "# Storage\nKeep `persist_rows` idempotent; it lives in `lib/store.py`.\n")
        self.write("knowledge/retries.md", "# Retries\nRetries go through `lib/store.py` too.\n")
        self.tick()

        memories = self.graph(mode="explain", name="persist_rows")["memories"]
        self.assertEqual([(m["title"], m["matched"]) for m in memories], [("app · Storage", "persist_rows")])
        module = self.graph(mode="explain", path="lib/store.py")
        self.assertEqual(sorted(m["title"] for m in module["memories"]), ["app · Retries", "app · Storage"])
        graph = self.store.request({"op": "memory_graph", "include_code": True})
        code = [node for node in graph["nodes"] if node["kind"] == "code"]
        self.assertEqual([(node["project"], node["path"]) for node in code], [("app", "lib/store.py")])
        self.assertIn({"source": "concept:file:lib/store.py", "target": code[0]["id"], "kind": "code",
                       "strength": 1.0}, graph["edges"])

    def test_each_language_yields_definitions_and_the_edges_into_them(self):
        files = {
            "app/models/invoice.rb": "class Invoice < ApplicationRecord\n  def settle!\n    ledger_entry\n  end\n\n"
                                     "  def ledger_entry\n    1\n  end\nend\n",
            "app/jobs/close_job.rb": "require_relative '../models/invoice'\nclass CloseJob\n  def perform\n"
                                     "    Invoice.find(1).settle!\n  end\nend\n",
            "web/util.ts": "export function formatAmount(cents: number): string {\n  return `${cents}`\n}\n",
            "web/page.tsx": "import { formatAmount } from './util'\nexport class Page {\n  render() {\n"
                            "    return formatAmount(this.total())\n  }\n  total() { return 1 }\n}\n",
            "mac/Pane.swift": "final class LedgerPane: NSView {\n    func reload() {\n        refreshRows()\n    }\n"
                              "    private func refreshRows() {}\n}\n",
            "mac/Host.swift": "struct Host {\n    let pane = LedgerPane()\n}\n",
            "mac/Sched.m": "#import \"Sched.h\"\n@implementation Sched\n- (void)tick {\n    [self drainQueue];\n}\n"
                           "- (void)drainQueue {\n}\n@end\n",
            "mac/Sched.h": "@interface Sched : NSObject\n- (void)tick;\n@end\n",
            "svc/queue.go": "package svc\n\ntype Queue struct {\n}\n\nfunc (q *Queue) Push(item string) {\n"
                            "\tq.grow()\n}\n\nfunc (q *Queue) grow() {\n}\n",
            "core/src/lib.rs": "mod parse;\npub fn run() {\n    parse_all();\n}\n",
            "core/src/parse.rs": "pub fn parse_all() {\n}\n",
            "docs/billing.md": "# Billing\nSettlement lives in [the model](../app/models/invoice.rb).\n",
        }
        for relative, text in files.items():
            self.write(relative, text)
        self.tick()

        def callers(name, key="callers"):
            return sorted((edge["from"], edge["confidence"]) for edge in self.graph(mode="explain", name=name)[key])
        self.assertEqual(callers("settle!"), [("CloseJob.perform", "inferred")])
        self.assertEqual(callers("ledger_entry"), [("Invoice.settle!", "inferred")])
        self.assertFalse(self.graph(mode="explain", name="ApplicationRecord")["found"])  # defined elsewhere
        self.assertEqual(callers("app/models/invoice.rb", "importers"),
                         [("app/jobs/close_job.rb", "extracted")])
        self.assertEqual(callers("app/models/invoice.rb", "documented_in"), [("docs/billing.md", "extracted")])
        self.assertEqual(callers("formatAmount"), [("Page.render", "inferred")])
        self.assertEqual(callers("total"), [("Page.render", "extracted")])
        self.assertEqual(callers("web/util.ts", "importers"), [("web/page.tsx", "extracted")])
        self.assertEqual(callers("refreshRows"), [("LedgerPane.reload", "inferred")])
        self.assertEqual(callers("LedgerPane"), [("Host", "inferred")])
        self.assertEqual(callers("drainQueue"), [("Sched.tick", "extracted")])
        self.assertEqual(self.graph(mode="explain", name="Sched")["node"]["path"], "mac/Sched.m")
        self.assertEqual(callers("grow"), [("Queue.Push", "inferred")])
        self.assertEqual(callers("parse_all"), [("run", "inferred")])
        self.assertEqual(callers("core/src/parse.rs", "importers"), [("core/src/lib.rs", "extracted")])

    def test_secrets_and_keys_are_never_read(self):
        for relative in ("deploy/id_ed25519", "config/secrets/tokens.py", ".env.py",
                         "ops/credentials.py"):
            self.write(relative, "#!/usr/bin/env python3\ndef leaked():\n    pass\n")
        self.write("app.py", "def kept():\n    pass\n")
        self.tick()
        self.assertEqual(self.graph(mode="query", name="leaked")["results"], [])
        self.assertEqual([r["path"] for r in self.graph(mode="query", name="kept")["results"]], ["app.py"])


if __name__ == "__main__":
    unittest.main()
