import datetime as dt
import importlib.machinery
import importlib.util
import io
import json
import plistlib
import tempfile
import unittest
from contextlib import redirect_stderr
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


_LOADER = importlib.machinery.SourceFileLoader(
    "omni", str(Path(__file__).parents[1] / "helpers/omni")
)
OMNI = importlib.util.module_from_spec(importlib.util.spec_from_loader("omni", _LOADER))
_LOADER.exec_module(OMNI)


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

    def test_structured_calendar_intervals_keep_their_alignment(self):
        cases = {
            "every 2 hours at minute 15": ("hour", dt.datetime(2026, 7, 29, 12, 15,
                                                                 tzinfo=self.zone)),
            "every 3 days at 09:00": ("day", dt.datetime(2026, 7, 30, 9, 0,
                                                           tzinfo=self.zone)),
            "every 2 weeks on monday at 10:00": ("week", dt.datetime(2026, 8, 3, 10, 0,
                                                                       tzinfo=self.zone)),
            "every 1 month on day 31 at 09:00": ("month", dt.datetime(2026, 7, 31, 9, 0,
                                                                        tzinfo=self.zone)),
        }
        for phrase, (unit, first) in cases.items():
            with self.subTest(phrase=phrase):
                schedule, next_run = OMNI.parse_schedule(phrase, "Europe/Paris", self.now)
                self.assertEqual(schedule["kind"], "calendar_interval")
                self.assertEqual(schedule["unit"], unit)
                self.assertEqual(dt.datetime.fromtimestamp(next_run, self.zone), first)

    def test_monthly_interval_clamps_short_months_without_losing_requested_day(self):
        schedule, first = OMNI.parse_schedule(
            "every 1 month on day 31 at 09:00", "Europe/Paris", self.now)
        august = OMNI.next_calendar(schedule, "Europe/Paris", first)
        september = OMNI.next_calendar(schedule, "Europe/Paris", august)
        self.assertEqual(dt.datetime.fromtimestamp(august, self.zone).date(),
                         dt.date(2026, 8, 31))
        self.assertEqual(dt.datetime.fromtimestamp(september, self.zone).date(),
                         dt.date(2026, 9, 30))

    def test_schedule_form_builds_context_sensitive_human_phrase(self):
        values = {"repeat_count": "2", "repeat_unit": "weeks",
                  "repeat_on": "friday", "repeat_time": "14:30"}
        self.assertEqual(OMNI.schedule_phrase_from_form(values),
                         "every 2 weeks on friday at 14:30")
        self.assertIn("monday", OMNI.agent_form_choices("repeat_on", values))
        values["repeat_unit"] = "months"
        OMNI.normalize_schedule_controls(values)
        self.assertIn(values["repeat_on"], [str(day) for day in range(1, 32)])

    def test_daily_follows_wall_clock_across_dst(self):
        before_dst = dt.datetime(2026, 3, 28, 9, 0, tzinfo=self.zone).timestamp()
        schedule = {"kind": "daily", "hour": 9, "minute": 0}
        next_run = OMNI.next_calendar(schedule, "Europe/Paris", before_dst)
        result = dt.datetime.fromtimestamp(next_run, self.zone)
        self.assertEqual((result.hour, result.minute), (9, 0))

    def test_duration(self):
        expected = {"30 sec": 30, "1.5h": 5400, "2day": 172800}
        for value, seconds in expected.items():
            with self.subTest(value=value):
                self.assertEqual(OMNI.parse_duration(value), seconds)
        for value in ("off", "none", "disabled", "0"):
            with self.subTest(value=value):
                self.assertIsNone(OMNI.parse_duration(value))

    def test_duration_rejects_invalid_or_nonpositive_values(self):
        for value in ("later", "0h", "-1h", "0.1s"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                OMNI.parse_duration(value)

    def test_time_formats_and_errors(self):
        expected = {"09:00": (9, 0), "9am": (9, 0), "9:15PM": (21, 15)}
        for value, parsed in expected.items():
            with self.subTest(value=value):
                self.assertEqual(OMNI.parse_time(value), parsed)
        with self.assertRaises(ValueError):
            OMNI.parse_time("breakfast")

    def test_schedule_normalizes_whitespace_and_case(self):
        schedule, _ = OMNI.parse_schedule("  DAILY   AT  9AM ", "Europe/Paris", self.now)
        self.assertEqual(schedule["phrase"], "daily at 9am")

    def test_schedule_rejects_bad_forms(self):
        phrases = ("every 0 hours", "weekly on someday at 9am",
                   "once at 2020-01-01 09:00", "tomorrow")
        for phrase in phrases:
            with self.subTest(phrase=phrase), self.assertRaises(ValueError):
                OMNI.parse_schedule(phrase, "Europe/Paris", self.now)

    def test_next_occurrence_for_each_schedule_kind(self):
        after = self.now.timestamp()
        self.assertIsNone(OMNI.next_calendar({"kind": "once"}, "Europe/Paris", after))
        self.assertEqual(
            OMNI.next_calendar({"kind": "interval", "interval_seconds": 90},
                               "Europe/Paris", after),
            after + 90,
        )
        weekly = {"kind": "weekly", "weekday": self.now.weekday(), "hour": 11, "minute": 0}
        result = dt.datetime.fromtimestamp(
            OMNI.next_calendar(weekly, "Europe/Paris", after), self.zone)
        self.assertEqual(result, dt.datetime(2026, 8, 5, 11, 0, tzinfo=self.zone))

    def test_project_discovery_uses_nearest_agent_instructions(self):
        with tempfile.TemporaryDirectory() as root:
            project = Path(root) / "my-project"
            nested = project / "packages" / "feature"
            nested.mkdir(parents=True)
            (project / "AGENTS.md").write_text("instructions")
            self.assertEqual(OMNI.discover_project(nested), project.resolve())

    def test_project_discovery_accepts_a_file_and_claude_instructions(self):
        with tempfile.TemporaryDirectory() as root:
            project = Path(root) / "my project"
            nested = project / "src"
            nested.mkdir(parents=True)
            (project / "CLAUDE.md").write_text("instructions")
            source = nested / "main.py"
            source.write_text("pass")
            self.assertEqual(OMNI.discover_project(source), project.resolve())

    def test_project_discovery_rejects_missing_workspace(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(SystemExit, "Workspace does not exist"):
                OMNI.discover_project(Path(root) / "missing")

    def test_project_discovery_falls_back_to_requested_folder(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / "standalone"
            folder.mkdir()
            self.assertEqual(OMNI.discover_project(folder), folder.resolve())

    def test_noun_first_cli_defaults(self):
        args = OMNI.cli_parser().parse_args(["agents"])
        self.assertEqual((args.command, args.agents_command), ("agents", None))
        args = OMNI.cli_parser().parse_args(["agent", "Aspen Wrapup"])
        self.assertEqual((args.name, args.action), ("Aspen Wrapup", "show"))

    def test_legacy_agent_cli_is_normalized(self):
        self.assertEqual(OMNI.normalized_cli_argv(["agent", "list"]), ["agents"])
        self.assertEqual(OMNI.normalized_cli_argv(["agent", "edit", "Aspen"]),
                         ["agent", "Aspen", "edit"])
        self.assertEqual(OMNI.normalized_cli_argv(["agent", "add", "."]),
                         ["agents", "add", "."])
        self.assertEqual(OMNI.normalized_cli_argv(["run", "Aspen"]),
                         ["agent", "Aspen", "run"])
        run_id = "1B4E28BA-2FA1-11D2-883F-0016D3CCA427"
        self.assertEqual(OMNI.normalized_cli_argv(["run", run_id, "--json"]), ["run", run_id, "--json"])

    def test_agent_name_is_generated_from_workspace_and_command(self):
        self.assertEqual(OMNI.generated_agent_name("/tmp/aspen-rb", "/kit:wrapup"),
                         "aspen-rb · kit:wrapup")
        self.assertEqual(OMNI.generated_agent_name("/tmp/aspen-rb", ""), "aspen-rb")
        self.assertEqual(OMNI.generated_agent_name("/", "/// review ! now\nignored"),
                         "Agent · review now")

    def test_path_completion_lists_matching_directories(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            (base / "project-one").mkdir()
            (base / "project-two").mkdir()
            (base / "plain-file").write_text("x")
            matches = OMNI.path_completion_matches(str(base / "project"))
            self.assertEqual(matches, [str(base / "project-one") + "/",
                                       str(base / "project-two") + "/"])

    def test_path_completion_handles_relative_and_missing_paths(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.object(OMNI.os, "getcwd", return_value=root):
            base = Path(root)
            (base / "Alpha").mkdir()
            with mock.patch.object(OMNI.Path, "iterdir", return_value=iter([base / "Alpha"])):
                self.assertEqual(OMNI.path_completion_matches("Al"), ["Alpha/"])
        self.assertEqual(OMNI.path_completion_matches("/path/that/does/not/exist/x"), [])

    def test_model_choices_have_default_and_custom_entries(self):
        for provider in ("codex", "claude"):
            choices = OMNI.model_choices(provider)
            self.assertEqual(choices[0], "provider default")
            self.assertEqual(choices[-1], "Custom model…")

    def test_codex_model_catalog_filters_hidden_entries(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            (home / ".codex").mkdir()
            (home / ".codex/models_cache.json").write_text(json.dumps({"models": [
                {"slug": "visible"}, {"slug": "hidden", "visibility": "hide"}, {},
            ]}))
            with mock.patch.object(OMNI.Path, "home", return_value=home):
                self.assertEqual(OMNI.codex_model_choices(), ["visible"])

    def test_codex_model_catalog_falls_back_on_corrupt_cache(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            (home / ".codex").mkdir()
            (home / ".codex/models_cache.json").write_text("not json")
            with mock.patch.object(OMNI.Path, "home", return_value=home):
                self.assertEqual(OMNI.codex_model_choices(),
                                 ["gpt-5.6-sol", "gpt-5.6-terra"])

    def test_claude_model_catalog_discovers_nested_values_once(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            (home / ".claude").mkdir()
            (home / ".claude/settings.json").write_text(json.dumps({
                "claude-sonnet-5-0": {"nested": "claude-opus-5-0"},
                "ignored": ["claude-sonnet-5-0", "unrelated"],
            }))
            with mock.patch.object(OMNI.Path, "home", return_value=home):
                choices = OMNI.claude_model_choices()
            self.assertEqual(choices[:4], ["opus", "sonnet", "haiku", "fable"])
            self.assertEqual(choices.count("claude-sonnet-5-0"), 1)
            self.assertIn("claude-opus-5-0", choices)

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

    def test_editor_visual_rows_preserve_offsets_and_blank_lines(self):
        self.assertEqual(OMNI.editor_visual_rows("abcd\n\nef", 3), [
            (0, 3, "abc"), (3, 4, "d"), (5, 5, ""), (6, 8, "ef"),
        ])

    def test_display_path_abbreviates_only_the_home_directory(self):
        with mock.patch.object(OMNI.Path, "home", return_value=Path("/Users/test")):
            self.assertEqual(OMNI.display_path("/Users/test/work"), "~/work")
            self.assertEqual(OMNI.display_path("/Users/tester/work"), "/Users/tester/work")


class PresentationHelperTests(unittest.TestCase):
    def capture(self, function, *args, **kwargs):
        output = io.StringIO()
        with redirect_stdout(output):
            function(*args, **kwargs)
        return output.getvalue()

    def test_terminal_width_is_bounded_and_has_a_fallback(self):
        cases = ((20, 60), (84, 80), (200, 112))
        for columns, expected in cases:
            with self.subTest(columns=columns), \
                    mock.patch.object(OMNI.os, "get_terminal_size",
                                      return_value=OMNI.os.terminal_size((columns, 24))):
                self.assertEqual(OMNI.terminal_width(), expected)
        with mock.patch.object(OMNI.os, "get_terminal_size", side_effect=OSError):
            self.assertEqual(OMNI.terminal_width(), 88)

    def test_system_timezone_reads_zoneinfo_symlink_and_falls_back(self):
        with mock.patch.object(OMNI.Path, "resolve",
                               return_value=Path("/usr/share/zoneinfo/America/Toronto")):
            self.assertEqual(OMNI.system_timezone(), "America/Toronto")
        with mock.patch.object(OMNI.Path, "resolve", side_effect=OSError):
            self.assertEqual(OMNI.system_timezone(), "UTC")

    def test_panel_renders_title_padding_and_truncation(self):
        output = self.capture(OMNI.print_panel, ["short", "x" * 80], 24,
                              title="STATUS", padded=True)
        self.assertIn(" STATUS ", output)
        self.assertIn("short", output)
        self.assertIn("…", output)
        self.assertEqual(output.count("│"), 8)

    def test_time_formatting_handles_missing_and_named_timezones(self):
        self.assertEqual(OMNI.fmt_time(None), "—")
        timestamp = dt.datetime(2026, 8, 2, 9, 30,
                                tzinfo=OMNI.ZoneInfo("UTC")).timestamp()
        self.assertEqual(OMNI.fmt_time(timestamp, "UTC"), "2026-08-02 09:30")

    def test_tui_text_clips_to_window_and_ignores_invalid_positions(self):
        window = mock.Mock()
        window.getmaxyx.return_value = (3, 10)
        OMNI.tui_text(window, 1, 2, "long content", style=7, width=20)
        window.addnstr.assert_called_once_with(1, 2, "long content", 7, 7)
        window.reset_mock()
        window.getmaxyx.return_value = (3, 10)
        OMNI.tui_text(window, 3, 0, "outside")
        window.addnstr.assert_not_called()
        window.getmaxyx.side_effect = OMNI.curses.error
        OMNI.tui_text(window, 0, 0, "ignored")

    def test_tui_box_draws_all_edges_and_title(self):
        with mock.patch.object(OMNI, "tui_text") as draw:
            OMNI.tui_box(mock.Mock(), 2, 3, 4, 8, style=4, title="RUN")
        self.assertEqual(draw.call_count, 7)
        self.assertEqual(draw.call_args_list[0].args[3], "╭──────╮")
        self.assertIn(" RUN ", [call.args[3] for call in draw.call_args_list])
        with mock.patch.object(OMNI, "tui_text") as draw:
            OMNI.tui_box(mock.Mock(), 0, 0, 1, 8)
        draw.assert_not_called()

    def test_tui_palette_supports_monochrome_terminals(self):
        with mock.patch.object(OMNI.curses, "start_color") as start, \
                mock.patch.object(OMNI.curses, "has_colors", return_value=False):
            palette = OMNI.tui_palette()
        start.assert_called_once_with()
        self.assertEqual(set(palette.values()), {0})

    def test_ask_uses_defaults_and_routes_multiline_input(self):
        with mock.patch.object(OMNI, "readline", None), \
                mock.patch("builtins.input", return_value=""):
            self.assertEqual(OMNI.ask("Name", "Default"), "Default")
        with mock.patch.object(OMNI, "interactive_text_editor",
                               return_value="multiple\nlines") as editor:
            self.assertEqual(OMNI.ask("Prompt", "old", multiline=True),
                             "multiple\nlines")
        editor.assert_called_once_with("Prompt", "old")

    def test_interactive_picker_handles_keyboard_selection(self):
        window = mock.Mock()
        window.getmaxyx.return_value = (20, 80)
        window.getch.side_effect = [OMNI.curses.KEY_DOWN, 10]
        with mock.patch.object(OMNI.curses, "wrapper",
                               side_effect=lambda screen: screen(window)), \
                mock.patch.object(OMNI.curses, "mousemask"), \
                mock.patch.object(OMNI, "tui_palette",
                                  return_value={"peach": 1, "muted": 2}), \
                mock.patch.object(OMNI, "tui_cursor"), \
                mock.patch.object(OMNI, "tui_box"), \
                mock.patch.object(OMNI, "tui_text"):
            result = OMNI.interactive_picker("Provider", ["codex", "claude"],
                                             default="codex")
        self.assertEqual(result, "claude")
        window.keypad.assert_called_once_with(True)
        window.timeout.assert_called_once_with(-1)

    def test_interactive_path_picker_accepts_existing_directory(self):
        with tempfile.TemporaryDirectory() as root:
            window = mock.Mock()
            window.getmaxyx.return_value = (20, 100)
            window.get_wch.return_value = "\n"
            with mock.patch.object(OMNI.curses, "wrapper",
                                   side_effect=lambda screen: screen(window)), \
                    mock.patch.object(OMNI.curses, "mousemask"), \
                    mock.patch.object(OMNI, "path_completion_matches", return_value=[]), \
                    mock.patch.object(OMNI, "tui_palette",
                                      return_value={"peach": 1, "muted": 2,
                                                    "blue": 3}), \
                    mock.patch.object(OMNI, "tui_cursor"), \
                    mock.patch.object(OMNI, "tui_box"), \
                    mock.patch.object(OMNI, "tui_text"):
                result = OMNI.interactive_path_picker("Workspace", root)
        self.assertEqual(result, str(Path(root).resolve()))
        window.timeout.assert_called_once_with(-1)

    def test_interactive_text_editor_inserts_at_cursor(self):
        window = mock.Mock()
        window.getmaxyx.return_value = (20, 100)
        window.get_wch.side_effect = ["X", OMNI.curses.KEY_LEFT, "\x1e", "\n"]
        with mock.patch.object(OMNI.curses, "wrapper",
                               side_effect=lambda screen: screen(window)), \
                mock.patch.object(OMNI.curses, "mousemask"), \
                mock.patch.object(OMNI, "tui_palette",
                                  return_value={"peach": 1, "muted": 2}), \
                mock.patch.object(OMNI, "tui_cursor"), \
                mock.patch.object(OMNI, "tui_box"), \
                mock.patch.object(OMNI, "tui_text"):
            result = OMNI.interactive_text_editor("Prompt", "ab")
        self.assertEqual(result, "ab\nX")
        window.timeout.assert_called_once_with(-1)


class DashboardExit(Exception):
    pass


class DashboardTests(unittest.TestCase):
    @staticmethod
    def agent(name):
        return {
            "name": name, "provider": "codex", "enabled": True,
            "workspace": "/tmp", "model": "",
            "schedule": {"phrase": "daily at 09:00"},
            "overlap": "replace", "timeout_seconds": 7200,
            "next_run": None, "system_prompt": "System",
            "startup_prompt": "Start",
        }

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = mock.patch.object(OMNI, "DASHBOARD_STATE", Path(directory.name) / "dashboard.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_dashboard(self, keys, mouse=None, agents=None, runs=None, notifications=None, size=(32, 120)):
        agents = agents or [self.agent("A"), self.agent("B")]
        requests = []

        def request(payload):
            requests.append(payload)
            if payload["op"] == "list_agents":
                return {"agents": agents}
            if payload["op"] == "list_runs":
                return {"runs": runs or []}
            if payload["op"] == "notifications":
                if notifications is None:
                    raise SystemExit("Unknown operation")
                return notifications
            if payload["op"] == "run_agent":
                return {"run_id": "run-12345678"}
            return {"ok": True}

        window = mock.Mock()
        window.getmaxyx.return_value = size
        window.getch.side_effect = [*keys, DashboardExit()]
        palette = {"peach": 1, "muted": 2, "blue": 3,
                   "green": 4, "red": 5}
        with mock.patch.object(OMNI, "request", side_effect=request), \
                mock.patch.object(OMNI.time, "monotonic", return_value=0), \
                mock.patch.dict(OMNI.os.environ, {
                    "OMNITERM_KIND": "dashboard", "OMNITERM_DASHBOARD_KEYS": "1"}), \
                mock.patch.object(OMNI.curses, "mousemask"), \
                mock.patch.object(OMNI.curses, "getmouse", return_value=mouse), \
                mock.patch.object(OMNI, "tui_palette", return_value=palette), \
                mock.patch.object(OMNI, "tui_cursor"), \
                mock.patch.object(OMNI, "tui_box"), \
                mock.patch.object(OMNI, "tui_text") as draw, \
                redirect_stdout(io.StringIO()), self.assertRaises(DashboardExit):
            OMNI.dashboard_screen(window)
        return requests, draw

    NOTIFICATIONS = {
        "unread_count": 3, "attention_count": 2,
        "notifications": [
            {"id": "n1", "agent": "A", "run_id": "run-aaaa1111", "unread": True, "severity": "urgent"},
            {"id": "n2", "agent": "A", "run_id": "run-aaaa1111", "unread": False, "severity": "attention"},
            {"id": "n3", "agent": "A", "run_id": None, "unread": True, "severity": "attention"},
            {"id": "n4", "agent": None, "run_id": None, "unread": True, "severity": "info"},
        ],
    }
    RUNS = [{"id": "run-aaaa1111", "agent": "A", "state": "completed", "started": 0}]

    def texts(self, draw):
        return [call.args[3] for call in draw.call_args_list]

    def test_notification_counts_per_agent_and_run(self):
        per_agent, per_run = OMNI.notification_counts(self.NOTIFICATIONS["notifications"])
        self.assertEqual(per_agent, {"A": 2})
        self.assertEqual(per_run, {"run-aaaa1111": (1, 2)})
        self.assertEqual(OMNI.inbox_button_label(0), "[ ✉ Inbox ]")
        self.assertEqual(OMNI.inbox_button_label(3), "[ ✉ Inbox 3 ]")

    def test_dashboard_shows_inbox_button_and_counts(self):
        _, draw = self.run_dashboard([], runs=self.RUNS, notifications=self.NOTIFICATIONS, size=(48, 120))
        texts = self.texts(draw)
        self.assertIn("[ ✉ Inbox 3 ]", texts)
        self.assertIn("2 need you", texts)
        self.assertIn("✉ 2", texts)
        self.assertTrue(any("run-aaaa" in text and "✉ 2 (1 new)" in text for text in texts), texts)

    def test_clicking_the_inbox_button_or_pressing_i_opens_the_inbox(self):
        _, draw = self.run_dashboard([], notifications=self.NOTIFICATIONS)
        button = next(call for call in draw.call_args_list if call.args[3] == "[ ✉ Inbox 3 ]")
        y, x = button.args[1], button.args[2]
        click = (0, x + 2, y, 0, OMNI.curses.BUTTON1_CLICKED)
        requests, _ = self.run_dashboard([OMNI.curses.KEY_MOUSE], mouse=click, notifications=self.NOTIFICATIONS)
        self.assertIn({"op": "show_inbox"}, requests)
        requests, _ = self.run_dashboard([ord("i")], notifications=self.NOTIFICATIONS)
        self.assertIn({"op": "show_inbox"}, requests)

    def test_dashboard_still_draws_when_the_scheduler_has_no_notifications(self):
        _, draw = self.run_dashboard([], notifications=None)
        self.assertIn("[ ✉ Inbox ]", self.texts(draw))

    def test_down_selects_next_agent_and_enter_only_focuses_run_now(self):
        requests, draw = self.run_dashboard(
            [OMNI.curses.KEY_DOWN, OMNI.DASHBOARD_ACTIVATE_KEY])
        self.assertNotIn("run_agent", [payload["op"] for payload in requests])
        self.assertIn("▶ B", [call.args[3] for call in draw.call_args_list])
        focused_run_buttons = [
            call for call in draw.call_args_list
            if call.args[3] == "[ ▶ Run now ]" and call.args[4] & OMNI.curses.A_REVERSE
        ]
        self.assertTrue(focused_run_buttons)

    def test_enter_activates_run_now_only_after_it_has_focus(self):
        activate = OMNI.DASHBOARD_ACTIVATE_KEY
        requests, _ = self.run_dashboard([activate, activate])
        self.assertEqual(
            [payload for payload in requests if payload["op"] == "run_agent"],
            [{"op": "run_agent", "name": "A"}],
        )

    def test_return_bytes_arrows_and_shortcuts_cannot_activate_run_now(self):
        activate = OMNI.DASHBOARD_ACTIVATE_KEY
        requests, _ = self.run_dashboard(
            [activate, 10, 13, ord("r"), OMNI.curses.KEY_DOWN])
        self.assertNotIn("run_agent", [payload["op"] for payload in requests])

    def test_left_from_run_now_returns_to_agent_list(self):
        activate = OMNI.DASHBOARD_ACTIVATE_KEY
        requests, _ = self.run_dashboard(
            [activate, OMNI.curses.KEY_LEFT, activate])
        self.assertNotIn("run_agent", [payload["op"] for payload in requests])

    def test_run_now_button_is_clickable(self):
        mouse = (0, 105, 8, 0, OMNI.curses.BUTTON1_CLICKED)
        requests, _ = self.run_dashboard([OMNI.curses.KEY_MOUSE], mouse=mouse)
        self.assertEqual(
            [payload for payload in requests if payload["op"] == "run_agent"],
            [{"op": "run_agent", "name": "A"}],
        )


    def repo_agents(self, root):
        agents = []
        for name, workspace in (("Zed", "api/@head"), ("Beta", "web"), ("Alpha", "api/@head/sub")):
            folder = Path(root) / workspace
            folder.mkdir(parents=True, exist_ok=True)
            agent = self.agent(name)
            agent["workspace"] = str(folder)
            agents.append(agent)
        (Path(root) / "api/@head/.git").write_text("gitdir: elsewhere\n")
        (Path(root) / "web/.git").mkdir()
        return agents

    def drawn(self, draw):
        return [call.args[3] for call in draw.call_args_list]

    def test_dangerous_agent_is_marked_in_the_list_and_details(self):
        agent = self.agent("A")
        agent.update(provider="claude", permission_mode="bypassPermissions")
        _, draw = self.run_dashboard([], agents=[agent])
        text = self.drawn(draw)
        self.assertIn("⚠", text)
        self.assertIn("⚠ DANGEROUS · no safety checks", text)

    def test_normal_agent_shows_no_danger_marker(self):
        _, draw = self.run_dashboard([])
        self.assertFalse([value for value in self.drawn(draw) if "⚠" in str(value)])

    def test_grouping_key_lists_agents_under_their_repository_and_is_remembered(self):
        with tempfile.TemporaryDirectory() as root:
            agents = self.repo_agents(root)
            _, draw = self.run_dashboard([ord("g")], agents=agents)
            text = self.drawn(draw)
            self.assertIn("▤ api  ·  2", text)
            self.assertIn("▤ web  ·  1", text)
            listed = ["▤ api  ·  2", "  Alpha", "▶ Zed", "▤ web  ·  1", "  Beta"]
            self.assertEqual([value for value in text if value in listed][-5:], listed)
            self.assertTrue(json.loads(OMNI.DASHBOARD_STATE.read_text())["group_by_repo"])

            _, draw = self.run_dashboard([], agents=agents)
            self.assertIn("▤ web  ·  1", self.drawn(draw))

    def test_grouping_button_is_clickable_and_toggles_back_to_the_flat_list(self):
        with tempfile.TemporaryDirectory() as root:
            agents = self.repo_agents(root)
            click = (0, 50, 6, 0, OMNI.curses.BUTTON1_CLICKED)
            _, draw = self.run_dashboard([OMNI.curses.KEY_MOUSE], mouse=click, agents=agents)
            self.assertIn("[ ≡ All agents ]", self.drawn(draw))

            _, draw = self.run_dashboard([ord("g")], agents=agents)
            self.assertIn("[ ▤ By repo ]", self.drawn(draw))
            self.assertFalse(json.loads(OMNI.DASHBOARD_STATE.read_text())["group_by_repo"])

    def test_grouping_keeps_the_same_agent_selected(self):
        with tempfile.TemporaryDirectory() as root:
            agents = self.repo_agents(root)
            _, draw = self.run_dashboard([OMNI.curses.KEY_DOWN, ord("g")], agents=agents)
            self.assertEqual(self.drawn(draw).count("▶ Beta"), 2)


class DangerousPermissionTests(unittest.TestCase):
    def test_each_provider_has_exactly_one_dangerous_mode(self):
        self.assertTrue(OMNI.has_dangerous_permissions(
            {"provider": "claude", "permission_mode": "bypassPermissions"}))
        self.assertTrue(OMNI.has_dangerous_permissions(
            {"provider": "codex", "permission_mode": "dangerous"}))
        for agent in ({"provider": "claude", "permission_mode": "auto"},
                      {"provider": "codex", "permission_mode": "never"},
                      {"provider": "codex", "permission_mode": "bypassPermissions"},
                      {"provider": "claude"}):
            self.assertFalse(OMNI.has_dangerous_permissions(agent))

    def test_editor_offers_the_mode_for_each_provider(self):
        self.assertIn("bypassPermissions",
                      OMNI.agent_form_choices("permission_mode", {"provider": "claude"}))
        self.assertIn("dangerous", OMNI.agent_form_choices("permission_mode", {"provider": "codex"}))


class AgentGroupingTests(unittest.TestCase):
    def test_workspace_repo_finds_enclosing_repository_and_folds_worktrees(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root).resolve()
            nested = root / "plain/src/deep"
            nested.mkdir(parents=True)
            (root / "plain/.git").mkdir()
            worktree = root / "repo/@main"
            worktree.mkdir(parents=True)
            (worktree / ".git").write_text("gitdir: elsewhere\n")
            loose = root / "loose"
            loose.mkdir()
            self.assertEqual(OMNI.workspace_repo(str(nested)), str(root / "plain"))
            self.assertEqual(OMNI.workspace_repo(str(worktree)), str(root / "repo"))
            self.assertEqual(OMNI.workspace_repo(str(loose)), str(loose))
            self.assertEqual(OMNI.workspace_repo(str(root / "missing/folder")),
                             str(root / "missing/folder"))

    def test_workspace_repo_does_not_treat_a_home_repository_as_the_project(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root).resolve()
            (home / ".git").mkdir()
            project = home / "Projects/app"
            project.mkdir(parents=True)
            with mock.patch.object(OMNI.Path, "home", return_value=home):
                self.assertEqual(OMNI.workspace_repo(str(project)), str(project))

    def test_labels_keep_only_the_distinguishing_path(self):
        self.assertEqual(OMNI.repo_labels(["/p/a/one", "/p/a/two", "/p/b"]),
                         {"/p/a/one": "a/one", "/p/a/two": "a/two", "/p/b": "b"})
        self.assertEqual(OMNI.repo_labels(["/p/app"]), {"/p/app": "app"})
        self.assertEqual(OMNI.repo_labels(["/p/app", "/p/app/nested"]),
                         {"/p/app": "app", "/p/app/nested": "nested"})

    def groups(self):
        return [("a", ["1", "2", "3"]), ("b", ["4", "5"])]

    def test_layout_shows_everything_that_fits_from_the_top(self):
        rows = OMNI.agent_list_rows(self.groups(), 0, 14)
        self.assertEqual(rows, [("header", "a", 3), ("agent", 0), ("agent", 1), ("agent", 2)])
        rows = OMNI.agent_list_rows(self.groups(), 0, 18)
        self.assertEqual(rows[-2:], [("header", "b", 2), ("agent", 3)])

    def test_layout_keeps_selection_visible_and_pins_its_group(self):
        rows = OMNI.agent_list_rows(self.groups(), 2, 9)
        self.assertEqual(rows, [("header", "a", 3), ("agent", 1), ("agent", 2)])
        rows = OMNI.agent_list_rows(self.groups(), 4, 9)
        self.assertEqual(rows, [("header", "b", 2), ("agent", 3), ("agent", 4)])

    def test_flat_layout_has_no_headers_and_scrolls_by_card(self):
        rows = OMNI.agent_list_rows([(None, ["1", "2", "3", "4"])], 3, 8)
        self.assertEqual(rows, [("agent", 2), ("agent", 3)])

    def test_layout_always_shows_the_selection_in_a_tiny_panel(self):
        self.assertIn(("agent", 4), OMNI.agent_list_rows(self.groups(), 4, 2))

    def test_missing_or_corrupt_preference_defaults_to_the_flat_list(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root) / "dashboard.json"
            with mock.patch.object(OMNI, "DASHBOARD_STATE", state):
                self.assertFalse(OMNI.load_repo_grouping())
                for corrupt in ("not json", "[]"):
                    state.write_text(corrupt)
                    self.assertFalse(OMNI.load_repo_grouping())
                OMNI.save_repo_grouping(True)
                self.assertTrue(OMNI.load_repo_grouping())


class AgentWizardTests(unittest.TestCase):
    def test_new_agent_wizard_normalizes_and_persists_configuration(self):
        with tempfile.TemporaryDirectory() as root:
            requests = []

            def request(payload):
                requests.append(payload)
                if payload["op"] == "get_settings":
                    return {"settings": {"timezone": "UTC",
                                         "default_timeout_seconds": 1800}}
                return {"ok": True}

            def form(values, **kwargs):
                values.update({
                    "name": "", "provider": "codex", "workspace": root,
                    "system_prompt": "You review code.", "startup_prompt": "review",
                    "model": "", "schedule_phrase": "every 1 hour",
                    "overlap": "replace", "timeout": "2h",
                    "permission_mode": "default", "advanced_args": "--color always",
                })
                values.update(OMNI.schedule_form_values("every 1 hour"))
                kwargs["validator"](values)
                return values

            with mock.patch.object(OMNI, "request", side_effect=request), \
                    mock.patch.object(OMNI, "interactive_agent_form", side_effect=form), \
                    redirect_stdout(io.StringIO()):
                OMNI.edit_agent(seed_path=root)

        saved = [payload for payload in requests if payload["op"] == "save_agent"]
        self.assertEqual(len(saved), 1)
        agent = saved[0]["agent"]
        self.assertEqual(agent["name"], f"{Path(root).name} · review")
        self.assertEqual(agent["provider"], "codex")
        self.assertEqual(agent["workspace"], str(Path(root).resolve()))
        self.assertEqual(agent["schedule"]["kind"], "interval")
        self.assertEqual(agent["timeout_seconds"], 7200)
        self.assertEqual(agent["permission_mode"], "")
        self.assertEqual(agent["advanced_args"], ["--color", "always"])

    def test_workspace_tab_completion_extends_prefix_then_cycles_matches(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "alpha").mkdir()
            (Path(root) / "alpine").mkdir()
            query = str(Path(root) / "a")
            value, state = OMNI.shell_path_tab(query, {})
            self.assertEqual(value, str(Path(root) / "alp"))
            value, state = OMNI.shell_path_tab(value, state)
            self.assertEqual(value, str(Path(root) / "alpha") + "/")
            self.assertEqual(state["index"], 0)
            value, state = OMNI.shell_path_tab(value, state)
            self.assertEqual(value, str(Path(root) / "alpine") + "/")
            self.assertEqual(state["index"], 1)

    def test_inline_form_navigates_fields_and_modified_enter_adds_prompt_line(self):
        with tempfile.TemporaryDirectory() as root:
            values = {
                "name": "", "provider": "codex", "workspace": root,
                "system_prompt": "", "startup_prompt": "", "model": "",
                "schedule_phrase": "daily at 09:00", "overlap": "replace",
                "timeout": "2h", "permission_mode": "", "advanced_args": "",
            }
            window = mock.Mock()
            window.getmaxyx.return_value = (32, 120)
            window.get_wch.side_effect = ([OMNI.curses.KEY_RIGHT] * 3 +
                                          ["\x1e", "x"] +
                                          [OMNI.curses.KEY_RIGHT] * (len(OMNI.AGENT_FORM_FIELDS) - 4) + ["\n"])
            palette = {"peach": 1, "muted": 2, "blue": 3, "green": 4, "red": 5}
            with mock.patch.object(OMNI.curses, "wrapper",
                                   side_effect=lambda callback: callback(window)), \
                    mock.patch.object(OMNI.curses, "mousemask"), \
                    mock.patch.object(OMNI, "tui_palette", return_value=palette), \
                    mock.patch.object(OMNI, "tui_cursor"), \
                    mock.patch.object(OMNI, "tui_text"):
                result = OMNI.interactive_agent_form(values)
        self.assertEqual(result["system_prompt"], "\nx")
        window.timeout.assert_called_once_with(-1)

    def test_inline_workspace_enter_accepts_current_path(self):
        with tempfile.TemporaryDirectory() as root:
            child = Path(root) / "project"
            child.mkdir()
            values = {
                "name": "", "provider": "codex", "workspace": root,
                "system_prompt": "", "startup_prompt": "", "model": "",
                "schedule_phrase": "daily at 09:00", "overlap": "replace",
                "timeout": "2h", "permission_mode": "", "advanced_args": "",
            }
            window = mock.Mock()
            window.getmaxyx.return_value = (32, 120)
            window.get_wch.side_effect = ([OMNI.curses.KEY_RIGHT] * 2 + ["\n"] +
                                          [OMNI.curses.KEY_RIGHT] * (len(OMNI.AGENT_FORM_FIELDS) - 4) + ["\n"])
            palette = {"peach": 1, "muted": 2, "blue": 3, "green": 4, "red": 5}
            with mock.patch.object(OMNI.curses, "wrapper",
                                   side_effect=lambda callback: callback(window)), \
                    mock.patch.object(OMNI.curses, "mousemask"), \
                    mock.patch.object(OMNI, "tui_palette", return_value=palette), \
                    mock.patch.object(OMNI, "tui_cursor"), \
                    mock.patch.object(OMNI, "tui_text"):
                result = OMNI.interactive_agent_form(values)
        self.assertEqual(Path(result["workspace"]).resolve(), Path(root).resolve())

    def test_inline_workspace_tab_descends_before_enter_accepts(self):
        with tempfile.TemporaryDirectory() as root:
            child = Path(root) / "project"
            child.mkdir()
            values = {
                "name": "", "provider": "codex", "workspace": root,
                "system_prompt": "", "startup_prompt": "", "model": "",
                "schedule_phrase": "daily at 09:00", "overlap": "replace",
                "timeout": "2h", "permission_mode": "", "advanced_args": "",
            }
            window = mock.Mock()
            window.getmaxyx.return_value = (32, 120)
            window.get_wch.side_effect = ([OMNI.curses.KEY_RIGHT] * 2 + ["\t", "\t", "\n"] +
                                          [OMNI.curses.KEY_RIGHT] * (len(OMNI.AGENT_FORM_FIELDS) - 4) + ["\n"])
            palette = {"peach": 1, "muted": 2, "blue": 3, "green": 4, "red": 5}
            with mock.patch.object(OMNI.curses, "wrapper",
                                   side_effect=lambda callback: callback(window)), \
                    mock.patch.object(OMNI.curses, "mousemask"), \
                    mock.patch.object(OMNI, "tui_palette", return_value=palette), \
                    mock.patch.object(OMNI, "tui_cursor"), \
                    mock.patch.object(OMNI, "tui_text"):
                result = OMNI.interactive_agent_form(values)
        self.assertEqual(Path(result["workspace"]).resolve(), child.resolve())

    def test_claude_auto_mode_label_normalizes_to_cli_permission_value(self):
        with tempfile.TemporaryDirectory() as root:
            values = {
                "name": "A", "provider": "claude", "workspace": root,
                "system_prompt": "", "startup_prompt": "", "model": "",
                "schedule_phrase": "daily at 09:00", "overlap": "replace",
                "timeout": "2h", "permission_mode": "auto-mode", "advanced_args": "",
            }
            agent = OMNI.normalized_agent(values, {}, "UTC")
        self.assertEqual(agent["permission_mode"], "auto")
        self.assertIn("auto-mode", OMNI.agent_form_choices("permission_mode", values))


class SocketRequestTests(unittest.TestCase):
    def test_loop_save_refuses_old_scheduler_without_sending_definition(self):
        client = mock.Mock()
        client.recv.side_effect = [b'{"ok":true,"settings":{}}', b""]
        with mock.patch.object(OMNI.socket, "socket", return_value=client) as socket, \
                self.assertRaisesRegex(SystemExit, "does not support persistent loops"):
            OMNI.request({"op": "save_agent", "agent": {"kind": "loop", "name": "Loop"}})
        socket.assert_called_once()
        self.assertEqual(json.loads(client.sendall.call_args.args[0]), {"op": "get_settings"})

    def test_request_frames_json_reads_chunks_and_closes(self):
        client = mock.Mock()
        client.recv.side_effect = [b'{"ok":true,', b'"value":7}', b""]
        with mock.patch.object(OMNI.socket, "socket", return_value=client):
            self.assertEqual(OMNI.request({"op": "ping"})["value"], 7)
        client.connect.assert_called_once_with(str(OMNI.SOCKET))
        self.assertEqual(json.loads(client.sendall.call_args.args[0]), {"op": "ping"})
        client.shutdown.assert_called_once_with(OMNI.socket.SHUT_WR)
        client.close.assert_called_once_with()

    def test_request_reports_connection_and_scheduler_errors_and_closes(self):
        client = mock.Mock()
        client.connect.side_effect = OSError("offline")
        with mock.patch.object(OMNI.socket, "socket", return_value=client), \
                self.assertRaisesRegex(SystemExit, "scheduler is unavailable"):
            OMNI.request({"op": "ping"})
        client.close.assert_called_once_with()

        client = mock.Mock()
        client.recv.side_effect = [b'{"ok":false,"error":"bad request"}', b""]
        with mock.patch.object(OMNI.socket, "socket", return_value=client), \
                self.assertRaisesRegex(SystemExit, "bad request"):
            OMNI.request({"op": "bad"})
        client.close.assert_called_once_with()


class CommandOutputTests(unittest.TestCase):
    def capture(self, function, *args, **kwargs):
        output = io.StringIO()
        with redirect_stdout(output):
            function(*args, **kwargs)
        return output.getvalue()

    def test_machine_readable_outputs_are_valid_json(self):
        agents = [{"name": "A", "provider": "codex", "enabled": True,
                   "schedule": {"phrase": "daily at 09:00"}}]
        self.assertEqual(json.loads(self.capture(OMNI.print_agents, agents, True)), agents)
        self.assertEqual(json.loads(self.capture(OMNI.print_agent, agents[0], True)), agents[0])
        runs = [{"id": "run", "agent": "A", "state": "completed", "started": 1}]
        self.assertEqual(json.loads(self.capture(OMNI.print_runs, runs, True)), runs)

    def test_empty_human_readable_lists_explain_what_to_do(self):
        self.assertIn("No agents configured", self.capture(OMNI.print_agents, []))
        self.assertIn("No runs found", self.capture(OMNI.print_runs, []))

    def test_preview_schedule_prints_once_only_once(self):
        timestamp = dt.datetime(2026, 8, 2, 9, 0, tzinfo=OMNI.ZoneInfo("UTC")).timestamp()
        responses = [
            {"agent": {"next_run": timestamp, "schedule": {"kind": "once"}}},
            {"settings": {"timezone": "UTC"}},
        ]
        with mock.patch.object(OMNI, "request", side_effect=responses):
            output = self.capture(OMNI.preview_schedule, "A")
        self.assertEqual(output.strip(), "2026-08-02T09:00:00+00:00")

    def test_main_routes_noun_first_actions_to_scheduler(self):
        with mock.patch.object(OMNI, "request", return_value={"ok": True}) as request:
            OMNI.main(["agent", "A", "enable"])
        request.assert_called_once_with({"op": "set_enabled", "name": "A", "enabled": True})

        with mock.patch.object(OMNI, "request", return_value={"run_id": "new"}) as request:
            output = self.capture(OMNI.main, ["resume", "old"])
        self.assertEqual(output.strip(), "new")
        request.assert_called_once_with({"op": "resume_run", "run_id": "old"})

    def test_human_readable_agent_and_run_tables_include_state(self):
        agent = {"name": "A", "provider": "claude", "enabled": False,
                 "workspace": "/tmp", "model": "", "schedule": {"phrase": "daily"},
                 "overlap": "parallel", "timeout_seconds": None, "next_run": None}
        output = self.capture(OMNI.print_agent, agent)
        self.assertIn("disabled", output)
        self.assertIn("provider default", output)
        self.assertIn("off", output)
        agents = self.capture(OMNI.print_agents, [agent])
        self.assertIn("NAME", agents)
        self.assertIn("disabled", agents)
        runs = self.capture(OMNI.print_runs, [{"id": "run-1", "agent": "A",
                                               "state": "failed", "started": 0}])
        self.assertIn("run-1", runs)
        self.assertIn("failed", runs)

    def test_only_real_failures_are_red(self):
        self.assertEqual(OMNI.RUN_STATE_COLORS["failed"], "red")
        self.assertEqual(OMNI.RUN_STATE_COLORS["completed"], "green")
        self.assertEqual(OMNI.RUN_STATE_COLORS["interrupted"], "peach")
        self.assertNotIn("closed", OMNI.RUN_STATE_COLORS)

    def test_a_live_run_that_finished_its_turn_is_listed_as_idle(self):
        rows = [{"id": "run-1", "agent": "A", "state": "running", "turn_finished": 1, "started": 1},
                {"id": "run-2", "agent": "A", "state": "running", "turn_finished": 0, "started": 1},
                {"id": "run-3", "agent": "A", "state": "completed", "turn_finished": 1, "started": 1}]
        self.assertEqual([OMNI.run_state_label(run) for run in rows], ["idle", "running", "completed"])
        self.assertEqual(OMNI.run_state_label({"state": "evicted", "turn_finished": 0}), "evicted")
        self.assertEqual(OMNI.run_state_label({"state": "evicted"}), "ended")
        listing = self.capture(OMNI.print_runs, rows)
        self.assertIn("idle", listing.splitlines()[1])
        self.assertIn("running", listing.splitlines()[2])

    def test_preview_schedule_prints_five_recurring_occurrences(self):
        start = dt.datetime(2026, 8, 2, 9, 0,
                            tzinfo=OMNI.ZoneInfo("UTC")).timestamp()
        responses = [{"agent": {"next_run": start,
                                  "schedule": {"kind": "interval",
                                               "interval_seconds": 60}}},
                     {"settings": {"timezone": "UTC"}}]
        with mock.patch.object(OMNI, "request", side_effect=responses):
            lines = self.capture(OMNI.preview_schedule, "A").splitlines()
        self.assertEqual(len(lines), 5)
        self.assertEqual(lines[-1], "2026-08-02T09:04:00+00:00")

    def test_main_routes_agent_read_run_and_schedule_actions(self):
        agent = {"name": "A"}
        with mock.patch.object(OMNI, "request",
                               return_value={"agent": agent}) as request, \
                mock.patch.object(OMNI, "print_agent") as print_agent:
            OMNI.main(["agent", "A", "show", "--json"])
        request.assert_called_once_with({"op": "show_agent", "name": "A"})
        print_agent.assert_called_once_with(agent, True)

        with mock.patch.object(OMNI, "request",
                               return_value={"runs": [{"id": "1"}]}) as request, \
                mock.patch.object(OMNI, "print_runs") as print_runs:
            OMNI.main(["agent", "A", "runs", "--json"])
        request.assert_called_once_with({"op": "list_runs", "name": "A"})
        print_runs.assert_called_once_with([{"id": "1"}], True)

        with mock.patch.object(OMNI, "preview_schedule") as preview:
            OMNI.main(["agent", "A", "schedule"])
            OMNI.main(["schedule", "preview", "A"])
        self.assertEqual(preview.call_args_list, [mock.call("A"), mock.call("A")])

    def test_main_routes_top_level_and_mutating_commands(self):
        responses = {"run_agent": {"run_id": "new"}, "list_runs": {"runs": []},
                     "get_settings": {"settings": {"timezone": "UTC"}}}
        with mock.patch.object(
                OMNI, "request",
                side_effect=lambda payload: responses.get(payload["op"], {"ok": True})) as request, \
                redirect_stdout(io.StringIO()):
            OMNI.main(["run", "A"])
            OMNI.main(["runs", "--agent", "A", "--json"])
            OMNI.main(["stop", "run-1"])
            OMNI.main(["settings"])
            OMNI.main(["agent", "A", "disable"])
            OMNI.main(["agent", "A", "delete"])
        payloads = [call.args[0] for call in request.call_args_list]
        self.assertEqual(payloads, [
            {"op": "run_agent", "name": "A"},
            {"op": "list_runs", "name": "A"},
            {"op": "stop_run", "run_id": "run-1"},
            {"op": "get_settings"},
            {"op": "set_enabled", "name": "A", "enabled": False},
            {"op": "delete_agent", "name": "A"},
        ])

    def test_main_routes_dashboard_editor_and_mcp_commands(self):
        with mock.patch.object(OMNI, "dashboard_supervisor") as dashboard:
            OMNI.main([])
            OMNI.main(["dashboard"])
        self.assertEqual(dashboard.call_count, 2)

        with mock.patch.object(OMNI, "edit_agent") as edit:
            OMNI.main(["agents", "add", "/tmp/project"])
        edit.assert_called_once_with(seed_path="/tmp/project")

        agent = {"name": "A"}
        with mock.patch.object(OMNI, "request", return_value={"agent": agent}), \
                mock.patch.object(OMNI, "edit_agent") as edit:
            OMNI.main(["agent", "A", "edit"])
        edit.assert_called_once_with(agent)

        with mock.patch.object(OMNI, "install_mcp") as install, \
                mock.patch.object(OMNI, "mcp_status") as status, \
                mock.patch.object(OMNI, "mcp_round_trip", return_value={"ok": True}) as doctor, \
                mock.patch.object(OMNI, "uninstall_mcp") as uninstall, \
                redirect_stdout(io.StringIO()):
            OMNI.main(["mcp", "install", "--quiet"])
            OMNI.main(["mcp", "status", "--json"])
            OMNI.main(["mcp", "doctor"])
            OMNI.main(["mcp", "uninstall"])
        install.assert_called_once_with(True)
        status.assert_called_once_with(True)
        doctor.assert_called_once_with()
        uninstall.assert_called_once_with()


RUN_ID = "1b4e28ba-2fa1-11d2-883f-0016d3cca427"
REPORT = {"id": RUN_ID, "agent": "Nightly", "state": "completed", "reason": "exit 0",
          "started": 1, "finished": 2, "origin": "scheduled", "workspace": "/work", "provider": "claude",
          "transcript_path": "/t.jsonl", "branch": "fix/build", "head_start": "aaaaaaaaaaaaaaaa",
          "head_end": "bbbbbbbbbbbbbbbb", "commits": [{"sha": "bbbbbbbbbbbbbbbb", "subject": "Fix the build"}],
          "final_message": "I fixed the build.", "summary": "- Fixed the build\n- Tests pass",
          "summary_state": "ready", "diff_state": "ready", "diff_files": ["src/a.py", "src/b.py"],
          "pr_url": "https://example.test/pr/4"}


class RunReportCLITests(unittest.TestCase):
    def run_cli(self, argv, report=REPORT, diff=None):
        responses = {"get_run": {"ok": True, "run": report},
                     "run_diff": diff or {"ok": True, "diff": "", "diff_state": "none", "diff_files": [],
                                          "truncated": False}}
        output = io.StringIO()
        with mock.patch.object(OMNI, "request", side_effect=lambda payload: responses[payload["op"]]) as request, \
                redirect_stdout(output):
            OMNI.main(argv)
        return output.getvalue(), [call.args[0] for call in request.call_args_list]

    def test_report_shows_the_summary_and_what_the_run_changed(self):
        output, payloads = self.run_cli(["run", RUN_ID])
        self.assertEqual(payloads, [{"op": "get_run", "id": RUN_ID}])
        for text in ("Nightly", "completed · exit 0", "/work", "fix/build", "aaaaaaaaaaaa..bbbbbbbbbbbb",
                     "Summary", "  - Tests pass", "Commits (1)", "Fix the build",
                     "Changed files (2)", "  src/b.py", "https://example.test/pr/4"):
            self.assertIn(text, output)
        self.assertNotIn("I fixed the build.", output)

    def test_report_falls_back_to_the_labelled_final_message(self):
        for state in ("failed", "pending", None):
            with self.subTest(state=state):
                report = {**REPORT, "summary": None, "summary_state": state, "commits": [],
                          "diff_files": [], "diff_state": "none", "pr_url": None}
                output, _ = self.run_cli(["run", RUN_ID], report)
                self.assertIn(f"Final message (AI summary {state or 'not available'})", output)
                self.assertIn("  I fixed the build.", output)
                self.assertIn("Changed files  none (none)", output)
                self.assertNotIn("PR ", output)
        output, _ = self.run_cli(["run", RUN_ID], {"id": RUN_ID, "state": "running"})
        self.assertIn("No summary (AI summary not available) and no final message", output)

    def test_agent_text_cannot_reach_the_terminal_as_escape_sequences(self):
        report = {**REPORT, "summary": "\x1b]0;pwned\x07Done\x1b[2J", "commits": [{"subject": "a\x1b[31mb"}]}
        output, _ = self.run_cli(["run", RUN_ID], report)
        self.assertNotIn("\x1b", output)
        self.assertIn("  Done", output)
        self.assertIn("ab", output)

    def test_diff_flag_prints_the_diff_and_says_when_it_was_cut(self):
        diff = {"ok": True, "diff": "diff --git a/x b/x\n+\tnew\x1b[0m\n", "diff_state": "ready",
                "diff_files": ["x"], "truncated": True}
        output, payloads = self.run_cli(["run", RUN_ID, "--diff"], diff=diff)
        self.assertEqual([payload["op"] for payload in payloads], ["get_run", "run_diff"])
        self.assertIn("Diff (truncated)", output)
        self.assertIn("+\tnew", output)
        self.assertNotIn("\x1b", output)

    def test_json_is_the_scheduler_report_with_the_diff_when_asked(self):
        output, _ = self.run_cli(["run", RUN_ID, "--json"])
        self.assertEqual(json.loads(output), REPORT)
        diff = {"ok": True, "diff": "patch", "diff_state": "ready", "diff_files": [], "truncated": False}
        output, _ = self.run_cli(["run", RUN_ID, "--json", "--diff"], diff=diff)
        value = json.loads(output)
        self.assertEqual((value["diff"], value["diff_truncated"], value["agent"]), ("patch", False, "Nightly"))

    def test_unknown_run_exits_with_the_schedulers_error(self):
        client = mock.Mock()
        client.recv.side_effect = [b'{"ok":false,"code":"run_not_found","error":"Run not found"}', b""]
        with mock.patch.object(OMNI.socket, "socket", return_value=client), \
                self.assertRaisesRegex(SystemExit, "Run not found"):
            OMNI.main(["run", RUN_ID])


class RunScreenCLITests(unittest.TestCase):
    SCREEN = ("\x1b]0;title\x07\x1b[2J\x1b[H\x1b[1;38;2;247;177;133mOmni\x1b[0m ready\r\n"
              "\x1b[?25l$ \x1b[31merror\x1b[m\n")

    def run_cli(self, argv, tty, response=None):
        response = response or {"ok": True, "screen": self.SCREEN, "truncated": False}
        output = io.StringIO()
        output.isatty = lambda: tty
        with mock.patch.object(OMNI, "request", return_value=response) as request, \
                mock.patch.object(OMNI.sys, "stdout", output):
            OMNI.main(argv)
        request.assert_called_once_with({"op": "run_screen", "id": RUN_ID})
        return output.getvalue()

    def test_terminal_keeps_colours_but_no_other_control_sequence(self):
        output = self.run_cli(["run", RUN_ID, "--screen"], tty=True)
        self.assertIn("\x1b[1;38;2;247;177;133mOmni\x1b[0m ready", output)
        self.assertIn("$ \x1b[31merror\x1b[m", output)
        for sequence in ("\x1b]0;", "\x1b[2J", "\x1b[H", "\x1b[?25l", "\r"):
            self.assertNotIn(sequence, output)
        self.assertTrue(output.rstrip("\n").endswith("\x1b[0m"))

    def test_pipes_and_plain_get_no_escapes_at_all(self):
        for argv, tty in (([], False), (["--plain"], True)):
            with self.subTest(argv=argv, tty=tty):
                output = self.run_cli(["run", RUN_ID, "--screen", *argv], tty=tty)
                self.assertNotIn("\x1b", output)
                self.assertEqual(output, "Omni ready\n$ error\n")

    def test_truncation_is_said_and_json_is_plain(self):
        output = self.run_cli(["run", RUN_ID, "--screen"], tty=False,
                              response={"ok": True, "screen": "last\n", "truncated": True})
        self.assertIn("earlier scrollback not kept", output)
        value = json.loads(self.run_cli(["run", RUN_ID, "--screen", "--json"], tty=True))
        self.assertEqual(value, {"screen": "Omni ready\n$ error\n", "truncated": False})

    def test_a_run_without_a_screen_exits_with_the_schedulers_error(self):
        client = mock.Mock()
        client.recv.side_effect = [b'{"ok":false,"code":"screen_not_found","error":"No screen saved"}', b""]
        with mock.patch.object(OMNI.socket, "socket", return_value=client), \
                self.assertRaisesRegex(SystemExit, "No screen saved"):
            OMNI.main(["run", RUN_ID, "--screen"])


SESSION = "0c8a5f2e-1111-4222-8333-944455556666"


def claude_line(kind, content, **fields):
    return {"type": kind, "message": {"role": kind, "content": content}, **fields}


def codex_item(**payload):
    return {"type": "response_item", "payload": payload}


class RunTailTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name).resolve()
        self.claude_root = self.home / ".claude/projects"
        self.codex_root = self.home / ".codex/sessions"
        patcher = mock.patch.object(OMNI, "TRANSCRIPT_ROOTS", (self.claude_root, self.codex_root))
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, path, *events):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(event) + "\n" for event in events))
        return path

    def claude_transcript(self, *events):
        return self.write(self.claude_root / "-work" / f"{SESSION}.jsonl", *events)

    def codex_transcript(self, *events):
        return self.write(self.codex_root / "2026/11/05" / f"rollout-2026-11-05T10-00-00-{SESSION}.jsonl", *events)

    def test_claude_turns_render_like_the_terminal(self):
        path = self.claude_transcript(
            claude_line("user", "Fix the build"),
            claude_line("user", "<system-reminder>injected</system-reminder>"),
            claude_line("user", "meta", isMeta=True),
            claude_line("assistant", [{"type": "thinking", "thinking": "secret plan"},
                                      {"type": "text", "text": "Looking."},
                                      {"type": "tool_use", "name": "Bash",
                                       "input": {"command": "make test " + "x" * 200}}]),
            claude_line("user", [{"type": "tool_result", "content": "line1\nline2\nline3\nline4\nline5\nline6"}]),
            claude_line("assistant", [{"type": "text", "text": "subagent"}], isSidechain=True),
            claude_line("assistant", [{"type": "text", "text": "Fixed.\nAll green."}]),
            {"type": "api-request-blob", "blob": "x" * 50})
        result = OMNI.run_tail({"id": "r", "provider": "claude", "transcript_path": str(path)})
        self.assertTrue(result["ok"])
        self.assertEqual((result["source"], result["path"], result["truncated"]), ("transcript", str(path), False))
        lines = result["text"].splitlines()
        self.assertEqual(lines[0], "❯ Fix the build")
        self.assertIn("● Looking.", lines)
        call = next(line for line in lines if line.startswith("● Bash("))
        self.assertTrue(call.endswith("…)"))
        self.assertLessEqual(len(call), len("● Bash()") + OMNI.TAIL_ARGUMENT_CHARACTERS)
        self.assertIn("  ⎿ line1", lines)
        self.assertIn("    line4", lines)
        self.assertNotIn("    line5", lines)
        self.assertIn("    … +2 lines", lines)
        self.assertEqual(lines[-2:], ["● Fixed.", "  All green."])
        for hidden in ("secret plan", "subagent", "injected", "meta", "\x1b"):
            self.assertNotIn(hidden, result["text"])

    def test_codex_turns_render_like_the_terminal(self):
        self.codex_transcript(
            {"type": "session_meta", "payload": {"id": SESSION, "cwd": "/work"}},
            codex_item(type="message", role="developer", content=[{"type": "input_text", "text": "rules"}]),
            codex_item(type="message", role="user", content=[
                {"type": "input_text", "text": "<environment_context>cwd</environment_context>"},
                {"type": "input_text", "text": "Update the docs"}]),
            codex_item(type="reasoning", summary=[{"type": "summary_text", "text": "thinking"}]),
            codex_item(type="function_call", name="exec_command", arguments=json.dumps({"cmd": "rg TODO"})),
            codex_item(type="function_call_output", output=json.dumps({"output": "a.md:1\nb.md:2"})),
            codex_item(type="custom_tool_call", name="apply_patch", input="*** Begin Patch\n*** Update File: a.md"),
            codex_item(type="custom_tool_call_output", output="Success"),
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "duplicate"}},
            codex_item(type="message", role="assistant", content=[{"type": "output_text", "text": "Docs updated."}]))
        result = OMNI.run_tail({"id": "r", "provider": "codex", "session_id": SESSION})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["text"].splitlines(), [
            "❯ Update the docs", "", "● exec_command(rg TODO)", "  ⎿ a.md:1", "    b.md:2",
            "● apply_patch(*** Begin Patch *** Update File: a.md)", "  ⎿ Success", "● Docs updated."])

    def test_transcript_is_found_from_session_ids_when_no_path_was_recorded(self):
        claude = self.claude_transcript(claude_line("user", "hi"))
        self.assertEqual(OMNI.run_transcript({"id": SESSION, "provider": "claude"}), claude)
        self.assertEqual(OMNI.run_transcript({"id": "r", "provider": None, "resume_session_id": SESSION}), claude)
        codex = self.codex_transcript()
        self.assertEqual(OMNI.run_transcript({"id": "r", "provider": "codex", "session_id": SESSION}), codex)
        self.assertIsNone(OMNI.run_transcript({"id": "r", "provider": "codex", "session_id": "*"}))

    def test_only_provider_transcripts_are_ever_read(self):
        outside = self.write(self.home / "notes.jsonl", claude_line("user", "private"))
        self.claude_transcript(claude_line("user", "real"))
        run = {"id": "r", "provider": "claude", "session_id": SESSION, "transcript_path": str(outside)}
        self.assertEqual(OMNI.run_tail(run)["text"].strip(), "❯ real")
        result = OMNI.run_tail({"id": "r", "transcript_path": str(outside)})
        self.assertEqual(result, {"ok": False, "error": "No transcript found for this run"})

    def test_line_limit_colour_and_a_long_transcript_tail(self):
        events = [claude_line("assistant", [{"type": "text", "text": f"step {index}"}]) for index in range(30)]
        path = self.claude_transcript(*events)
        result = OMNI.run_tail({"transcript_path": str(path)}, count=5)
        self.assertEqual(result["text"].splitlines(), [f"● step {index}" for index in range(25, 30)])
        self.assertTrue(result["truncated"])
        self.assertEqual(len(OMNI.run_tail({"transcript_path": str(path)}, count=99999)["text"].splitlines()), 30)
        with mock.patch.object(OMNI, "TAIL_BYTES", 120), mock.patch.object(OMNI, "TAIL_BYTES_MAX", 480):
            result = OMNI.run_tail({"transcript_path": str(path)}, count=200)
        self.assertTrue(result["truncated"])
        self.assertIn("● step 29", result["text"])
        self.assertNotIn("● step 0", result["text"])
        self.claude_transcript(claude_line("user", [{"type": "tool_result", "content": "out"}]),
                               claude_line("assistant", [{"type": "tool_use", "name": "Read",
                                                         "input": {"file_path": "/a"}}]))
        coloured = OMNI.run_tail({"transcript_path": str(path)}, colour=True)["text"]
        self.assertIn("\x1b[2m  ⎿ out\x1b[0m", coloured)
        self.assertIn("● \x1b[1mRead\x1b[0m(/a)", coloured)

    def cli(self, argv, run=None, tty=False):
        output = io.StringIO()
        output.isatty = lambda: tty
        response = {"ok": True, "run": run} if run is not None else None
        side_effect = (lambda payload: response) if response else SystemExit("OmniTerm scheduler is unavailable")
        with mock.patch.object(OMNI, "request", side_effect=side_effect) as request, \
                mock.patch.object(OMNI.sys, "stdout", output):
            OMNI.main(argv)
        if response:
            request.assert_called_once_with({"op": "get_run", "id": RUN_ID})
        return output.getvalue()

    def test_cli_contract(self):
        path = self.claude_transcript(claude_line("user", "Go"),
                                      claude_line("assistant", [{"type": "tool_use", "name": "Bash",
                                                                "input": {"command": "ls"}}]))
        run = {"id": RUN_ID, "provider": "claude", "transcript_path": str(path)}
        value = json.loads(self.cli(["run", RUN_ID, "--tail", "--json", "--lines", "1"], run))
        self.assertEqual(value, {"ok": True, "text": "● \x1b[1mBash\x1b[0m(ls)", "source": "transcript",
                                 "path": str(path), "truncated": True})
        value = json.loads(self.cli(["run", RUN_ID, "--tail", "--json", "--plain"], run))
        self.assertEqual(value["text"], "❯ Go\n\n● Bash(ls)")
        self.assertEqual(self.cli(["run", RUN_ID, "--tail"], run), "❯ Go\n\n● Bash(ls)\n")
        self.assertIn("\x1b[1m", self.cli(["run", RUN_ID, "--tail"], run, tty=True))
        value = json.loads(self.cli(["run", RUN_ID, "--tail", "--json"], {"id": RUN_ID, "provider": "codex"}))
        self.assertEqual(value, {"ok": False, "error": "No transcript found for this run"})
        value = json.loads(self.cli(["run", RUN_ID, "--tail", "--json"]))
        self.assertEqual(value, {"ok": False, "error": "OmniTerm scheduler is unavailable"})
        with self.assertRaisesRegex(SystemExit, "No transcript"):
            self.cli(["run", RUN_ID, "--tail"], {"id": RUN_ID})


APPEARANCE_FIXTURE = json.loads((Path(__file__).parent / "fixtures/agent_appearance.json").read_text())


class FolderCLITests(unittest.TestCase):
    def test_set_normalizes_deduplicates_and_prints_the_saved_list(self):
        output = io.StringIO()
        with mock.patch.object(OMNI, "request", return_value={"ok": True, "groups": ["Saved", "Empty"]}) as request, \
                redirect_stdout(output):
            OMNI.main(["folders", "set", " Work / Docs ", "Work/Docs", "Empty"])
        request.assert_called_once_with({"op": "set_sidebar_groups", "groups": ["Work/Docs", "Empty"]})
        self.assertEqual(output.getvalue().splitlines(), ["Saved", "Empty"])

    def test_set_json_returns_the_saved_folder_array(self):
        output = io.StringIO()
        with mock.patch.object(OMNI, "request", return_value={"ok": True, "groups": ["Équipe", "Empty"]}) as request, \
                redirect_stdout(output):
            OMNI.main(["folders", "set", "Équipe", "Empty", "--json"])
        request.assert_called_once_with({"op": "set_sidebar_groups", "groups": ["Équipe", "Empty"]})
        self.assertEqual(json.loads(output.getvalue()), ["Équipe", "Empty"])

    def test_invalid_folder_paths_make_no_request(self):
        for path in ("", "   ", "A/B/C", "/A", "A//B", "A\x07B", "x" * 41):
            with self.subTest(path=path), mock.patch.object(OMNI, "request") as request, \
                    self.assertRaises(SystemExit):
                OMNI.main(["folders", "set", "Valid", path])
            request.assert_not_called()

    def test_set_propagates_scheduler_failure_without_success_output(self):
        output = io.StringIO()
        with mock.patch.object(OMNI, "request", side_effect=SystemExit("Could not write settings")) as request, \
                redirect_stdout(output), self.assertRaisesRegex(SystemExit, "Could not write settings"):
            OMNI.main(["folders", "set", "Work"])
        request.assert_called_once_with({"op": "set_sidebar_groups", "groups": ["Work"]})
        self.assertEqual(output.getvalue(), "")

    def test_missing_paths_or_unknown_actions_are_rejected_before_request(self):
        for arguments in (["folders"], ["folders", "set"], ["folders", "rename", "Work"]):
            with self.subTest(arguments=arguments), mock.patch.object(OMNI, "request") as request, \
                    redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                OMNI.main(arguments)
            self.assertEqual(error.exception.code, 2)
            request.assert_not_called()


class AgentAppearanceTests(unittest.TestCase):
    def test_shared_fixture_matches_the_validators(self):
        for field, normalizer in OMNI.APPEARANCE_NORMALIZERS.items():
            for value in APPEARANCE_FIXTURE[field]["valid"]:
                with self.subTest(field=field, value=value):
                    normalizer(value)
            for value in APPEARANCE_FIXTURE[field]["invalid"]:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    normalizer(value)
        for value, expected in APPEARANCE_FIXTURE["group_normalized"]:
            with self.subTest(group=value):
                self.assertEqual(OMNI.normalized_group(value), expected)
        for value in ("", "   ", None):
            self.assertIsNone(OMNI.normalized_group(value))
        for value in ("", None):
            self.assertIsNone(OMNI.normalized_icon(value))
            self.assertIsNone(OMNI.normalized_color(value))
        for value in APPEARANCE_FIXTURE["name"]["invalid"]:
            with self.subTest(name=value), self.assertRaises(ValueError):
                OMNI.normalized_agent_name(value)
        for value in APPEARANCE_FIXTURE["name"]["valid"]:
            with self.subTest(name=value):
                self.assertEqual(OMNI.normalized_agent_name(value), value.strip())
        self.assertEqual(OMNI.normalized_agent_name("  " + "x" * 100 + " "), "x" * 100)

    def test_emoji_is_one_pictographic_grapheme(self):
        for text in ("中", "©️", "🇫", "🏽", "e\u0301\u2000"):
            with self.subTest(text=text):
                self.assertEqual(OMNI.is_single_emoji(text), text in ("中", "©️", "🇫", "🏽"))
        self.assertFalse(OMNI.is_single_emoji("👨‍👩‍👧‍👦" * 3))

    def test_lists_are_the_contract(self):
        self.assertGreaterEqual(len(OMNI.AGENT_ICONS), 300)
        self.assertEqual(len(set(OMNI.AGENT_ICONS)), len(OMNI.AGENT_ICONS), "no icon is listed twice")
        for name in OMNI.AGENT_ICONS:
            self.assertIsNotNone(OMNI.normalized_icon("sf:" + name), name)
        self.assertEqual(len(OMNI.AGENT_COLORS), 16)
        self.assertEqual(set(OMNI.AGENT_COLORS), set(APPEARANCE_FIXTURE["color"]["valid"]) - {""})
        self.assertEqual(set(OMNI.AGENT_COLOR_RGB), set(OMNI.AGENT_COLORS), "every colour has a terminal colour")
        output = io.StringIO()
        with redirect_stdout(output), mock.patch.object(OMNI, "request") as request:
            OMNI.main(["agents", "appearance", "--json"])
        request.assert_not_called()
        catalog = json.loads(output.getvalue())
        self.assertEqual([icon["name"] for icon in catalog["icons"]], list(OMNI.AGENT_ICONS))
        self.assertEqual(catalog["colors"], list(OMNI.AGENT_COLORS))
        ant = next(icon for icon in catalog["icons"] if icon["name"] == "ant")
        self.assertEqual(ant["category"], "Development")
        self.assertIn("bug", ant["keywords"])

    def test_every_catalog_icon_exists_on_macos_13(self):
        availability = Path("/System/Library/CoreServices/CoreGlyphs.bundle/Contents/Resources/name_availability.plist")
        if not availability.exists():
            self.skipTest("SF Symbols availability data is not on this machine")
        data = plistlib.loads(availability.read_bytes())
        for name in OMNI.AGENT_ICONS:
            with self.subTest(name=name):
                year = data["symbols"].get(name)
                self.assertIsNotNone(year, "unknown SF Symbol")
                major, _, minor = data["year_to_release"][year]["macOS"].partition(".")
                self.assertLessEqual((int(major), int(minor or 0)), (13, 0))

    def test_editor_keeps_an_agents_appearance_and_absent_means_none(self):
        with tempfile.TemporaryDirectory() as workspace:
            values = {"name": "A", "provider": "codex", "workspace": workspace, "system_prompt": "",
                      "startup_prompt": "", "model": "", "schedule_phrase": "daily at 09:00", "overlap": "replace",
                      "timeout": "off", "permission_mode": "", "advanced_args": ""}
            old = {"name": "A", "group": "Work/Code", "icon": "sf:hammer", "color": "blue"}
            agent = OMNI.normalized_agent(dict(values), old, "UTC")
            self.assertEqual((agent["group"], agent["icon"], agent["color"]), ("Work/Code", "sf:hammer", "blue"))
            agent = OMNI.normalized_agent({**values, "group": "", "color": None}, old, "UTC")
            self.assertNotIn("group", agent)
            self.assertNotIn("color", agent)
            self.assertEqual(agent["icon"], "sf:hammer")
            with self.assertRaisesRegex(ValueError, "two levels"):
                OMNI.normalized_agent({**values, "group": "A/B/C"}, old, "UTC")

    def test_set_validates_then_sends_canonical_values_and_empty_clears(self):
        with mock.patch.object(OMNI, "request", return_value={"ok": True}) as request:
            OMNI.main(["agent", "A", "set", "--group", " Work / Code ", "--icon", "", "--color", "red"])
        request.assert_called_once_with({"op": "set_agent_appearance", "name": "A",
                                         "group": "Work/Code", "icon": "", "color": "red"})
        for argv, message in ((["--color", "beige"], "Color"), (["--icon", "sf:Trash"], "Icon"), ([], "Give")):
            with self.subTest(argv=argv), mock.patch.object(OMNI, "request") as request, \
                    self.assertRaisesRegex(SystemExit, message):
                OMNI.main(["agent", "A", "set", *argv])
            request.assert_not_called()

    def test_rename_calls_the_scheduler(self):
        output = io.StringIO()
        with mock.patch.object(OMNI, "request", return_value={"ok": True, "agent": {"name": "B"}}) as request, \
                redirect_stdout(output):
            OMNI.main(["agent", "A", "rename", "  B "])
        request.assert_called_once_with({"op": "rename_agent", "name": "A", "new_name": "B"})
        self.assertEqual(output.getvalue().strip(), "B")
        for argv, message in ((["rename"], "new name"), (["rename", "x" * 101], "1-100")):
            with mock.patch.object(OMNI, "request") as request, self.assertRaisesRegex(SystemExit, message):
                OMNI.main(["agent", "A", *argv])
            request.assert_not_called()

    def agents(self):
        return [{"name": name, "provider": "codex", "enabled": True, "schedule": {"phrase": "daily at 09:00"}, **fields}
                for name, fields in (("Loose", {}), ("Deploy", {"group": "Work", "icon": "emoji:🚀", "color": "red"}),
                                     ("Review", {"group": "Work/Code", "icon": "sf:hammer"}),
                                     ("Notes", {"group": "Home", "color": "green"}))]

    def listing(self, tty, order=None):
        output = io.StringIO()
        with redirect_stdout(output), \
                mock.patch.object(OMNI, "stored_settings", return_value={"sidebar_groups": order} if order else {}):
            OMNI.print_agents(self.agents(), tty=tty)
        return output.getvalue().splitlines()

    def test_list_shows_sections_and_subsections(self):
        lines = self.listing(tty=False)
        self.assertEqual([line[:29].rstrip() for line in lines[1:]],
                         ["Loose", "Home", "  Notes", "Work", "  Deploy", "  Code", "    Review"])
        self.assertTrue(all(line.index("codex") == 29 for line in lines if "codex" in line))
        self.assertNotIn("\x1b", "".join(lines))
        ordered = self.listing(tty=False, order=["Work", "Work/Code", "Home"])
        self.assertEqual([line.strip()[:6] for line in ordered[2:4]], ["Work", "Deploy"])

    def test_agents_follow_the_sidebar_order_within_each_group(self):
        agents = [{"name": name, "provider": "codex", "enabled": True, "schedule": {"phrase": "daily at 09:00"},
                   **({"group": group} if group else {})}
                  for name, group in (("beta", "Work"), ("Alpha", "Work"), ("Gamma", "Work"), ("Delta", "Work"),
                                      ("zed", ""), ("Ace", ""))]
        output = io.StringIO()
        settings = {"sidebar_groups": ["Work"], "sidebar_agent_order": ["Gamma", "zed", "Missing", "beta", 7]}
        with redirect_stdout(output), mock.patch.object(OMNI, "stored_settings", return_value=settings):
            OMNI.print_agents(agents, tty=False)
        names = [line[:29].strip() for line in output.getvalue().splitlines()[1:]]
        self.assertEqual(names, ["zed", "Ace", "Work", "Gamma", "beta", "Alpha", "Delta"])

    def test_sidebar_order_interleaves_groups_and_agents_at_each_level(self):
        agents = [{"name": name, "provider": "codex", "enabled": True, "schedule": {"phrase": "daily at 09:00"},
                   **({"group": group} if group else {})}
                  for name, group in (("Loose", ""), ("Zulu", ""), ("Bravo", ""), ("Deploy", "Work"),
                                      ("Review", "Work/Code"), ("Lint", "Work/Code"), ("Notes", "Home"),
                                      ("Misc", "Work"), ("Old", "Archive"))]
        order = ["agent:Zulu", "group:Work", "agent:Loose", "group:Empty", "group:Work/Code", "agent:Deploy",
                 "agent:Lint", "agent:Missing", 7]
        output = io.StringIO()
        with redirect_stdout(output), mock.patch.object(OMNI, "stored_settings", return_value={
                "sidebar_order": order, "sidebar_groups": ["Home"], "sidebar_agent_order": ["Bravo"]}):
            OMNI.print_agents(agents, tty=False)
        rows = [line[:29].rstrip() for line in output.getvalue().splitlines()[1:]]
        self.assertEqual(rows, ["Zulu", "Work", "  Code", "    Lint", "    Review", "  Deploy", "  Misc", "Loose",
                                "Archive", "  Old", "Home", "  Notes", "Bravo"])

    def test_order_command_sets_the_sidebar_order(self):
        with mock.patch.object(OMNI, "request", return_value={"ok": True}) as request:
            OMNI.main(["agents", "order", "B", " A ", "B", "C"])
        request.assert_called_once_with({"op": "set_sidebar_agent_order", "names": ["B", "A", "C"]})
        with mock.patch.object(OMNI, "request") as request, self.assertRaisesRegex(SystemExit, "names"):
            OMNI.main(["agents", "order", " "])
        request.assert_not_called()

    def test_terminal_list_shows_icon_and_colour(self):
        lines = self.listing(tty=True)
        deploy = next(line for line in lines if "Deploy" in line)
        self.assertIn("\x1b[38;2;255;105;97m🚀", deploy)
        review = next(line for line in lines if "Review" in line)
        self.assertIn("● Review", review)
        self.assertTrue(all(OMNI.display_width(line[:line.index("codex")]) == 29 for line in lines if "codex" in line))

    def test_normalize_accepts_and_returns_appearance(self):
        with tempfile.TemporaryDirectory() as workspace:
            base = {"name": "A", "provider": "codex", "workspace": workspace, "schedule_phrase": "daily at 09:00"}
            result = OMNI.normalize_agent_json({**base, "group": "Work / Code", "icon": "emoji:🚀", "color": "pink"},
                                               {"timezone": "UTC"})
            self.assertEqual({key: result["agent"][key] for key in ("group", "icon", "color")},
                             {"group": "Work/Code", "icon": "emoji:🚀", "color": "pink"})
            result = OMNI.normalize_agent_json({**base, "group": "", "icon": None}, {"timezone": "UTC"})
            self.assertFalse(set(OMNI.APPEARANCE_FIELDS) & set(result["agent"]))
            result = OMNI.normalize_agent_json({**base, "group": "a/b/c", "icon": "sf:Trash", "color": "Red"},
                                               {"timezone": "UTC"})
            self.assertEqual(set(result["errors"]), {"group", "icon", "color"})


class EditorHelperJSONTests(unittest.TestCase):
    SETTINGS = {"timezone": "UTC", "default_timeout_seconds": 3600}

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.workspace = str(Path(directory.name).resolve())
        patcher = mock.patch.object(OMNI, "request", side_effect=AssertionError("must not contact the scheduler"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def normalize(self, **fields):
        candidate = {"name": "Nightly", "provider": "codex", "workspace": self.workspace,
                     "schedule_phrase": "daily at 09:00", **fields}
        return OMNI.normalize_agent_json(candidate, self.SETTINGS)

    def test_loop_normalization_keeps_session_even_when_fresh_was_requested(self):
        result = self.normalize(kind="loop", overlap="replace", session_policy="fresh", compact_on_end=True)
        self.assertTrue(result["ok"], result)
        agent = result["agent"]
        self.assertEqual(agent["overlap"], "parallel")
        self.assertEqual(agent["timeout_seconds"], 86400)
        self.assertEqual(agent["session_policy"], "resume")
        self.assertEqual(agent["run_budget_seconds"], 600)
        self.assertTrue(agent["compact_on_end"])

    def test_loop_has_no_overlap_or_session_replacement_choices(self):
        self.assertEqual(OMNI.agent_form_choices("overlap", {"kind": "loop"}), [])
        self.assertEqual(OMNI.agent_form_choices("session_policy", {"kind": "loop"}), [])
        self.assertEqual(OMNI.agent_form_choices("overlap", {"kind": "scheduled"}), ["replace", "parallel"])

    def test_normal_agent_can_reuse_without_changing_overlap_or_retention(self):
        result = self.normalize(session_policy="resume", compact_on_end=True, timeout="4h")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["agent"]["kind"], "scheduled")
        self.assertEqual(result["agent"]["overlap"], "replace")
        self.assertEqual(result["agent"]["timeout_seconds"], 14400)
        self.assertEqual(result["agent"]["session_policy"], "resume")

    def cli(self, argv, stdin=""):
        output = io.StringIO()
        with mock.patch.object(OMNI.sys, "stdin", io.StringIO(stdin)), \
                mock.patch.object(OMNI, "stored_settings", return_value=self.SETTINGS), \
                redirect_stdout(output):
            OMNI.main(argv)
        return json.loads(output.getvalue())

    def test_schedule_parse_gives_the_canonical_schedule_and_occurrences(self):
        result = self.cli(["schedule", "parse", "--json", "Every 2 Hours at minute 15", "--count", "3"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["phrase"], "every 2 hours at minute 15")
        self.assertEqual(result["schedule"]["kind"], "calendar_interval")
        self.assertEqual(result["timezone"], "UTC")
        self.assertEqual(len(result["occurrences"]), 3)
        self.assertEqual(result["occurrences"][0], result["next_run"])
        self.assertEqual(result["occurrences"][1] - result["occurrences"][0], 7200)

    def test_schedule_parse_reports_an_invalid_phrase_as_data(self):
        for phrase in ("sometimes", "every 0 days", "once at 2000-01-01 09:00", "weekly on funday at 9am"):
            with self.subTest(phrase=phrase):
                result = self.cli(["schedule", "parse", "--json", phrase])
                self.assertFalse(result["ok"])
                self.assertTrue(result["error"])

    def test_once_schedule_stops_after_one_occurrence(self):
        result = OMNI.parse_schedule_json("once at 2999-01-01 09:00", 5, self.SETTINGS)
        self.assertEqual(len(result["occurrences"]), 1)

    def test_valid_agent_becomes_the_editors_canonical_agent(self):
        result = self.normalize(schedule_phrase=None, schedule={"phrase": "weekdays at 09:00"},
                                advanced_args=["--search"], enabled=False)
        self.assertTrue(result["ok"], result)
        agent = result["agent"]
        self.assertEqual(agent["schedule"]["kind"], "weekdays")
        self.assertIsInstance(agent["next_run"], float)
        self.assertEqual((agent["workspace"], agent["timeout_seconds"], agent["enabled"]),
                         (self.workspace, 3600, False))
        self.assertEqual(agent["advanced_args"], ["--search"])
        self.assertFalse(result["dangerous"])
        self.assertEqual(set(agent), {"name", "provider", "workspace", "system_prompt", "startup_prompt", "model",
                                      "schedule", "next_run", "overlap", "timeout_seconds", "permission_mode",
                                      "advanced_args", "enabled", "kind", "session_policy", "compact_on_end",
                                      "run_budget_seconds", "wrap_up_seconds"})

    def test_cli_reads_stdin_and_reports_bad_json(self):
        candidate = {"provider": "claude", "workspace": self.workspace, "schedule_phrase": "every 30 minutes"}
        result = self.cli(["agents", "normalize", "--json"], json.dumps(candidate))
        self.assertTrue(result["ok"])
        self.assertEqual(result["agent"]["name"], Path(self.workspace).name)
        result = self.cli(["agents", "normalize", "--json"], "{nope")
        self.assertFalse(result["ok"])
        self.assertIn("Invalid JSON", result["errors"]["agent"])

    def test_errors_are_reported_per_field(self):
        result = self.normalize(workspace=self.workspace + "/missing", schedule_phrase="sometimes",
                                overlap="queue", advanced_args=["--model=x"], system_prompt=3)
        self.assertFalse(result["ok"])
        self.assertEqual(set(result["errors"]),
                         {"workspace", "schedule", "overlap", "advanced_args", "system_prompt"})
        self.assertIn("does not exist", result["errors"]["workspace"])
        self.assertIn("schedule", self.normalize(schedule_phrase=None)["errors"])

    def test_permission_modes_follow_the_provider(self):
        cases = [("claude", "auto-mode", "auto"), ("claude", "auto", "auto"), ("claude", "default", ""),
                 ("codex", "on-request", "on-request"), ("codex", "", "")]
        for provider, mode, stored in cases:
            with self.subTest(provider=provider, mode=mode):
                result = self.normalize(provider=provider, permission_mode=mode)
                self.assertEqual(result["agent"]["permission_mode"], stored)
        for provider, mode in (("claude", "dangerous"), ("codex", "bypassPermissions"), ("codex", "plan")):
            with self.subTest(provider=provider, mode=mode):
                self.assertIn("permission_mode", self.normalize(provider=provider, permission_mode=mode)["errors"])
        self.assertIn("provider", self.normalize(provider="gemini")["errors"])

    def test_a_person_may_choose_dangerous_modes_and_is_told(self):
        for provider, mode in (("claude", "bypassPermissions"), ("codex", "dangerous")):
            with self.subTest(provider=provider):
                result = self.normalize(provider=provider, permission_mode=mode)
                self.assertTrue(result["ok"])
                self.assertTrue(result["dangerous"])

    def test_timeouts_parse_like_the_editor(self):
        cases = [({"timeout": "30m"}, 1800), ({"timeout": "off"}, None), ({"timeout": None}, None),
                 ({"timeout_seconds": 600}, 600), ({"timeout_seconds": None}, None), ({}, 3600)]
        for fields, expected in cases:
            with self.subTest(fields=fields):
                self.assertEqual(self.normalize(**fields)["agent"]["timeout_seconds"], expected)
        for fields in ({"timeout": "soon"}, {"timeout": "0.001s"}, {"timeout_seconds": "600"},
                       {"timeout_seconds": True}):
            with self.subTest(fields=fields):
                self.assertIn("timeout", self.normalize(**fields)["errors"])

    def test_settings_come_from_the_file_not_the_scheduler(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            with mock.patch.object(OMNI, "SETTINGS_FILE", path):
                self.assertEqual(OMNI.stored_settings(), {})
                path.write_text(json.dumps({"timezone": "Asia/Tokyo"}))
                self.assertEqual(OMNI.parse_schedule_json("daily at 09:00")["timezone"], "Asia/Tokyo")
                path.write_text("[1]")
                self.assertEqual(OMNI.stored_settings(), {})


class MCPRegistrationTests(unittest.TestCase):
    def test_provider_state_distinguishes_missing_owned_and_conflicting_servers(self):
        launcher = Path("/tmp/omni-mcp")
        results = [
            mock.Mock(returncode=1, stdout="not found\n"),
            mock.Mock(returncode=0, stdout=f"command: {launcher}\n"),
            mock.Mock(returncode=0, stdout="command: /other/server\n"),
        ]
        with mock.patch.object(OMNI, "mcp_launcher_path", return_value=launcher), \
                mock.patch.object(OMNI.subprocess, "run", side_effect=results):
            self.assertEqual(OMNI.provider_mcp_state("claude")["state"], "missing")
            self.assertEqual(OMNI.provider_mcp_state("claude")["state"], "installed")
            self.assertEqual(OMNI.provider_mcp_state("codex")["state"], "conflict")

    def test_install_requires_launcher_and_does_not_overwrite_conflicts(self):
        with tempfile.TemporaryDirectory() as root:
            missing = Path(root) / "missing"
            with mock.patch.object(OMNI, "mcp_launcher_path", return_value=missing), \
                    self.assertRaisesRegex(SystemExit, "Missing MCP launcher"):
                OMNI.install_mcp(quiet=True)

            launcher = Path(root) / "omni-mcp"
            launcher.write_text("#!/bin/sh\n")
            states = iter([
                {"provider": "claude", "state": "missing", "detail": ""},
                {"provider": "claude", "state": "installed", "detail": ""},
                {"provider": "codex", "state": "conflict", "detail": "other"},
            ])
            with mock.patch.object(OMNI, "mcp_launcher_path", return_value=launcher), \
                    mock.patch.object(OMNI.shutil, "which", return_value="/bin/provider"), \
                    mock.patch.object(OMNI, "provider_mcp_state",
                                      side_effect=lambda provider: next(states)), \
                    mock.patch.object(OMNI.subprocess, "run",
                                      return_value=mock.Mock(returncode=0, stdout="ok")) as run:
                results = OMNI.install_mcp(quiet=True)
        self.assertEqual([item["state"] for item in results], ["installed", "conflict"])
        run.assert_called_once_with(
            ["claude", "mcp", "add", "--scope", "user", "omni", "--", str(launcher)],
            text=True, stdout=OMNI.subprocess.PIPE, stderr=OMNI.subprocess.STDOUT)

    def test_uninstall_removes_only_owned_provider_registration(self):
        states = [
            {"provider": "claude", "state": "installed", "detail": ""},
            {"provider": "codex", "state": "conflict", "detail": "other"},
        ]
        with mock.patch.object(OMNI.shutil, "which", return_value="/bin/provider"), \
                mock.patch.object(OMNI, "provider_mcp_state", side_effect=states), \
                mock.patch.object(OMNI.subprocess, "run",
                                  return_value=mock.Mock(returncode=0, stdout="")) as run, \
                redirect_stdout(io.StringIO()):
            OMNI.uninstall_mcp()
        run.assert_called_once_with(
            ["claude", "mcp", "remove", "--scope", "user", "omni"],
            text=True, stdout=OMNI.subprocess.PIPE, stderr=OMNI.subprocess.STDOUT)

    def test_uninstall_command_fails_after_attempting_both_owned_registrations(self):
        for returncodes in ((49, 0), (0, 49), (49, 50)):
            with self.subTest(returncodes=returncodes):
                states = [{"provider": provider, "state": "installed", "detail": ""}
                          for provider in ("claude", "codex")]
                results = [mock.Mock(returncode=returncode, stdout="provider result")
                           for returncode in returncodes]
                with mock.patch.object(OMNI.shutil, "which", return_value="/bin/provider"), \
                        mock.patch.object(OMNI, "provider_mcp_state", side_effect=states), \
                        mock.patch.object(OMNI.subprocess, "run", side_effect=results) as run, \
                        mock.patch.object(OMNI, "mcp_launcher_path") as launcher, \
                        mock.patch.object(OMNI, "request") as request, \
                        redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit) as failure:
                        OMNI.main(["mcp", "uninstall"])
                self.assertEqual(failure.exception.code, 1)
                self.assertEqual(run.call_args_list, [
                    mock.call(["claude", "mcp", "remove", "--scope", "user", "omni"],
                              text=True, stdout=OMNI.subprocess.PIPE, stderr=OMNI.subprocess.STDOUT),
                    mock.call(["codex", "mcp", "remove", "omni"],
                              text=True, stdout=OMNI.subprocess.PIPE, stderr=OMNI.subprocess.STDOUT),
                ])
                launcher.assert_not_called()
                request.assert_not_called()

    def test_uninstall_command_succeeds_when_both_owned_registrations_are_removed(self):
        states = [{"provider": provider, "state": "installed", "detail": ""}
                  for provider in ("claude", "codex")]
        with mock.patch.object(OMNI.shutil, "which", return_value="/bin/provider"), \
                mock.patch.object(OMNI, "provider_mcp_state", side_effect=states), \
                mock.patch.object(OMNI.subprocess, "run",
                                  return_value=mock.Mock(returncode=0, stdout="")) as run, \
                mock.patch.object(OMNI, "mcp_launcher_path") as launcher, \
                mock.patch.object(OMNI, "request") as request, \
                redirect_stdout(io.StringIO()):
            self.assertIsNone(OMNI.main(["mcp", "uninstall"]))
        self.assertEqual(run.call_args_list, [
            mock.call(["claude", "mcp", "remove", "--scope", "user", "omni"],
                      text=True, stdout=OMNI.subprocess.PIPE, stderr=OMNI.subprocess.STDOUT),
            mock.call(["codex", "mcp", "remove", "omni"],
                      text=True, stdout=OMNI.subprocess.PIPE, stderr=OMNI.subprocess.STDOUT),
        ])
        launcher.assert_not_called()
        request.assert_not_called()

    def test_uninstall_command_preserves_missing_conflicting_and_unavailable_providers(self):
        for provider_states in (("missing", "conflict"), ("conflict", "missing"),
                                ("unavailable", "installed"), ("installed", "unavailable"),
                                ("unavailable", "unavailable")):
            with self.subTest(provider_states=provider_states):
                states = dict(zip(("claude", "codex"), provider_states))
                with mock.patch.object(OMNI.shutil, "which", side_effect=lambda provider:
                                       None if states[provider] == "unavailable" else "/bin/provider"), \
                        mock.patch.object(OMNI, "provider_mcp_state", side_effect=lambda provider:
                                          {"provider": provider, "state": states[provider], "detail": ""}) as state, \
                        mock.patch.object(OMNI.subprocess, "run",
                                          return_value=mock.Mock(returncode=0, stdout="")) as run, \
                        mock.patch.object(OMNI, "mcp_launcher_path") as launcher, \
                        mock.patch.object(OMNI, "request") as request, \
                        redirect_stdout(io.StringIO()):
                    self.assertIsNone(OMNI.main(["mcp", "uninstall"]))
                self.assertEqual(state.call_args_list, [mock.call(provider) for provider in states
                                                       if states[provider] != "unavailable"])
                commands = {"claude": ["claude", "mcp", "remove", "--scope", "user", "omni"],
                            "codex": ["codex", "mcp", "remove", "omni"]}
                self.assertEqual(run.call_args_list, [
                    mock.call(commands[provider], text=True, stdout=OMNI.subprocess.PIPE,
                              stderr=OMNI.subprocess.STDOUT)
                    for provider in states if states[provider] == "installed"
                ])
                launcher.assert_not_called()
                request.assert_not_called()

    def test_status_reports_unavailable_scheduler_as_json(self):
        launcher = Path("/tmp/omni-mcp")
        with mock.patch.object(OMNI, "mcp_launcher_path", return_value=launcher), \
                mock.patch.object(OMNI.shutil, "which", return_value=None), \
                mock.patch.object(OMNI, "request", side_effect=SystemExit("offline")):
            output = io.StringIO()
            with redirect_stdout(output):
                value = OMNI.mcp_status(as_json=True)
        self.assertEqual(json.loads(output.getvalue()), value)
        self.assertEqual(value["scheduler"]["state"], "unavailable")
        self.assertEqual([item["state"] for item in value["providers"]],
                         ["unavailable", "unavailable"])

    def test_doctor_round_trip_terminates_adapter(self):
        process = mock.Mock()
        process.stdin = mock.Mock()
        process.stdout.readline.side_effect = [
            json.dumps({"result": {"protocolVersion": "2025-11-25"}}) + "\n",
            json.dumps({"result": {"tools": [{"name": "list_agents"}]}}) + "\n",
            json.dumps({"result": {"isError": False,
                                    "structuredContent": {"agents": [{"name": "A"}]}}}) + "\n",
        ]
        with mock.patch.object(OMNI, "mcp_launcher_path", return_value=Path("/tmp/omni-mcp")), \
                mock.patch.object(OMNI.subprocess, "Popen", return_value=process):
            result = OMNI.mcp_round_trip()
        self.assertEqual(result, {"protocol": "2025-11-25", "tool_count": 1,
                                  "agent_count": 1})
        self.assertEqual(process.stdin.write.call_count, 3)
        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=3)



class NotificationCLITests(unittest.TestCase):
    def test_run_id_comes_from_the_runner_environment_first(self):
        lookup = mock.Mock()
        self.assertEqual(OMNI.current_run_id(lookup, {"OMNI_RUN_ID": " r1 "}, [10]), "r1")
        lookup.assert_not_called()

    def test_run_id_falls_back_to_an_active_ancestor_runner_or_provider(self):
        runs = [{"id": "old", "state": "completed", "pid": 10, "pgid": 11},
                {"id": "live", "state": "running", "pid": 20, "pgid": 21}]
        self.assertEqual(OMNI.current_run_id(lambda: runs, {}, [99, 21, 20]), "live")
        self.assertIsNone(OMNI.current_run_id(lambda: runs, {}, [10, 11]))
        self.assertIsNone(OMNI.current_run_id(lambda: runs, {}, []))

    def test_process_ancestors_walks_one_snapshot_and_stops_at_cycles(self):
        snapshot = mock.Mock(stdout="  1 0\n 50 1\n 60 50\n 70 60\n 80 81\n 81 80\n")
        with mock.patch.object(OMNI.subprocess, "run", return_value=snapshot):
            self.assertEqual(OMNI.process_ancestors(70), [70, 60, 50])
            self.assertEqual(OMNI.process_ancestors(80), [80, 81])

    def test_notify_payload_validates_and_resolves_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = OMNI.notify_payload("T", body="B", severity="info", workspace=directory,
                                          include_diff=False, dedupe_key="k", run_id="r")
            self.assertEqual(payload, {"op": "notify", "title": "T", "body": "B", "severity": "info",
                                       "workspace": str(Path(directory).resolve()), "include_diff": False,
                                       "dedupe_key": "k", "run_id": "r"})
        self.assertEqual(OMNI.notify_payload("T"), {"op": "notify", "title": "T"})
        with self.assertRaises(ValueError):
            OMNI.notify_payload("T", severity="loud")

    def test_notify_payload_accepts_only_omniterms_own_kinds(self):
        self.assertEqual(OMNI.notify_payload("T", kind="wrap_up")["kind"], "wrap_up")
        for kind in ("agent", "storage", "run_limit", "anything"):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                OMNI.notify_payload("T", kind=kind)

    def test_settings_set_sends_one_nested_change(self):
        calls = []
        with mock.patch.object(OMNI, "request", side_effect=lambda p: calls.append(p) or {"ok": True}), \
                redirect_stdout(io.StringIO()):
            OMNI.main(["settings", "set", "notifications.levels.info.banner", "true"])
            OMNI.main(["settings", "set", "agent_defaults.provider", "codex"])
            OMNI.main(["settings", "set", "retained_run_tabs", "12"])
        self.assertEqual([call["settings"] for call in calls], [
            {"notifications": {"levels": {"info": {"banner": True}}}},
            {"agent_defaults": {"provider": "codex"}},
            {"retained_run_tabs": 12},
        ])
        self.assertTrue(all(call["op"] == "set_settings" for call in calls))

    def test_settings_set_reports_the_schedulers_refusal(self):
        with mock.patch.object(OMNI, "request", return_value={"ok": False, "error": "Invalid setting: x"}), \
                self.assertRaisesRegex(SystemExit, "Invalid setting: x"):
            OMNI.main(["settings", "set", "x", "1"])
        with self.assertRaises(SystemExit):
            OMNI.main(["settings", "set", "a..b", "1"])

    def test_mute_and_unmute_set_muted_until(self):
        calls = []
        with mock.patch.object(OMNI, "request", side_effect=lambda p: calls.append(p) or {"ok": True}), \
                mock.patch.object(OMNI.time, "time", return_value=1000), redirect_stdout(io.StringIO()):
            OMNI.main(["mute"])
            OMNI.main(["mute", "2h"])
            OMNI.main(["mute", "30m"])
            OMNI.main(["unmute"])
        self.assertEqual([call["settings"]["notifications"]["muted_until"] for call in calls],
                         ["forever", 1000 + 7200, 1000 + 1800, 0])
        with self.assertRaises(ValueError):
            OMNI.mute_until("soon")
        morning = dt.datetime.fromtimestamp(OMNI.mute_until("tomorrow", now=1000))
        self.assertEqual((morning.hour, morning.minute), (8, 0))

    def test_agent_defaults_never_offer_a_dangerous_permission(self):
        defaults = OMNI.agent_defaults({"agent_defaults": {
            "provider": "codex", "overlap": "parallel",
            "models": {"codex": "gpt-5", "claude": "--evil"},
            "permission_modes": {"claude": "bypassPermissions", "codex": "on-request"}}})
        self.assertEqual(defaults, {"provider": "codex", "overlap": "parallel",
                                    "models": {"claude": "", "codex": "gpt-5"},
                                    "permission_modes": {"claude": "", "codex": "on-request"}})
        self.assertEqual(OMNI.agent_defaults({})["provider"], "claude")
        self.assertEqual(OMNI.agent_defaults({"agent_defaults": {"provider": "llama"}})["provider"], "claude")

    def test_notify_command_sends_one_request_and_prints_the_id(self):
        calls = []

        def scheduler(payload):
            calls.append(payload)
            return {"ok": True, "id": "n1"}

        output = io.StringIO()
        with mock.patch.object(OMNI, "request", side_effect=scheduler), \
                mock.patch.dict(OMNI.os.environ, {"OMNI_RUN_ID": "r9"}), redirect_stdout(output):
            OMNI.main(["notify", "Build failed", "--body", "see log", "--severity", "urgent",
                       "--no-diff", "--dedupe-key", "build"])
        self.assertEqual(calls, [{"op": "notify", "title": "Build failed", "body": "see log",
                                  "severity": "urgent", "include_diff": False,
                                  "dedupe_key": "build", "run_id": "r9"}])
        self.assertEqual(output.getvalue().strip(), "n1")

    def test_notify_command_reads_the_body_from_stdin(self):
        calls = []
        with mock.patch.object(OMNI, "request", side_effect=lambda p: calls.append(p) or {"id": "n"}), \
                mock.patch.dict(OMNI.os.environ, {"OMNI_RUN_ID": "r"}), \
                mock.patch.object(OMNI.sys, "stdin", io.StringIO("from a pipe")), \
                redirect_stdout(io.StringIO()):
            OMNI.main(["notify", "T", "--body", "-"])
        self.assertEqual(calls[0]["body"], "from a pipe")

    def test_notification_commands_map_to_scheduler_operations(self):
        cases = [
            (["notification", "n1", "read"], {"op": "notification_read", "id": "n1", "read": True}),
            (["notification", "n1", "unread"], {"op": "notification_read", "id": "n1", "read": False}),
            (["notification", "n1", "delete"], {"op": "notification_delete", "id": "n1"}),
            (["notifications", "--read-all"], {"op": "notifications_read_all"}),
            (["notifications", "--unread", "--json"], {"op": "notifications", "unread": True, "limit": 100}),
            (["inbox"], {"op": "show_inbox"}),
            (["inbox", "n1"], {"op": "show_inbox", "id": "n1"}),
        ]
        for argv, payload in cases:
            with self.subTest(argv=argv), \
                    mock.patch.object(OMNI, "request", return_value={"ok": True, "notifications": []}) as request, \
                    redirect_stdout(io.StringIO()):
                OMNI.main(argv)
                request.assert_called_once_with(payload)

    def test_notification_list_shows_severity_as_text(self):
        response = {"notifications": [{"id": "n1", "unread": True, "severity": "urgent", "title": "Key expired",
                                       "agent": None, "updated": 0}],
                    "attention_count": 1, "unread_count": 1}
        output = io.StringIO()
        with redirect_stdout(output):
            OMNI.print_notifications(response)
        self.assertIn("‼ Key expired", output.getvalue())
        self.assertIn("1 unread need attention", output.getvalue())


class SessionKnowledgeCLITests(unittest.TestCase):
    def test_memory_install_can_save_a_paused_provider_agent(self):
        with mock.patch.object(OMNI, "configure_memory_agent", return_value={"changed": True}) as configure, redirect_stdout(io.StringIO()):
            OMNI.main(["knowledge", "memory", "install", "--provider", "codex", "--workspace", "/tmp/project", "--disabled", "--json"])
        configure.assert_called_once_with(provider="codex", workspace="/tmp/project", enabled=False)

    def invoke(self, argv, response=None):
        output = io.StringIO()
        with mock.patch.object(OMNI, "request", return_value=response or {"ok": True}) as request, redirect_stdout(output):
            OMNI.main(argv)
        return request.call_args.args[0], json.loads(output.getvalue())

    def test_session_catalog_and_transcript_pages_use_native_service(self):
        payload, _ = self.invoke(["sessions", "--provider", "codex", "--workspace", "/tmp/project",
                                 "--state", "unknown", "--limit", "7", "--cursor", "next", "--json"])
        self.assertEqual(payload, {"op": "list_sessions", "provider": "codex", "workspace": "/tmp/project",
                                   "state": "unknown", "limit": 7, "cursor": "next"})
        payload, result = self.invoke(["session", "claude:session", "--offset", "25", "--limit", "10", "--json"],
                                      {"ok": True, "messages": [{"text": "\x1b[31muntrusted"}], "next_offset": 35})
        self.assertEqual(payload, {"op": "get_session", "session_id": "claude:session", "offset": 25, "limit": 10})
        self.assertEqual(result["next_offset"], 35)
        self.assertEqual(result["messages"][0]["text"], "\x1b[31muntrusted")

    def test_graph_commands_ask_the_code_graph_about_this_folder_or_a_project(self):
        payload, _ = self.invoke(["graph", "explain", "Store.save", "--json"])
        self.assertEqual(payload, {"op": "code_graph", "mode": "explain", "name": "Store.save", "limit": 30,
                                   "workspace": str(Path.cwd())})
        payload, _ = self.invoke(["graph", "path", "a", "b", "--project", "/tmp/app", "--json"])
        self.assertEqual(payload, {"op": "code_graph", "mode": "path", "name": "a", "target": "b", "limit": 30,
                                   "project": "/tmp/app"})
        self.assertEqual(self.invoke(["graph", "status", "--all", "--json"])[0],
                         {"op": "code_graph", "mode": "stats", "limit": 30})

    def test_knowledge_search_preserves_filters_and_semantic_mode(self):
        payload, _ = self.invoke(["knowledge", "search", "retry behavior", "--mode", "semantic",
                                 "--provider", "claude", "--workspace", "/tmp/project", "--session-id", "claude:s", "--limit", "4"])
        self.assertEqual(payload, {"op": "search_transcripts", "query": "retry behavior", "mode": "semantic",
                                   "provider": "claude", "workspace": "/tmp/project", "session_id": "claude:s", "limit": 4})
        self.assertEqual(self.invoke(["knowledge", "status"])[0], {"op": "knowledge_status"})
        self.assertEqual(self.invoke(["knowledge", "memories"])[0], {"op": "search_memories", "mode": "lexical", "limit": 20})
        self.assertEqual(self.invoke(["knowledge", "memories", "retry", "--mode", "hybrid"])[0],
                         {"op": "search_memories", "query": "retry", "mode": "hybrid", "limit": 20})

    def test_session_message_keeps_stable_retry_identity_and_attribution(self):
        identifier = "aa8c98c4-6d30-4be6-871a-39bb7d5aad88"
        payload, _ = self.invoke(["session", "codex:target", "send", "Review the report", "--request-id", identifier,
                                 "--sender-session-id", "claude:sender", "--sender-name", "Reviewer", "--json"])
        self.assertEqual(payload, {"op": "send_session_message", "session_id": "codex:target", "message": "Review the report",
                                   "request_id": identifier, "sender_session_id": "claude:sender", "sender_name": "Reviewer"})
        self.assertEqual(self.invoke(["message", identifier])[0], {"op": "get_session_message", "request_id": identifier})
        payload, _ = self.invoke(["session", "claude:target", "send", "Hello"])
        self.assertEqual(str(OMNI.uuid.UUID(payload["request_id"])), payload["request_id"])

    def test_missing_message_does_not_make_a_request(self):
        with mock.patch.object(OMNI, "request") as request, self.assertRaisesRegex(SystemExit, "Give a message"):
            OMNI.main(["session", "claude:target", "send"])
        request.assert_not_called()

    def test_lost_send_response_preserves_generated_retry_id_in_error(self):
        identifier = "aa8c98c4-6d30-4be6-871a-39bb7d5aad88"
        with mock.patch.object(OMNI.uuid, "uuid4", return_value=identifier), \
                mock.patch.object(OMNI, "request", side_effect=SystemExit("reply lost")), \
                self.assertRaisesRegex(SystemExit, identifier):
            OMNI.main(["session", "claude:target", "send", "Hello"])


if __name__ == "__main__":
    unittest.main()
