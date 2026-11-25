"""Forge projects in the knowledge worker: sessions map to their project without waiting for forge."""
import importlib.util
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

    def __call__(self, arguments, stdin):
        self.calls.append(arguments)
        if arguments[0] == "list":
            return {"schema": 1, "projects": [WEB]}
        if arguments[0] == "resolve":
            return {"schema": 1, "resolved": {path: "/w/acme/web" if "spike" in path else None
                                              for path in stdin.split()}}
        return {"schema": 1, "project": {"root": arguments[1]}}


class ProjectsTest(unittest.TestCase):
    def test_sessions_take_their_project_including_worktrees_outside_it(self):
        forge = FakeForge()
        projects = PROJECTS.Projects(runner=forge)
        projects.list_projects({})
        sessions = [{"workspace": "/w/acme/web/@head/app"}, {"workspace": "/w/acme/web/gone-slug"},
                    {"workspace": "/elsewhere/web-spike"}, {"workspace": "/tmp"}]
        projects.annotate(sessions)
        self.assertEqual([s.get("project", {}).get("name") for s in sessions], ["acme/web", "acme/web", None, None])
        projects._refresh(list_again=False)  # what the background thread does
        projects.annotate(sessions)
        self.assertEqual(sessions[2]["project"]["id"], "/w/acme/web")
        self.assertNotIn("project", sessions[3])

    def test_detail_is_only_for_listed_projects(self):
        projects = PROJECTS.Projects(runner=FakeForge())
        self.assertEqual(projects.get_project({"project": "/w/acme/web"})["project"]["root"], "/w/acme/web")
        with self.assertRaises(ValueError):
            projects.get_project({"project": "/etc"})
        with self.assertRaises(ValueError):
            projects.get_project_file({"project": "/w/acme/web", "path": "/etc/passwd"})

    def test_old_forge_is_reported(self):
        projects = PROJECTS.Projects(runner=lambda arguments, stdin: {"schema": 0})
        status = projects.list_projects({})["forge"]
        self.assertFalse(status["available"])
        self.assertIn("too old", status["error"])


if __name__ == "__main__":
    unittest.main()
