import datetime as dt
import importlib.machinery
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
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
        args = OMNI.cli_parser().parse_args(["agent", "Apex Wrapup"])
        self.assertEqual((args.name, args.action), ("Apex Wrapup", "show"))

    def test_legacy_agent_cli_is_normalized(self):
        self.assertEqual(OMNI.normalized_cli_argv(["agent", "list"]), ["agents"])
        self.assertEqual(OMNI.normalized_cli_argv(["agent", "edit", "Apex"]),
                         ["agent", "Apex", "edit"])
        self.assertEqual(OMNI.normalized_cli_argv(["agent", "add", "."]),
                         ["agents", "add", "."])
        self.assertEqual(OMNI.normalized_cli_argv(["run", "Apex"]),
                         ["run", "Apex"])

    def test_agent_name_is_generated_from_workspace_and_command(self):
        self.assertEqual(OMNI.generated_agent_name("/tmp/apex-rb", "/forge:wrapup"),
                         "apex-rb · forge:wrapup")
        self.assertEqual(OMNI.generated_agent_name("/tmp/apex-rb", ""), "apex-rb")
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

    def run_dashboard(self, keys, mouse=None):
        agents = [self.agent("A"), self.agent("B")]
        requests = []

        def request(payload):
            requests.append(payload)
            if payload["op"] == "list_agents":
                return {"agents": agents}
            if payload["op"] == "list_runs":
                return {"runs": []}
            if payload["op"] == "run_agent":
                return {"run_id": "run-12345678"}
            return {"ok": True}

        window = mock.Mock()
        window.getmaxyx.return_value = (32, 120)
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
                                          [OMNI.curses.KEY_RIGHT] * 12 + ["\n"])
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
                                          [OMNI.curses.KEY_RIGHT] * 12 + ["\n"])
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
                                          [OMNI.curses.KEY_RIGHT] * 12 + ["\n"])
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


if __name__ == "__main__":
    unittest.main()
