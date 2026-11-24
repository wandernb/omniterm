import importlib.util
import random
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("omni_concepts_for_tests", ROOT / "helpers/omni_concepts.py")
CONCEPTS = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(CONCEPTS)
HOME = "/Users/me"
WORKSPACE = "/Users/me/Projects/OmniTerm"


def extract(title, body="", tags=(), workspaces=(WORKSPACE,), limit=40):
    return CONCEPTS.extract(title, body, tags, workspaces, home=HOME, limit=limit)


class ExtractionTests(unittest.TestCase):
    def test_each_kind_is_found_in_memory_text(self):
        body = ("Running `git rebase -i main` failed with ENOENT, then SQLITE_BUSY and sqlite3.OperationalError.\n"
                "See knowledge/plans/@2026-11-10-02-semantic-search.md and `Hippocampus.graph()`.\n"
                "$ python3 -m unittest tests.test_kb\n"
                "pip install Requests==2.31 numpy. npm i @scope/pkg@1.2 lodash.\n"
                "from collections import OrderedDict\n"
                "Edit helpers/omni_kb.py:183 and the Makefile.")
        found = extract("Fix ValueError in `_cached_entry`", body)
        for key in ("command:git rebase", "command:python3 -m unittest", "error:ENOENT", "error:SQLITE_BUSY",
                    "error:OperationalError", "error:ValueError", "plan:2026-11-10-02-semantic-search",
                    "symbol:Hippocampus.graph", "symbol:_cached_entry", "package:requests", "package:numpy",
                    "package:@scope/pkg", "package:lodash", "package:collections", "file:helpers/omni_kb.py",
                    "file:Makefile"):
            self.assertIn(key, found)
        self.assertNotIn("file:knowledge/plans/@2026-11-10-02-semantic-search.md", found,
                         "A plan's file is named as the plan")

    def test_prose_urls_and_lookalikes_are_not_concepts(self):
        found = extract("Notes", "Read and/or write EVERY item. Use `foo` and `the thing`. "
                                 "See https://example.com/docs/page.html, version 0.3.1, /tmp/x/run.log, "
                                 "/workspace, `--force`, price $ 5 and import the data.")
        self.assertEqual(found, [])

    def test_paths_fold_to_the_workspace_or_home_and_drop_line_numbers(self):
        found = extract("Paths", "/Users/me/Projects/OmniTerm/helpers/omni_kb.py:183:4 and "
                                 "$HOME/Projects/OmniTerm/@head/scripts/build-app.sh, ~/notes/todo.txt, "
                                 "/Users/me/Projects/Other/lib/a.rb#L10-L20 and ./tests/test_kb.py.",
                        workspaces=(WORKSPACE + "/@head",))
        self.assertEqual([key for key in found if key.startswith("file:")],
                         ["file:helpers/omni_kb.py", "file:scripts/build-app.sh", "file:~/notes/todo.txt",
                          "file:~/Projects/Other/lib/a.rb", "file:tests/test_kb.py"])

    def test_relative_markdown_links_keep_their_naming_part_only(self):
        self.assertEqual(CONCEPTS.normalise_path("../../personas/carl.md"), "personas/carl.md")
        self.assertIsNone(CONCEPTS.normalise_path("../MASTER.md"), "every project has one: no shared meaning")
        self.assertIsNone(CONCEPTS.normalise_path("../../CHANGELOG.md"))

    def test_conventional_project_documents_join_nothing_unless_the_path_is_absolute(self):
        self.assertIsNone(CONCEPTS.normalise_path("knowledge/plans/STATUS.md"))
        self.assertIsNone(CONCEPTS.normalise_path("README.md"))
        self.assertEqual(CONCEPTS.normalise_path("/Users/me/Projects/fjord/knowledge/MASTER.md", home="/Users/me"),
                         "~/Projects/fjord/knowledge/MASTER.md", "an absolute path names one file")
        self.assertEqual(CONCEPTS.normalise_path("/Users/me/Projects/fjord/knowledge/MASTER.md",
                                                 roots=("/Users/me/Projects/fjord",), home="/Users/me"),
                         "~/Projects/fjord/knowledge/MASTER.md", "not the repo-relative knowledge/MASTER.md")
        self.assertEqual(CONCEPTS.normalise_path("knowledge/plans/@2026-11-10-02-x.md"),
                         "knowledge/plans/@2026-11-10-02-x.md")

    def test_tags_name_concepts_only_when_they_look_like_one(self):
        self.assertEqual(extract("T", tags=["build", "omni_kb.py", "ENOENT"]), ["error:ENOENT", "file:omni_kb.py"])

    def test_concepts_are_bounded_and_every_kind_gets_a_turn(self):
        body = " ".join("helpers/file_%02d.py" % index for index in range(30)) + " ENOENT `git status`"
        found = CONCEPTS.extract("T", body, home=HOME)
        self.assertEqual(len(found), CONCEPTS.MAX_CONCEPTS)
        self.assertIn("error:ENOENT", found)
        self.assertIn("command:git status", found)
        self.assertEqual(found, CONCEPTS.extract("T", body, home=HOME))


class GraphPieceTests(unittest.TestCase):
    def test_aliases_fold_to_the_one_path_they_can_mean(self):
        folded = CONCEPTS.fold_aliases({
            "a": ["file:omni_kb.py", "file:main.py"],
            "b": ["file:helpers/omni_kb.py", "file:~/Projects/OmniTerm/helpers/omni_kb.py"],
            "c": ["file:src/main.py"], "d": ["file:lib/main.py"]})
        self.assertEqual(folded["a"], ["file:helpers/omni_kb.py", "file:main.py"], "main.py is ambiguous")
        self.assertEqual(folded["b"], ["file:helpers/omni_kb.py"])

    def test_hubs_need_two_memories_and_at_most_sixty(self):
        memberships = {"m%03d" % index: ["file:common.py"] for index in range(61)}
        memberships["m000"] = ["file:common.py", "error:ENOENT", "file:alone.py"]
        memberships["m001"] = ["file:common.py", "error:ENOENT"]
        shared = CONCEPTS.hubs(memberships)
        self.assertEqual(shared, {"error:ENOENT": ["m000", "m001"]})

    def test_topics_are_deterministic_communities_named_by_their_concepts(self):
        memberships = {
            "a1": ["file:build.sh", "command:xcodebuild", "error:EXC_BAD_ACCESS"],
            "a2": ["file:build.sh", "command:xcodebuild"],
            "a3": ["command:xcodebuild", "error:EXC_BAD_ACCESS"],
            "b1": ["package:requests", "error:ConnectionError"],
            "b2": ["package:requests", "error:ConnectionError", "symbol:fetch_all"],
            "b3": ["package:requests", "symbol:fetch_all"],
            "lone": ["file:unique.py"]}
        topics = CONCEPTS.communities(memberships)
        self.assertEqual([topic["members"] for topic in topics], [["a1", "a2", "a3"], ["b1", "b2", "b3"]])
        self.assertEqual(topics[0]["concepts"], ["command:xcodebuild", "error:EXC_BAD_ACCESS", "file:build.sh"])
        self.assertEqual(topics[1]["concepts"][0], "package:requests")
        shuffled = list(memberships.items())
        random.Random(7).shuffle(shuffled)
        self.assertEqual(CONCEPTS.communities(dict(shuffled)), topics)
        self.assertEqual(CONCEPTS.communities({}), [])



class WorkspaceAliasTests(unittest.TestCase):
    def test_worktree_checkouts_fold_into_their_repository(self):
        alias = CONCEPTS.workspace_alias
        self.assertEqual(alias("/p/cobalt/@head"), "/p/cobalt")
        self.assertEqual(alias("/p/cobalt/@maintenance"), "/p/cobalt")
        self.assertEqual(alias("/p/garden/@head/platform/libs"), "/p/garden/platform/libs")
        self.assertEqual(alias("/p/garden-wt/gdn-next"), "/p/garden")
        self.assertEqual(alias("/p/garden-wt/cobalt-host/apps"), "/p/garden/apps")
        self.assertEqual(alias("/p/omni/.claude/worktrees/fix-1"), "/p/omni")
        self.assertEqual(alias("/p/fjord/core"), "/p/fjord/core")
        self.assertEqual(alias("/@head"), "/@head", "a root-level name is not a worktree")
        self.assertEqual(alias("relative/@head"), "relative/@head")

if __name__ == "__main__":
    unittest.main()
