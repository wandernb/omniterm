import datetime as dt
import importlib.machinery
import tempfile
import unittest
from pathlib import Path
from unittest import mock


OMNI = importlib.machinery.SourceFileLoader(
    "omni", str(Path(__file__).parents[1] / "helpers/omni")
).load_module()


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.zone = OMNI.ZoneInfo("Europe/Paris")
        self.now = dt.datetime(2026, 7, 29, 12, 0, tzinfo=self.zone)

    def test_friendly_forms(self):
        expected = {
            "every 30 minutes": "interval",
            "daily at 09:00": "daily",
            "weekdays at 9am": "weekdays",
            "weekly on monday at 10:00": "weekly",
            "once at 2026-08-01 14:00": "once",
        }
        for phrase, kind in expected.items():
            with self.subTest(phrase=phrase):
                schedule, next_run = OMNI.parse_schedule(phrase, "Europe/Paris", self.now)
                self.assertEqual(schedule["kind"], kind)
                self.assertGreater(next_run, self.now.timestamp())

    def test_weekdays_skips_weekend(self):
        friday = dt.datetime(2026, 7, 31, 18, 0, tzinfo=self.zone)
        _, next_run = OMNI.parse_schedule("weekdays at 9am", "Europe/Paris", friday)
        self.assertEqual(dt.datetime.fromtimestamp(next_run, self.zone).weekday(), 0)

    def test_daily_follows_wall_clock_across_dst(self):
        before_dst = dt.datetime(2026, 3, 28, 9, 0, tzinfo=self.zone).timestamp()
        schedule = {"kind": "daily", "hour": 9, "minute": 0}
        next_run = OMNI.next_calendar(schedule, "Europe/Paris", before_dst)
        result = dt.datetime.fromtimestamp(next_run, self.zone)
        self.assertEqual((result.hour, result.minute), (9, 0))

    def test_duration(self):
        self.assertEqual(OMNI.parse_duration("2h"), 7200)
        self.assertIsNone(OMNI.parse_duration("off"))

    def test_project_discovery_uses_nearest_agent_instructions(self):
        with tempfile.TemporaryDirectory() as root:
            project = Path(root) / "my-project"
            nested = project / "packages" / "feature"
            nested.mkdir(parents=True)
            (project / "AGENTS.md").write_text("instructions")
            self.assertEqual(OMNI.discover_project(nested), project.resolve())

    def test_project_discovery_falls_back_to_requested_folder(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / "standalone"
            folder.mkdir()
            self.assertEqual(OMNI.discover_project(folder), folder.resolve())

    def test_noun_first_cli_defaults(self):
        args = OMNI.cli_parser().parse_args(["agents"])
        self.assertEqual((args.command, args.agents_command), ("agents", None))
        args = OMNI.cli_parser().parse_args(["agent", "Apex Wrapup"])
        self.assertEqual((args.name, args.action), ("Apex Wrapup", "show"))

    def test_legacy_agent_cli_is_normalized(self):
        self.assertEqual(OMNI.normalized_cli_argv(["agent", "list"]), ["agents"])
        self.assertEqual(OMNI.normalized_cli_argv(["agent", "edit", "Apex"]),
                         ["agent", "Apex", "edit"])

    def test_agent_name_is_generated_from_workspace_and_command(self):
        self.assertEqual(OMNI.generated_agent_name("/tmp/apex-rb", "/forge:wrapup"),
                         "apex-rb · forge:wrapup")
        self.assertEqual(OMNI.generated_agent_name("/tmp/apex-rb", ""), "apex-rb")

    def test_path_completion_lists_matching_directories(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            (base / "project-one").mkdir()
            (base / "project-two").mkdir()
            (base / "plain-file").write_text("x")
            matches = OMNI.path_completion_matches(str(base / "project"))
            self.assertEqual(matches, [str(base / "project-one") + "/",
                                       str(base / "project-two") + "/"])

    def test_model_choices_have_default_and_custom_entries(self):
        for provider in ("codex", "claude"):
            choices = OMNI.model_choices(provider)
            self.assertEqual(choices[0], "provider default")
            self.assertEqual(choices[-1], "Custom model…")

    def test_prompt_preview_wraps_and_truncates(self):
        lines = OMNI.wrapped_preview("one two three four five six", 10, 2)
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[-1].endswith("…"))
        self.assertEqual(OMNI.wrapped_preview("", 20, 2), ["—"])

    def test_prompt_content_wraps_without_truncating(self):
        self.assertEqual(
            OMNI.wrapped_content("one two three four\n\nfive", 10),
            ["one two", "three four", "", "five"],
        )
        self.assertEqual(OMNI.wrapped_content("", 20), ["— No prompt configured —"])

    def test_cursor_visibility_is_best_effort(self):
        with mock.patch.object(OMNI.curses, "curs_set", side_effect=OMNI.curses.error):
            self.assertIsNone(OMNI.tui_cursor(1))


if __name__ == "__main__":
    unittest.main()
