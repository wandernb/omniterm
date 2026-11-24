import datetime as dt
import importlib.util
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("omni_knowledge_summary_tests", ROOT / "helpers/omni_knowledge.py")
KNOWLEDGE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(KNOWLEDGE)
_SUMMARIES_SPEC = importlib.util.spec_from_file_location("omni_summaries_tests", ROOT / "helpers/omni_summaries.py")
SUMMARIES = importlib.util.module_from_spec(_SUMMARIES_SPEC)
_SUMMARIES_SPEC.loader.exec_module(SUMMARIES)


class WeekTests(unittest.TestCase):
    def test_week_keys_and_bounds_are_local_iso_weeks(self):
        start, end = SUMMARIES.week_bounds("2026-W45")
        self.assertEqual(dt.datetime.fromtimestamp(start), dt.datetime(2026, 11, 2))
        self.assertEqual(dt.datetime.fromtimestamp(end), dt.datetime(2026, 11, 9))
        self.assertEqual(SUMMARIES.week_key(dt.datetime(2026, 11, 8, 23, 59).timestamp()), "2026-W45")
        self.assertEqual(SUMMARIES.week_key(dt.datetime(2026, 11, 9).timestamp()), "2026-W46")
        for bad in ("2026-45", "2026-W60", None, "W45"):
            with self.assertRaises(SUMMARIES.SummaryError):
                SUMMARIES.week_bounds(bad)

    def test_episodes_belong_to_the_week_of_their_day_others_to_creation_or_revision(self):
        start, end = SUMMARIES.week_bounds("2026-W45")
        episode = {"id": "episode-1", "tags": ["episode", "x"], "title": "x · Wed 2026-11-04 — 2 sessions",
                   "created": end + 100}
        self.assertIsNotNone(SUMMARIES.item_time(episode, start, end))
        late = dict(episode, title="x · Mon 2026-11-09 — 1 session")
        self.assertIsNone(SUMMARIES.item_time(late, start, end))
        revised = {"id": "m", "tags": [], "created": start - 1000, "updated": start + 1000}
        self.assertEqual(SUMMARIES.item_time(revised, start, end), start + 1000)

    def test_sources_are_spread_across_items_before_repeating_one(self):
        items = [{"sources": [{"session_id": "a", "message_id": str(index)} for index in range(5)]},
                 {"sources": [{"session_id": "b", "message_id": "0"}]}]
        chosen = SUMMARIES.spread_sources(items, 3)
        self.assertEqual(chosen, [{"session_id": "a", "message_id": "0"}, {"session_id": "b", "message_id": "0"},
                                  {"session_id": "a", "message_id": "1"}])


class WeeklySummaryStoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name).resolve()
        self.claude = self.home / ".claude/projects"
        self.store = KNOWLEDGE.KnowledgeStore(roots=[("claude", self.claude)], home=self.home, semantics=False,
                                              kb_roots=[])
        self.addCleanup(self.store.close)
        # Last week's Tuesday and Thursday, at noon: one daily episode each.
        monday, _ = SUMMARIES.week_bounds(SUMMARIES.week_key(time.time()))
        self.last_week = SUMMARIES.week_key(monday - 3 * 86400)
        self.days = [monday - 6 * 86400 + 43200, monday - 4 * 86400 + 43200]

    def session(self, name, cwd, turns, stamp):
        path = self.claude / "workspace" / (name + ".jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp))
        path.write_text("".join(json.dumps({"type": role, "timestamp": when, "cwd": str(cwd),
                                            "message": {"role": role, "content": [{"type": "text", "text": text}]}}) + "\n"
                                for role, text in turns))

    def request(self, op, **arguments):
        value = self.store.request({"op": op, **arguments})
        self.assertTrue(value["ok"], value)
        return value

    def index(self):
        for _ in range(100):
            if self.store.scan()["scan_complete"]:
                break
        self.assertTrue(self.store.request({"op": "memory_tick"})["ok"])

    def fixture(self):
        lumen = self.home / "Projects/lumen"
        self.session("a", lumen, [("user", "Deploy the API to staging please"),
                                   ("assistant", "Deployed to staging; smoke tests green.")], self.days[0])
        self.session("b", lumen / "repositories/os/@head", [("user", "Fix the flaky login test in the OS"),
                                                             ("assistant", "Fixed: a race in setup.")], self.days[1])
        self.session("c", self.home / "Projects/other", [("user", "Tidy the other project's README"),
                                                         ("assistant", "Tidied.")], self.days[1])
        self.index()
        episodes = self.store.connection.execute("SELECT id FROM memories WHERE id LIKE 'episode-%'").fetchall()
        self.assertEqual(len(episodes), 3)

    def test_candidates_list_due_project_weeks_with_cited_evidence_and_write_a_summary(self):
        self.fixture()
        listed = self.request("weekly_summary_candidates")
        self.assertEqual(listed["daily"], {"written_today": 0, "cap": SUMMARIES.SUMMARIES_PER_DAY,
                                           "remaining_today": SUMMARIES.SUMMARIES_PER_DAY})
        # "other" has one episode that week: too quiet to summarise.
        self.assertEqual([(item["project"], item["week"]) for item in listed["candidates"]],
                         [("lumen", self.last_week)])
        candidate = listed["candidates"][0]
        self.assertTrue(candidate["complete"])
        self.assertEqual(len(candidate["episodes"]), 2)
        self.assertEqual(len(candidate["cites"]), 2)
        self.assertEqual(len(candidate["sources"]), 4)  # request and outcome of each session
        self.assertIsNone(candidate["existing"])
        written = self.request("write_weekly_summary", project="lumen", week=self.last_week,
                               title="lumen, week %s: staging deploy and a login fix" % self.last_week,
                               body="Deployed the API to staging and fixed a flaky login test.",
                               agent="Omni Memory", run_id="run-9")
        memory = written["memory"]
        self.assertEqual(memory["id"], candidate["id"])
        self.assertEqual(memory["memory_type"], "episodic")
        self.assertEqual(memory["tags"], ["weekly-summary", "lumen", self.last_week])
        self.assertEqual(len(memory["sources"]), 4)
        self.assertEqual(written["cites"], candidate["cites"])
        stored = self.store.hippocampus._stored(candidate["id"])["summary"]
        self.assertEqual((stored["project"], stored["week"], stored["cites"]), ("lumen", self.last_week, candidate["cites"]))
        # Written once, the week is no longer due, and summaries never cite summaries.
        again = self.request("weekly_summary_candidates")
        self.assertEqual(again["candidates"], [])
        self.assertEqual(again["daily"]["written_today"], 1)
        named = self.request("weekly_summary_candidates", project="lumen", week=self.last_week)["candidates"][0]
        self.assertNotIn(candidate["id"], named["cites"])
        self.assertFalse(named["stale"])
        # The cited episode's explanation names the summary that cites it.
        explanation = self.request("explain_memory", id=candidate["cites"][0])["explanation"]
        self.assertEqual([item["id"] for item in explanation["cited_by_summaries"]], [candidate["id"]])
        summary = self.request("explain_memory", id=candidate["id"])["explanation"]
        self.assertEqual(summary["summarizes"]["cites"], candidate["cites"])

    def test_writes_stop_at_the_daily_cap_and_quiet_weeks_are_refused(self):
        self.fixture()
        module = self.store._helper_module("omni_summaries")  # the store's own copy of the module
        with mock.patch.object(module, "SUMMARIES_PER_DAY", 0):
            capped = self.store.request({"op": "write_weekly_summary", "project": "lumen", "week": self.last_week,
                                         "title": "t", "body": "b"})
            self.assertEqual((capped["ok"], capped["code"]), (False, "daily_cap"))
            self.assertEqual(self.request("weekly_summary_candidates")["candidates"], [])
        quiet = self.store.request({"op": "write_weekly_summary", "project": "other", "week": self.last_week,
                                    "title": "t", "body": "b"})
        self.assertEqual((quiet["ok"], quiet["code"]), (False, "nothing_to_summarize"))
        for bad in ({"project": "a/b"}, {"week": "2026-45"}, {"title": ""}, {"body": " "}):
            arguments = dict({"op": "write_weekly_summary", "project": "lumen", "week": self.last_week,
                              "title": "t", "body": "b"}, **bad)
            self.assertFalse(self.store.request(arguments)["ok"], bad)

    def test_a_new_memory_in_the_week_marks_the_summary_stale(self):
        self.fixture()
        self.request("write_weekly_summary", project="lumen", week=self.last_week, title="t", body="b")
        this_week = SUMMARIES.week_key(time.time())
        source = self.store.connection.execute(
            "SELECT id,session_id FROM messages WHERE session_id LIKE '%:b' LIMIT 1").fetchone()
        evidence = [{"session_id": source["session_id"], "message_id": source["id"]}]
        self.request("upsert_memory", id="fresh", title="Login race", body="The login setup raced.", sources=evidence)
        current = self.request("weekly_summary_candidates", project="lumen", week=this_week)["candidates"]
        # One new memory this week is below the minimum on its own.
        self.assertEqual(current, [])
        self.request("upsert_memory", id="fresh2", title="Login fix", body="Serialised setup.", sources=evidence)
        current = self.request("weekly_summary_candidates", project="lumen", week=this_week)["candidates"][0]
        self.assertEqual(sorted(current["cites"]), ["fresh", "fresh2"])
        self.assertFalse(current["complete"])


if __name__ == "__main__":
    unittest.main()
