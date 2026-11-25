"""Forge projects in the knowledge worker: sessions map to their project without waiting for forge."""
import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("omni_projects_test", ROOT / "helpers/omni_projects.py")
PROJECTS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROJECTS)

WEB = {"id": "/w/acme/web", "root": "/w/acme/web", "name": "web", "display_name": "acme/web",
       "checkouts": [{"name": "@head", "path": "/w/acme/web/@head"}]}


class FakeForge:
    def __init__(self):
        self.calls = []
        self.stdins = []

    def __call__(self, arguments, stdin):
        self.calls.append(arguments)
        self.stdins.append(stdin or "")
        if arguments[0] == "list":
            return {"schema": 1, "projects": [WEB]}
        if arguments[0] == "resolve":
            return {"schema": 1, "resolved": {path: "/w/acme/web" if "spike" in path else None
                                              for path in stdin.split()}}
        return {"schema": 1, "project": {"root": arguments[1]}}


class ProjectsTest(unittest.TestCase):
    def test_sessions_take_their_project_including_worktrees_outside_it(self):
        forge = FakeForge()
        projects = PROJECTS.Projects(runner=forge, cache=None)
        projects.list_projects({})
        sessions = [{"workspace": "/w/acme/web/@head/app"}, {"workspace": "/w/acme/web/gone-slug"},
                    {"workspace": "/elsewhere/web-spike"}, {"workspace": "/tmp"}]
        sessions.append({"workspace": PROJECTS.os.path.expanduser("~/Documents/notes")})
        projects.annotate(sessions)
        self.assertEqual([s.get("project", {}).get("name") for s in sessions], ["acme/web", "acme/web", None, None, None])
        projects._refresh(list_again=False)  # what the background thread does
        projects.annotate(sessions)
        self.assertEqual(sessions[2]["project"]["id"], "/w/acme/web")
        self.assertNotIn("project", sessions[3])
        # A folder macOS guards is never looked into (it would raise a privacy prompt).
        self.assertFalse(any("Documents" in stdin for stdin in forge.stdins))

    def test_detail_is_only_for_listed_projects(self):
        projects = PROJECTS.Projects(runner=FakeForge(), cache=None)
        self.assertEqual(projects.get_project({"project": "/w/acme/web"})["project"]["root"], "/w/acme/web")
        with self.assertRaises(ValueError):
            projects.get_project({"project": "/etc"})
        with self.assertRaises(ValueError):
            projects.get_project_file({"project": "/w/acme/web", "path": "/etc/passwd"})

    def test_old_forge_is_reported(self):
        projects = PROJECTS.Projects(runner=lambda arguments, stdin: {"schema": 0}, cache=None)
        status = projects.list_projects({})["forge"]
        self.assertFalse(status["available"])
        self.assertIn("too old", status["error"])

    def test_a_relaunch_answers_from_the_saved_list_without_waiting_for_forge(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = folder + "/projects.json"
            PROJECTS.Projects(runner=FakeForge(), cache=cache).list_projects({})
            calls = []
            relaunched = PROJECTS.Projects(runner=lambda arguments, stdin: calls.append(arguments), cache=cache)
            relaunched._kick = lambda wait=False: None  # the background refresh is not under test
            listed = relaunched.list_projects({})
            self.assertEqual([p["root"] for p in listed["projects"]], ["/w/acme/web"])
            self.assertEqual(calls, [])

    def test_session_filter_matches_checkouts_by_prefix_not_by_wildcard(self):
        import sqlite3
        projects = PROJECTS.Projects(runner=FakeForge(), cache=None)
        projects.list_projects({})
        projects._refresh(list_again=True)
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE s(workspace TEXT)")
        db.executemany("INSERT INTO s VALUES(?)", [("/w/acme/web",), ("/w/acme/web/@head/x",), ("/w/acme/webby",),
                                                  ("/w/acme/web_old",), ("/w/other",)])
        condition, bindings = projects.session_filter("/w/acme/web", column="workspace")
        found = [row[0] for row in db.execute("SELECT workspace FROM s WHERE " + condition, bindings)]
        self.assertEqual(found, ["/w/acme/web", "/w/acme/web/@head/x"])


if __name__ == "__main__":
    unittest.main()
