import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock
from pathlib import Path


_LOADER = importlib.machinery.SourceFileLoader(
    "omni_runner", str(Path(__file__).parents[1] / "helpers/omni-runner")
)
RUNNER = importlib.util.module_from_spec(importlib.util.spec_from_loader("omni_runner", _LOADER))
_LOADER.exec_module(RUNNER)


class ProviderPathTests(unittest.TestCase):
    def shell_result(self, stdout):
        return mock.Mock(stdout=stdout)

    def test_login_shell_path_ignores_startup_noise(self):
        output = f"welcome\n{RUNNER.PATH_MARKER}/a:/b{RUNNER.PATH_MARKER}bye\n"
        with mock.patch.object(RUNNER.subprocess, "run",
                               return_value=self.shell_result(output)) as run:
            self.assertEqual(RUNNER.login_shell_path({"SHELL": "/bin/zsh"}), "/a:/b")
        self.assertEqual(run.call_args.args[0][:2], ["/bin/zsh", "-lic"])
        self.assertEqual(run.call_args.kwargs["stdin"], RUNNER.subprocess.DEVNULL)

    def test_login_shell_path_tolerates_broken_or_silent_shells(self):
        failures = (OSError("missing"), RUNNER.subprocess.TimeoutExpired("zsh", 1))
        for failure in failures:
            with self.subTest(failure=failure), \
                    mock.patch.object(RUNNER.subprocess, "run", side_effect=failure):
                self.assertEqual(RUNNER.login_shell_path({"SHELL": "/bin/zsh"}), "")
        with mock.patch.object(RUNNER.subprocess, "run", return_value=self.shell_result("noise")):
            self.assertEqual(RUNNER.login_shell_path({"SHELL": "/bin/zsh"}), "")

    def test_search_path_prefers_login_shell_and_finds_launchd_only_providers(self):
        with mock.patch.object(RUNNER, "login_shell_path", return_value="/login:/usr/bin"):
            path = RUNNER.provider_search_path({"PATH": "/usr/bin:/bin"})
        directories = path.split(":")
        self.assertEqual(directories[:2], ["/login", "/usr/bin"])
        self.assertEqual(len(directories), len(set(directories)))
        self.assertIn(str(Path.home() / ".claude/bin"), directories)

    def test_real_claude_install_location_resolves_from_launchd_path(self):
        with tempfile.TemporaryDirectory() as root:
            binary = Path(root) / ".claude/bin/claude"
            binary.parent.mkdir(parents=True)
            binary.write_text("#!/bin/sh\n")
            binary.chmod(0o755)
            with mock.patch.dict(RUNNER.os.environ, {"HOME": root}), \
                    mock.patch.object(RUNNER, "login_shell_path", return_value=""):
                path = RUNNER.provider_search_path({"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"})
            self.assertEqual(shutil.which("claude", path=path), str(binary))


class ClaudeTrustTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name).resolve()
        patcher = mock.patch.object(RUNNER.Path, "home", return_value=self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config = self.home / ".claude.json"
        self.config.write_text(json.dumps({"oauth": "keep", "projects": {"/other": {"a": 1}}}))
        self.config.chmod(0o600)

    def folder(self, *parts):
        path = self.home.joinpath("Projects", *parts)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def test_trust_is_keyed_on_the_repository_root_including_worktrees(self):
        worktree = self.folder("repo/@main/src")
        (worktree.parent / ".git").write_text("gitdir: elsewhere\n")
        loose = self.folder("loose")
        self.assertEqual(RUNNER.trust_root(str(worktree)), str(worktree.parent))
        self.assertEqual(RUNNER.trust_root(str(loose)), str(loose))

    def test_repository_at_home_is_not_treated_as_the_workspace_root(self):
        (self.home / ".git").mkdir()
        project = self.folder("app")
        self.assertEqual(RUNNER.trust_root(str(project)), str(project))

    def test_trust_preserves_config_and_its_private_mode(self):
        project = self.folder("app")
        RUNNER.ensure_claude_trust(str(project))
        data = json.loads(self.config.read_text())
        self.assertTrue(data["projects"][str(project)]["hasTrustDialogAccepted"])
        self.assertEqual(data["oauth"], "keep")
        self.assertEqual(data["projects"]["/other"], {"a": 1})
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        self.assertEqual([path.name for path in self.home.glob(".claude.json.omni-*")], [])

    def test_already_trusted_folder_does_not_rewrite_the_config(self):
        project = self.folder("app")
        RUNNER.ensure_claude_trust(str(project))
        with mock.patch.object(RUNNER.os, "replace") as replace:
            RUNNER.ensure_claude_trust(str(project))
        replace.assert_not_called()

    def test_unreadable_config_is_left_untouched(self):
        for broken in ("not json", "[]"):
            with self.subTest(broken=broken):
                self.config.write_text(broken)
                RUNNER.ensure_claude_trust(str(self.folder("app")))
                self.assertEqual(self.config.read_text(), broken)

    def test_symlinked_config_stays_a_symlink(self):
        real = self.home / "dotfiles.json"
        self.config.rename(real)
        self.config.symlink_to(real)
        RUNNER.ensure_claude_trust(str(self.folder("app")))
        self.assertTrue(self.config.is_symlink())
        self.assertIn("hasTrustDialogAccepted", real.read_text())


class ProviderArgumentTests(unittest.TestCase):
    def test_managed_agent_environment_forces_color(self):
        environment = RUNNER.provider_environment({"NO_COLOR": "1", "TERM": "xterm-256color"})
        self.assertNotIn("NO_COLOR", environment)
        self.assertEqual(environment["FORCE_COLOR"], "1")
        self.assertEqual(environment["CLICOLOR"], "1")
        self.assertEqual(environment["COLORTERM"], "truecolor")

    def test_environment_drops_the_parent_session(self):
        environment = RUNNER.provider_environment({
            "CLAUDECODE": "1",
            "CLAUDE_CODE_CHILD_SESSION": "1",
            "CLAUDE_CODE_MESSAGING_TOKEN": "secret",
            "CLAUDE_CONFIG_DIR": "/config",
            "CLAUDE_CODE_USE_BEDROCK": "1",
        })
        self.assertNotIn("CLAUDECODE", environment)
        self.assertNotIn("CLAUDE_CODE_CHILD_SESSION", environment)
        self.assertNotIn("CLAUDE_CODE_MESSAGING_TOKEN", environment)
        self.assertEqual(environment["CLAUDE_CONFIG_DIR"], "/config")
        self.assertEqual(environment["CLAUDE_CODE_USE_BEDROCK"], "1")

    def test_managed_agent_environment_preserves_existing_truecolor_hint(self):
        environment = RUNNER.provider_environment({"COLORTERM": "24bit", "KEEP": "yes"})
        self.assertEqual(environment["COLORTERM"], "24bit")
        self.assertEqual(environment["KEEP"], "yes")

    def test_terminal_resize_is_forwarded_to_provider_group(self):
        with mock.patch.object(RUNNER.os, "killpg") as killpg:
            RUNNER.forward_terminal_resize(1234)
        killpg.assert_called_once_with(1234, RUNNER.signal.SIGWINCH)

    def test_terminal_resize_tolerates_a_finished_provider(self):
        for error in (ProcessLookupError, PermissionError):
            with self.subTest(error=error), \
                    mock.patch.object(RUNNER.os, "killpg", side_effect=error):
                RUNNER.forward_terminal_resize(1234)

    def base(self, provider):
        return {"provider": provider, "workspace": "/tmp/a space", "system_prompt": "system\nline",
                "startup_prompt": "user prompt", "model": "model", "permission_mode": "",
                "advanced_args": ["--flag", "value with space"]}

    def test_claude_roles_are_distinct(self):
        argv = RUNNER.provider_argv(self.base("claude"))
        self.assertEqual(argv[argv.index("--system-prompt") + 1], "system\nline")
        self.assertEqual(argv[-1], "user prompt")

    def test_claude_run_uses_run_id_as_session_and_keeps_discrete_arguments(self):
        argv = RUNNER.provider_argv(self.base("claude"), run_id="run-id")
        note = RUNNER.OMNI.run_context_note("agent", "run-id")
        self.assertEqual(argv, [
            "claude", "--system-prompt", "system\nline", "--session-id", "run-id",
            "--append-system-prompt", note,
            "--model", "model", "--flag", "value with space", "--", "user prompt",
        ])

    def test_variadic_advanced_flags_cannot_swallow_the_startup_prompt(self):
        # Claude's `--add-dir <directories...>` took the prompt as a directory,
        # so a manual run of an agent with extra directories sat idle.
        for provider in ("claude", "codex"):
            for resume, automatic in ((None, False), ("1234", True)):
                with self.subTest(provider=provider, resume=resume):
                    agent = self.base(provider)
                    agent["advanced_args"] = ["--add-dir", "/tmp/one", "--add-dir", "/tmp/two"]
                    argv = RUNNER.provider_argv(agent, resume_session_id=resume, automatic_resume=automatic)
                    self.assertEqual(argv[-2], "--")
                    self.assertTrue(argv[-1].startswith("user prompt"))
                    self.assertEqual(argv.count("--"), 1)

    def test_a_prompt_starting_with_a_dash_stays_the_prompt(self):
        agent = self.base("claude")
        agent["startup_prompt"] = "--help me triage"
        self.assertEqual(RUNNER.provider_argv(agent)[-2:], ["--", "--help me triage"])

    def test_runs_learn_which_run_they_are_and_how_to_notify(self):
        agent = self.base("claude")
        agent["name"] = "Nightly"
        note = RUNNER.OMNI.run_context_note("Nightly", "run-9")
        self.assertIn("“Nightly” (run run-9)", note)
        self.assertIn("notify_user", note)
        self.assertIn("Never for routine progress", note)
        # The person's own system prompt stays first and unchanged.
        argv = RUNNER.provider_argv(agent, run_id="run-9")
        self.assertEqual(argv[argv.index("--system-prompt") + 1], "system\nline")
        self.assertEqual(argv[argv.index("--append-system-prompt") + 1], note)
        resumed = RUNNER.provider_argv(agent, run_id="run-10", resume_session_id="1234")
        self.assertIn("--append-system-prompt", resumed)
        codex = self.base("codex")
        codex["name"] = "Nightly"
        argv = RUNNER.provider_argv(codex, run_id="run-9")
        instructions = json.loads(argv[argv.index("-c") + 1].removeprefix("developer_instructions="))
        self.assertTrue(instructions.startswith("system\nline\n\n"))
        self.assertTrue(instructions.endswith(note))

    def test_without_a_run_nothing_is_appended(self):
        self.assertNotIn("--append-system-prompt", RUNNER.provider_argv(self.base("claude")))

    def test_codex_roles_and_workspace_are_distinct(self):
        argv = RUNNER.provider_argv(self.base("codex"))
        self.assertEqual(argv[argv.index("-C") + 1], "/tmp/a space")
        self.assertIn("developer_instructions=", argv[argv.index("-c") + 1])
        self.assertEqual(argv[-1], "user prompt")

    def test_codex_runs_skip_the_startup_update_prompt(self):
        # The prompt would take the Enter OmniTerm types to submit /compact or a tick,
        # and install an update nobody asked for.
        for resume in (None, "1234"):
            argv = RUNNER.provider_argv(self.base("codex"), resume_session_id=resume)
            settings = [argv[index + 1] for index, value in enumerate(argv) if value == "-c"]
            self.assertIn("check_for_update_on_startup=false", settings)
            self.assertLess(argv.index("check_for_update_on_startup=false"),
                            argv.index("resume") if resume else len(argv))

    def test_codex_system_prompt_is_json_encoded_without_shell_interpolation(self):
        agent = self.base("codex")
        agent["system_prompt"] = 'quotes " and unicode café\nnext'
        argv = RUNNER.provider_argv(agent)
        configuration = argv[argv.index("-c") + 1]
        self.assertEqual(json.loads(configuration.removeprefix("developer_instructions=")),
                         agent["system_prompt"])

    def test_claude_resume_uses_provider_session_without_startup_prompt(self):
        argv = RUNNER.provider_argv(self.base("claude"), resume_session_id="1234")
        self.assertEqual(argv[argv.index("--resume") + 1], "1234")
        self.assertNotIn("user prompt", argv)
        self.assertNotIn("--system-prompt", argv)

    def test_codex_run_trusts_its_workspace_without_touching_provider_config(self):
        with tempfile.TemporaryDirectory() as root:
            root = os.path.realpath(root)
            nested = Path(root) / "src"
            nested.mkdir()
            (Path(root) / ".git").mkdir()
            agent = self.base("codex")
            agent["workspace"] = str(nested)
            argv = RUNNER.provider_argv(agent)
        settings = [argv[index + 1] for index, value in enumerate(argv) if value == "-c"]
        self.assertEqual(settings[1], 'projects={%s={trust_level="trusted"},%s={trust_level="trusted"}}'
                         % (json.dumps(str(nested)), json.dumps(root)))

    def test_codex_dangerous_mode_uses_its_flag_and_not_an_approval_policy(self):
        agent = self.base("codex")
        agent["permission_mode"] = "dangerous"
        argv = RUNNER.provider_argv(agent)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertNotIn("--ask-for-approval", argv)

    def test_claude_dangerous_mode_passes_through_as_a_permission_mode(self):
        agent = self.base("claude")
        agent["permission_mode"] = "bypassPermissions"
        argv = RUNNER.provider_argv(agent)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "bypassPermissions")

    def test_codex_resume_uses_provider_session_without_startup_prompt(self):
        argv = RUNNER.provider_argv(self.base("codex"), resume_session_id="1234")
        self.assertIn("resume", argv)
        self.assertEqual(argv[argv.index("resume") + 1], "1234")
        self.assertNotIn("user prompt", argv)


class SocketAndSessionTests(unittest.TestCase):
    def test_request_frames_json_reads_chunks_and_closes(self):
        client = mock.Mock()
        client.recv.side_effect = [b'{"ok":', b'true}', b""]
        with mock.patch.object(RUNNER.socket, "socket", return_value=client):
            self.assertEqual(RUNNER.request({"op": "ping"}), {"ok": True})
        self.assertEqual(json.loads(client.sendall.call_args.args[0]), {"op": "ping"})
        client.shutdown.assert_called_once_with(RUNNER.socket.SHUT_WR)
        client.close.assert_called_once_with()

    def test_request_reads_the_answer_when_the_scheduler_closed_first(self):
        client = mock.Mock()
        client.shutdown.side_effect = OSError(57, "Socket is not connected")
        client.recv.side_effect = [b'{"ok":true}', b""]
        with mock.patch.object(RUNNER.socket, "socket", return_value=client):
            self.assertEqual(RUNNER.request({"op": "ping"}), {"ok": True})
        client.close.assert_called_once_with()

    def test_request_closes_after_connection_failure(self):
        client = mock.Mock()
        client.connect.side_effect = OSError("offline")
        with mock.patch.object(RUNNER.socket, "socket", return_value=client), \
                self.assertRaises(OSError):
            RUNNER.request({"op": "ping"})
        client.close.assert_called_once_with()

    def test_codex_session_files_discovers_jsonl_recursively(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            sessions = home / ".codex/sessions/2026/08"
            sessions.mkdir(parents=True)
            expected = sessions / "one.jsonl"
            expected.write_text("{}")
            (sessions / "ignore.txt").write_text("{}")
            with mock.patch.object(RUNNER.Path, "home", return_value=home):
                self.assertEqual(RUNNER.codex_session_files(), {expected})

    def test_codex_session_files_returns_empty_when_catalog_is_absent(self):
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(RUNNER.Path, "home", return_value=Path(root)):
            self.assertEqual(RUNNER.codex_session_files(), set())

    def test_codex_session_id_matches_workspace_in_first_eight_events(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "session.jsonl"
            events = [{"type": "event"}, {"type": "session_meta",
                       "payload": {"cwd": "/tmp/work", "id": "session-1"}}]
            path.write_text("\n".join(json.dumps(event) for event in events))
            self.assertEqual(RUNNER.codex_session_id(path, "/tmp/work"), "session-1")
            self.assertIsNone(RUNNER.codex_session_id(path, "/tmp/other"))

    def test_codex_session_id_tolerates_missing_or_malformed_files(self):
        with tempfile.TemporaryDirectory() as root:
            malformed = Path(root) / "bad.jsonl"
            malformed.write_text("not json")
            self.assertIsNone(RUNNER.codex_session_id(malformed, "/tmp"))
            self.assertIsNone(RUNNER.codex_session_id(Path(root) / "missing", "/tmp"))


class ErrorCaptureTests(unittest.TestCase):
    def capture(self, script):
        errors = RUNNER.ErrorTail()
        self.addCleanup(os.close, errors.read_end)
        child = RUNNER.subprocess.Popen(["/bin/sh", "-c", script], stderr=errors.write_end)
        errors.close_write_end()
        with mock.patch.object(RUNNER.os, "write"):
            child.wait()
            return errors.text()

    def test_real_provider_error_is_kept_without_terminal_styling(self):
        text = self.capture(r"printf '\033[31mNo conversation found\033[0m\n' >&2; exit 1")
        self.assertEqual(text, "No conversation found")

    def test_only_the_end_of_long_error_output_is_kept(self):
        text = self.capture("yes 0123456789 | head -c 100000 >&2; echo last line >&2")
        self.assertLessEqual(len(text), RUNNER.RUN_LOG_BYTES)
        self.assertTrue(text.endswith("last line"))

    def test_run_logs_are_private_and_pruned_to_the_newest(self):
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(RUNNER, "RUN_LOG_DIRECTORY", Path(root) / "logs"), \
                mock.patch.object(RUNNER, "RUN_LOGS_KEPT", 2):
            for number in range(3):
                RUNNER.save_run_log(f"run-{number}", "boom")
                os.utime(RUNNER.RUN_LOG_DIRECTORY / f"run-{number}.log", (number + 1, number + 1))
            RUNNER.save_run_log("run-3", "boom")
            logs = sorted(path.name for path in RUNNER.RUN_LOG_DIRECTORY.iterdir())
            self.assertEqual(logs, ["run-2.log", "run-3.log"])
            self.assertEqual((RUNNER.RUN_LOG_DIRECTORY / "run-3.log").stat().st_mode & 0o777, 0o600)

    def test_unwritable_log_never_changes_the_outcome(self):
        with mock.patch.object(RUNNER, "RUN_LOG_DIRECTORY", Path("/dev/null/logs")):
            RUNNER.save_run_log("run", "boom")


class RunOutcomeTests(unittest.TestCase):
    def test_a_closed_tab_is_not_a_failure_whatever_the_exit_code(self):
        for code in (0, 1, -1, -9, 143):
            self.assertEqual(RUNNER.run_outcome(code, True), ("closed", "tab closed"))

    def test_a_requested_stop_is_not_a_failure(self):
        for code in (143, -15):
            self.assertEqual(RUNNER.run_outcome(code, False), ("stopped", "stopped"))

    def test_failure_reason_names_the_last_error_line(self):
        state, reason = RUNNER.run_outcome(1, False, "starting\nNo conversation found")
        self.assertEqual((state, reason), ("failed", "exit 1: No conversation found"))
        self.assertEqual(RUNNER.run_outcome(7, False), ("failed", "exit 7"))
        long_reason = RUNNER.run_outcome(1, False, "x" * 500)[1]
        self.assertEqual(len(long_reason), len("exit 1: ") + RUNNER.REASON_CHARACTERS)

    def test_a_finished_turn_completes_the_run_whatever_ended_it(self):
        cases = ((-1, True, "agent finished, then tab closed"),
                 (143, False, "agent finished, then stopped"),
                 (137, False, "agent finished, then exit 137"))
        for code, closed, reason in cases:
            with self.subTest(code=code):
                self.assertEqual(RUNNER.run_outcome(code, closed, turn_finished=True), ("completed", reason))

    def test_an_unfinished_turn_keeps_the_real_ending(self):
        self.assertEqual(RUNNER.run_outcome(137, False, turn_finished=False), ("failed", "exit 137"))
        self.assertEqual(RUNNER.run_outcome(0, False, turn_finished=False), ("completed", "exit 0"))


def claude_event(kind, stop_reason=None, **fields):
    return {"type": kind, "message": {"stop_reason": stop_reason}, **fields}


def codex_event(kind):
    return {"type": "event_msg", "payload": {"type": kind}}


class TurnStateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "session.jsonl"

    def write(self, *lines):
        self.path.write_text("".join(
            (line if isinstance(line, str) else json.dumps(line)) + "\n" for line in lines))

    def claude_state(self, *lines):
        self.write(*lines)
        return RUNNER.last_turn_state(self.path, RUNNER.claude_turn_event)

    def test_claude_end_turn_is_finished_despite_trailing_bookkeeping(self):
        self.assertIs(self.claude_state(claude_event("user"), claude_event("assistant", "end_turn"),
                                        {"type": "last-prompt"}, {"type": "summary"}), True)

    def test_claude_mid_turn_states_are_not_finished(self):
        cases = ((claude_event("assistant", "tool_use"),),
                 (claude_event("assistant", "end_turn"), claude_event("user")),
                 (claude_event("assistant", "max_tokens"),),
                 (claude_event("assistant", "end_turn", isApiErrorMessage=True),))
        for lines in cases:
            with self.subTest(lines=lines):
                self.assertIs(self.claude_state(*lines), False)

    def test_claude_subagent_and_meta_events_say_nothing_about_the_turn(self):
        self.assertIs(self.claude_state(claude_event("assistant", "end_turn"),
                                        claude_event("user", isSidechain=True),
                                        claude_event("user", isMeta=True)), True)

    def test_a_claude_compaction_summary_does_not_start_a_turn(self):
        # Compacting an idle session writes the summary as a user event; the
        # agent is still waiting for the person afterwards.
        self.assertIs(self.claude_state(claude_event("user"), claude_event("assistant", "end_turn"),
                                        {"type": "system", "subtype": "compact_boundary"},
                                        claude_event("user", isCompactSummary=True, isVisibleInTranscriptOnly=True),
                                        {"type": "system", "subtype": "informational"}), True)

    def test_codex_turn_follows_task_events(self):
        cases = (("task_complete", True), ("task_started", False), ("turn_aborted", False))
        for kind, expected in cases:
            with self.subTest(kind=kind):
                self.write(codex_event("task_started"), codex_event(kind), codex_event("token_count"))
                self.assertIs(RUNNER.last_turn_state(self.path, RUNNER.codex_turn_event), expected)

    def test_a_half_written_last_line_is_skipped(self):
        self.assertIs(self.claude_state(claude_event("assistant", "end_turn"), '{"type": "us'), True)

    def test_an_unsettled_transcript_is_unknown(self):
        self.assertIsNone(self.claude_state({"type": "summary"}))
        self.assertIsNone(RUNNER.last_turn_state(self.path.with_name("missing"), RUNNER.claude_turn_event))

    def test_only_the_tail_of_a_large_transcript_is_read(self):
        with mock.patch.object(RUNNER, "TURN_TAIL_BYTES", 200):
            self.assertIs(self.claude_state(claude_event("user"), "x" * 500,
                                            claude_event("assistant", "end_turn")), True)

    def tracker(self, provider="claude", session_id="run-1"):
        tracker = RUNNER.TurnTracker(provider, session_id)
        tracker.path = self.path
        return tracker

    def test_tracker_follows_new_turns_and_keeps_the_last_answer(self):
        tracker = self.tracker()
        self.assertIs(tracker.check(), False)
        self.write(claude_event("assistant", "end_turn"))
        self.assertIs(tracker.check(), True)
        self.write({"type": "summary"})
        os.utime(self.path, ns=(1, 1))
        self.assertIs(tracker.check(), True)
        self.write(claude_event("user"))
        os.utime(self.path, ns=(2, 2))
        self.assertIs(tracker.check(), False)

    def test_tracker_finds_each_provider_transcript(self):
        home = Path(self.directory.name)
        claude = home / ".claude/projects/-tmp/abc.jsonl"
        codex = home / ".codex/sessions/2026/11/01/rollout-2026-11-01T01-26-22-abc.jsonl"
        for path in (claude, codex):
            path.parent.mkdir(parents=True)
            path.write_text("")
        with mock.patch.object(RUNNER.Path, "home", return_value=home):
            self.assertEqual(RUNNER.TurnTracker("claude", "abc").transcript(), claude)
            self.assertEqual(RUNNER.TurnTracker("codex", "abc").transcript(), codex)
            self.assertIsNone(RUNNER.TurnTracker("claude", "other").transcript())
            self.assertIsNone(RUNNER.TurnTracker("codex", None).transcript())

    def test_watcher_reports_changes_once_and_retries_a_failed_report(self):
        tracker = mock.Mock(final_message=None, path=None)
        tracker.check.side_effect = [False, True, True, True, False]
        stop = mock.Mock()
        stop.wait.side_effect = [False] * 5 + [True]
        sent = []
        summaries = mock.Mock()

        def request(payload):
            sent.append(payload["finished"])
            if len(sent) == 1:
                raise OSError("scheduler restarted")

        with mock.patch.object(RUNNER, "request", side_effect=request):
            RUNNER.watch_turn(RUNNER.TurnReporter("run-1", tracker, summaries), stop)
        self.assertEqual(sent, [True, True, False])
        summaries.assert_called_once()
        self.assertIsInstance(summaries.call_args.args[0], float)

    def test_finished_turn_reports_its_final_message_and_transcript_then_summarizes(self):
        tracker = self.tracker()
        self.write(claude_event("user"), assistant_text("m1", "First answer"))
        sent, summaries = [], mock.Mock()
        reporter = RUNNER.TurnReporter("run-1", tracker, summaries)
        with mock.patch.object(RUNNER, "request", side_effect=lambda payload: sent.append(payload)), \
                mock.patch.object(RUNNER.time, "time", side_effect=[100.5, 200.5]):
            reporter.report()
            reporter.report()
            self.write(claude_event("user"), assistant_text("m2", "Second answer"))
            os.utime(self.path, ns=(3, 3))
            reporter.report()
        self.assertEqual(sent, [
            {"op": "runner_turn", "run_id": "run-1", "finished": True, "turn_at": 100.5,
             "final_message": "First answer", "transcript_path": str(self.path)},
            {"op": "runner_turn", "run_id": "run-1", "finished": True, "turn_at": 200.5,
             "final_message": "Second answer", "transcript_path": str(self.path)}])
        self.assertEqual(summaries.call_args_list, [mock.call(100.5), mock.call(200.5)])

    def test_unfinished_turn_sends_no_final_message(self):
        tracker = self.tracker()
        self.write(assistant_text("m1", "Done"), claude_event("user"))
        tracker.check()
        sent = []
        reporter = RUNNER.TurnReporter("run-1", tracker)
        reporter.reported = (True, "Done")
        with mock.patch.object(RUNNER, "request", side_effect=lambda payload: sent.append(payload)):
            reporter.report()
        self.assertEqual(sent, [{"op": "runner_turn", "run_id": "run-1", "finished": False}])


def assistant_text(message_id, *blocks, stop_reason="end_turn", **fields):
    content = [{"type": "text", "text": block} if isinstance(block, str) else block for block in blocks]
    return {"type": "assistant", "message": {"id": message_id, "stop_reason": stop_reason,
                                             "content": content}, **fields}


class FinalMessageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "session.jsonl"

    def turn(self, provider, *lines):
        self.path.write_text("".join(json.dumps(line) + "\n" for line in lines))
        return RUNNER.last_turn(self.path, *RUNNER.TURN_READERS[provider])

    def test_claude_final_message_joins_the_blocks_of_the_last_message(self):
        state, message = self.turn(
            "claude",
            claude_event("user"),
            assistant_text("old", "Earlier answer"),
            claude_event("user"),
            assistant_text("final", {"type": "thinking", "thinking": "hidden"}),
            assistant_text("final", "Opened PR #4."),
            {"type": "user", "isSidechain": True},
            assistant_text("final", "Tests pass.", {"type": "tool_use", "name": "x"}),
            {"type": "last-prompt"})
        self.assertIs(state, True)
        self.assertEqual(message, "Opened PR #4.\n\nTests pass.")

    def test_claude_turn_without_text_has_no_final_message(self):
        self.assertEqual(self.turn("claude", assistant_text("m", {"type": "thinking", "thinking": "x"})),
                         (True, None))
        self.assertEqual(self.turn("claude", assistant_text("m", "Working", stop_reason="tool_use")),
                         (False, None))

    def test_codex_final_message_prefers_task_complete(self):
        state, message = self.turn(
            "codex",
            {"type": "event_msg", "payload": {"type": "task_started"}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "Interim"}},
            {"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "All done."}})
        self.assertEqual((state, message), (True, "All done."))

    def test_codex_falls_back_to_the_turns_last_agent_message(self):
        previous_turn = {"type": "event_msg", "payload": {"type": "agent_message", "message": "Old turn"}}
        state, message = self.turn(
            "codex",
            previous_turn,
            {"type": "event_msg", "payload": {"type": "task_started"}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "First"}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant"}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "Last"}},
            {"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": None}})
        self.assertEqual((state, message), (True, "Last"))
        state, message = self.turn(
            "codex", previous_turn,
            {"type": "event_msg", "payload": {"type": "task_started"}},
            {"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": None}})
        self.assertEqual((state, message), (True, None))

    def test_final_message_is_capped(self):
        state, message = self.turn("codex", {"type": "event_msg", "payload": {
            "type": "task_complete", "last_agent_message": "x" * 5000}})
        self.assertEqual(len(message), RUNNER.FINAL_MESSAGE_CHARACTERS)
        self.assertTrue(message.endswith("…"))


class SummarizerTests(unittest.TestCase):
    def test_summarizer_has_no_tools_mcp_servers_or_transcript_and_takes_the_prompt_on_stdin(self):
        argv = RUNNER.summarizer_argv("haiku")
        self.assertEqual(argv[:4], ["claude", "-p", "--model", "haiku"])
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        for flag in ("--strict-mcp-config", "--safe-mode", "--no-session-persistence"):
            self.assertIn(flag, argv)
        self.assertNotIn("--mcp-config", argv)
        self.assertEqual(argv[-2:], ["--output-format", "text"])

    def test_summarizer_environment_is_not_a_run_and_not_a_child_session(self):
        environment = RUNNER.summarizer_environment("/login/bin:/usr/bin", {
            "OMNI_RUN_ID": "r", "OMNI_AGENT": "A", "OMNITERM_RUN_ID": "r", "CLAUDECODE": "1",
            "PATH": "/usr/bin", "HOME": "/Users/me"})
        for name in ("OMNI_RUN_ID", "OMNI_AGENT", "OMNITERM_RUN_ID", "CLAUDECODE", "FORCE_COLOR"):
            self.assertNotIn(name, environment)
        self.assertEqual(environment["PATH"], "/login/bin:/usr/bin")
        self.assertEqual(environment["NO_COLOR"], "1")
        self.assertEqual(environment["HOME"], "/Users/me")

    def test_prompt_lists_the_runs_facts_and_caps_long_lists(self):
        run = {"agent": "Nightly", "provider": "codex", "state": "completed", "workspace": "/w",
               "branch": "main", "diff_state": "ready", "final_message": "Fixed the build.",
               "commits": [{"sha": "a", "subject": f"Commit {index}"} for index in range(40)],
               "diff_files": [f"src/file{index}.py" for index in range(100)] + [7]}
        prompt = RUNNER.summary_prompt(run)
        for text in ("Nightly (codex)", "Branch: main", "- Commit 29", "… and 10 more",
                     "(100, diff ready)", "- src/file59.py", "… and 40 more", "Fixed the build."):
            self.assertIn(text, prompt)
        self.assertNotIn("Commit 30", prompt)
        self.assertNotIn("src/file60.py", prompt)
        with mock.patch.object(RUNNER, "SUMMARY_PROMPT_CHARACTERS", 100):
            self.assertLessEqual(len(RUNNER.summary_prompt(run)), 100)

    def test_prompt_tolerates_missing_and_malformed_fields(self):
        prompt = RUNNER.summary_prompt({"commits": "bad", "diff_files": None, "final_message": None})
        self.assertIn("- none", prompt)
        self.assertIn("(none)", prompt)

    def test_settings_default_to_haiku_and_on_and_fall_back_to_the_settings_file(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"
            with mock.patch.object(RUNNER, "SETTINGS_FILE", settings), \
                    mock.patch.object(RUNNER, "request", return_value={"ok": True, "settings": {}}):
                self.assertEqual(RUNNER.summary_settings(), (True, "haiku"))
                settings.write_text(json.dumps({"summaries": False, "summary_model": "sonnet"}))
                self.assertEqual(RUNNER.summary_settings(), (False, "sonnet"))
            with mock.patch.object(RUNNER, "SETTINGS_FILE", settings), \
                    mock.patch.object(RUNNER, "request", return_value={
                        "ok": True, "settings": {"summaries": True, "summary_model": "--evil"}}):
                self.assertEqual(RUNNER.summary_settings(), (True, "haiku"))

    def summarize(self, runs, process=None, settings=(True, "haiku"), turn_at=None):
        sent = []

        def request(payload):
            sent.append(payload)
            return next(runs) if payload["op"] == "get_run" else {"ok": True}

        with mock.patch.object(RUNNER, "request", side_effect=request), \
                mock.patch.object(RUNNER, "summary_settings", return_value=settings), \
                mock.patch.object(RUNNER.subprocess, "Popen", return_value=process) as popen, \
                mock.patch.object(RUNNER.time, "sleep"):
            RUNNER.summarize("run-1", turn_at)
        return sent, popen

    def test_summarizer_waits_for_the_diff_then_reports_the_summary(self):
        process = mock.Mock(returncode=0, pid=99)
        process.communicate.return_value = ("\x1b[1mDid it.\x1b[0m\n" + "line\n" * 20, "")
        runs = iter([{"ok": True, "run": {"diff_state": "pending"}},
                     {"ok": True, "run": {"diff_state": "ready", "final_message": "Done"}}])
        sent, popen = self.summarize(runs, process)
        self.assertEqual([payload["op"] for payload in sent], ["get_run", "get_run", "run_summary"])
        self.assertEqual(sent[-1]["state"], "ready")
        self.assertTrue(sent[-1]["summary"].startswith("Did it.\nline"))
        self.assertEqual(len(sent[-1]["summary"].splitlines()), RUNNER.SUMMARY_LINES)
        self.assertIn("Done", process.communicate.call_args.args[0])
        options = popen.call_args.kwargs
        self.assertTrue(options["start_new_session"])
        self.assertNotIn("OMNI_RUN_ID", options["env"])
        self.assertTrue(Path(options["cwd"]).name.startswith("omni-summary-"))
        self.assertFalse(Path(options["cwd"]).exists())

    def test_summarizer_reads_the_run_as_it_was_for_its_turn(self):
        # A loop may record the finished turn as a run of its own and start the
        # next one while the summarizer waits: it asks for that turn by turn_at.
        process = mock.Mock(returncode=0, pid=99)
        process.communicate.return_value = ("Did it.\n", "")
        sent, _ = self.summarize(iter([{"ok": True, "run": {"diff_state": "ready"}}]), process, turn_at=100.5)
        self.assertEqual(sent[0], {"op": "get_run", "id": "run-1", "turn_at": 100.5})

    def test_summarizer_reports_a_failure_instead_of_a_summary(self):
        process = mock.Mock(returncode=1, pid=99)
        process.communicate.return_value = ("", "Warning\nNot logged in\n")
        sent, _ = self.summarize(iter([{"ok": True, "run": {"diff_state": "none"}}]), process, turn_at=100.5)
        self.assertEqual(sent[-1], {"op": "run_summary", "run_id": "run-1", "state": "failed",
                                    "turn_at": 100.5, "error": "claude exit 1: Not logged in"})

    def test_summarizer_kills_a_stuck_model_call(self):
        process = mock.Mock(pid=99)
        process.communicate.side_effect = [RUNNER.subprocess.TimeoutExpired("claude", 90), ("", "")]
        with mock.patch.object(RUNNER, "signal_group") as kill:
            sent, _ = self.summarize(iter([{"ok": True, "run": {"diff_state": "none"}}]), process)
        kill.assert_called_once_with(99, RUNNER.signal.SIGKILL)
        self.assertEqual(sent[-1]["state"], "failed")
        self.assertIn("timed out", sent[-1]["error"])

    def test_summaries_turned_off_never_call_the_model(self):
        sent, popen = self.summarize(iter([]), settings=(False, "haiku"))
        popen.assert_not_called()
        self.assertEqual(sent, [{"op": "run_summary", "run_id": "run-1", "state": "failed",
                                 "error": "AI summaries are turned off"}])

    def test_a_missing_run_is_not_summarized(self):
        sent, popen = self.summarize(iter([{"ok": False, "code": "run_not_found"}]))
        popen.assert_not_called()
        self.assertEqual([payload["op"] for payload in sent], ["get_run"])

    def test_summarizer_starts_detached_without_the_runs_identity(self):
        with mock.patch.object(RUNNER.subprocess, "Popen") as popen, \
                mock.patch.dict(RUNNER.os.environ, {"OMNI_RUN_ID": "run-1"}):
            RUNNER.start_summarizer("run-1", "/login/bin", 100.5)
        argv, options = popen.call_args.args[0], popen.call_args.kwargs
        self.assertEqual(argv[-3:], ["--summarize", "run-1", "100.5"])
        self.assertTrue(options["start_new_session"])
        self.assertEqual(options["stdout"], RUNNER.subprocess.DEVNULL)
        self.assertNotIn("OMNI_RUN_ID", options["env"])

    def test_runner_entry_point_dispatches_the_summarizer(self):
        with mock.patch.object(RUNNER.sys, "argv", ["omni-runner", "--summarize", "run-1"]), \
                mock.patch.object(RUNNER, "summarize") as summarize, \
                mock.patch.object(RUNNER, "request") as request:
            RUNNER.main()
        summarize.assert_called_once_with("run-1", None)
        request.assert_not_called()
        with mock.patch.object(RUNNER.sys, "argv", ["omni-runner", "--summarize", "run-1", "100.5"]), \
                mock.patch.object(RUNNER, "summarize") as summarize:
            RUNNER.main()
        summarize.assert_called_once_with("run-1", 100.5)


class StartSlotTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = mock.patch.object(RUNNER, "START_SLOT", Path(directory.name) / "start.lock")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_second_start_waits_until_the_slot_is_released(self):
        first = RUNNER.acquire_start_slot()
        order = []

        def second():
            slot = RUNNER.acquire_start_slot()
            order.append("second")
            os.close(slot)

        with mock.patch.object(RUNNER, "START_SLOT_POLL", 0.01):
            waiter = RUNNER.threading.Thread(target=second)
            waiter.start()
            RUNNER.time.sleep(0.1)
            order.append("released")
            os.close(first)
            waiter.join(5)
        self.assertEqual(order, ["released", "second"])

    def test_a_stuck_holder_delays_a_run_but_never_loses_it(self):
        first = RUNNER.acquire_start_slot()
        self.addCleanup(os.close, first)
        with mock.patch.object(RUNNER, "START_SLOT_ATTEMPTS", 2), \
                mock.patch.object(RUNNER, "START_SLOT_POLL", 0):
            self.assertIsNone(RUNNER.acquire_start_slot())

    def test_unwritable_slot_does_not_stop_the_run(self):
        with mock.patch.object(RUNNER, "START_SLOT", Path("/dev/null/start.lock")):
            self.assertIsNone(RUNNER.acquire_start_slot())
        RUNNER.release_start_slot(None)

    def test_slot_stays_held_for_the_spacing_after_start(self):
        slot = RUNNER.acquire_start_slot()
        RUNNER.release_start_slot(slot, 0.2)
        with mock.patch.object(RUNNER, "START_SLOT_ATTEMPTS", 1), \
                mock.patch.object(RUNNER, "START_SLOT_POLL", 0):
            self.assertIsNone(RUNNER.acquire_start_slot())
        RUNNER.time.sleep(0.3)
        next_slot = RUNNER.acquire_start_slot()
        self.assertIsNotNone(next_slot)
        os.close(next_slot)


class ProviderCloseTests(unittest.TestCase):
    def test_provider_that_ignores_hangup_is_killed_after_the_grace_period(self):
        child = RUNNER.subprocess.Popen(
            ["/usr/bin/python3", "-c",
             "import signal,time;signal.signal(signal.SIGHUP,signal.SIG_IGN);print('ready',flush=True);time.sleep(30)"],
            stdout=RUNNER.subprocess.PIPE, start_new_session=True)
        self.addCleanup(child.stdout.close)
        self.addCleanup(child.wait)
        self.addCleanup(RUNNER.signal_group, child.pid, RUNNER.signal.SIGKILL)
        child.stdout.readline()
        with mock.patch.object(RUNNER, "PROVIDER_CLOSE_GRACE", 0.1):
            RUNNER.end_provider(child.pid)
        self.assertEqual(child.wait(timeout=5), -RUNNER.signal.SIGKILL)

    def test_ending_a_provider_that_already_exited_is_harmless(self):
        with mock.patch.object(RUNNER.os, "killpg", side_effect=ProcessLookupError), \
                mock.patch.object(RUNNER, "PROVIDER_CLOSE_GRACE", 0):
            RUNNER.end_provider(4321)


class RunnerLifecycleTests(unittest.TestCase):
    def setUp(self):
        for name, value in (("login_shell_path", ""), ("ensure_claude_trust", None),
                            ("acquire_start_slot", None), ("start_summarizer", None)):
            patcher = mock.patch.object(RUNNER, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def base_response(self, code=0):
        return {
            "ok": True,
            "agent": {"name": "A", "provider": "claude", "workspace": "/tmp",
                      "system_prompt": "system", "startup_prompt": "start",
                      "model": "", "permission_mode": "", "advanced_args": []},
            "run": {"session_id": "known"},
        }, mock.Mock(pid=4321, wait=mock.Mock(return_value=code))

    def test_reopened_memory_history_is_not_closed_after_its_turn(self):
        response, child = self.base_response()
        response["agent"]["omni_managed"] = "memory_consolidator:v1"
        response["run"].update(reason="resumed", resume_session_id="known")
        captured = {}

        def lifecycle(agent, *args, **kwargs):
            captured["agent"] = agent
            return mock.Mock()

        with mock.patch.object(RUNNER.sys, "argv", ["omni-runner", "run"]), \
                mock.patch.object(RUNNER, "request", return_value=response), \
                mock.patch.object(RUNNER.TurnTracker, "transcript", return_value=Path("/tmp/known.jsonl")), \
                mock.patch.object(RUNNER.TurnTracker, "baseline"), \
                mock.patch.object(RUNNER.subprocess, "Popen", return_value=child), \
                mock.patch.object(RUNNER, "SessionLifecycle", side_effect=lifecycle), \
                mock.patch.object(RUNNER, "watch_turn"), \
                mock.patch.object(RUNNER, "report_finished"), \
                mock.patch.object(RUNNER.signal, "signal"):
            RUNNER.main()
        self.assertNotIn("omni_managed", captured["agent"])

    def test_missing_run_exits_before_starting_provider(self):
        with mock.patch.object(RUNNER.sys, "argv", ["omni-runner", "missing"]), \
                mock.patch.object(RUNNER, "request", return_value={"ok": False,
                                                                    "error": "Run not found"}), \
                mock.patch.object(RUNNER.subprocess, "Popen") as popen, \
                self.assertRaisesRegex(SystemExit, "Run not found"):
            RUNNER.main()
        popen.assert_not_called()

    def test_missing_automatic_resume_transcript_starts_fresh_but_manual_resume_is_preserved(self):
        for reason in ("scheduled", "resumed"):
            with self.subTest(reason=reason):
                response, child = self.base_response()
                response["run"].update(resume_session_id="old", session_id="old", reason=reason)
                requests = []

                def request(payload):
                    requests.append(payload)
                    return response if payload["op"] == "run_spec" else {"ok": True}

                with mock.patch.object(RUNNER.sys, "argv", ["omni-runner", "new-run"]), \
                        mock.patch.object(RUNNER, "request", side_effect=request), \
                        mock.patch.object(RUNNER.TurnTracker, "transcript", return_value=None), \
                        mock.patch.object(RUNNER.subprocess, "Popen", return_value=child) as popen, \
                        mock.patch.object(RUNNER.signal, "signal"), redirect_stdout(io.StringIO()):
                    RUNNER.main()
                argv = popen.call_args.args[0]
                self.assertEqual("--resume" in argv, reason == "resumed")
                started = next(item for item in requests if item["op"] == "runner_started")
                self.assertEqual(started["session_id"], "old" if reason == "resumed" else "new-run")

    def test_completed_run_reports_lifecycle_and_exits(self):
        response, child = self.base_response(code=0)
        requests = []

        def request(payload):
            requests.append(payload)
            return response if payload["op"] == "run_spec" else {"ok": True}

        with mock.patch.object(RUNNER.sys, "argv", ["omni-runner", "run-1"]), \
                mock.patch.object(RUNNER, "request", side_effect=request), \
                mock.patch.object(RUNNER.subprocess, "Popen", return_value=child) as popen, \
                mock.patch.object(RUNNER.signal, "signal"), \
                mock.patch.object(RUNNER.os, "getpid", return_value=1234), \
                mock.patch.object(RUNNER.time, "monotonic", side_effect=[10, 17]), \
                mock.patch.object(RUNNER.time, "sleep") as sleep, \
                redirect_stdout(io.StringIO()) as output:
            RUNNER.main()

        sleep.assert_not_called()
        popen.assert_called_once()
        self.assertEqual(popen.call_args.kwargs["cwd"], "/tmp")
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        environment = popen.call_args.kwargs["env"]
        self.assertEqual(environment["OMNI_RUN_ID"], "run-1")
        self.assertEqual(environment["OMNI_AGENT"], response["agent"]["name"])
        self.assertIn({"op": "runner_started", "run_id": "run-1", "pid": 1234,
                       "pgid": 4321, "session_id": "known"}, requests)
        self.assertIn({"op": "runner_finished", "run_id": "run-1", "state": "completed",
                       "reason": "exit 0"}, requests)
        self.assertIn("completed: exit 0 · 7s", output.getvalue())

    def test_failed_run_is_reported_even_when_scheduler_disconnects(self):
        response, child = self.base_response(code=7)
        requests = []

        def request(payload):
            requests.append(payload)
            if payload["op"] == "run_spec":
                return response
            if payload["op"] == "runner_finished":
                raise OSError("scheduler restarted")
            return {"ok": True}

        with mock.patch.object(RUNNER.sys, "argv", ["omni-runner", "run-2"]), \
                mock.patch.object(RUNNER, "request", side_effect=request), \
                mock.patch.object(RUNNER.subprocess, "Popen", return_value=child), \
                mock.patch.object(RUNNER.signal, "signal"), \
                mock.patch.object(RUNNER.os, "getpid", return_value=1234), \
                mock.patch.object(RUNNER.time, "monotonic", side_effect=[20, 23]), \
                redirect_stdout(io.StringIO()) as output:
            RUNNER.main()

        self.assertIn({"op": "runner_finished", "run_id": "run-2", "state": "failed",
                       "reason": "exit 7"}, requests)
        self.assertIn("failed: exit 7 · 3s", output.getvalue())

    def test_closing_the_tab_ends_the_provider_and_records_closed(self):
        response, child = self.base_response(code=-1)
        requests, handlers = [], {}

        def request(payload):
            requests.append(payload)
            return response if payload["op"] == "run_spec" else {"ok": True}

        def wait():
            handlers[RUNNER.signal.SIGHUP](RUNNER.signal.SIGHUP, None)
            return -1

        child.wait.side_effect = wait
        with mock.patch.object(RUNNER.sys, "argv", ["omni-runner", "run-3"]), \
                mock.patch.object(RUNNER, "request", side_effect=request), \
                mock.patch.object(RUNNER.subprocess, "Popen", return_value=child), \
                mock.patch.object(RUNNER.signal, "signal",
                                  side_effect=lambda number, handler: handlers.update({number: handler})), \
                mock.patch.object(RUNNER, "end_provider") as end_provider, \
                mock.patch.object(RUNNER, "save_run_log") as save_run_log, \
                mock.patch("builtins.print", side_effect=OSError("no terminal")):
            RUNNER.main()

        end_provider.assert_called_once_with(4321)
        save_run_log.assert_not_called()
        self.assertIn(RUNNER.signal.SIGTERM, handlers)
        self.assertIn({"op": "runner_finished", "run_id": "run-3", "state": "closed",
                       "reason": "tab closed"}, requests)

    def test_closing_the_tab_after_the_agent_finished_is_a_completed_run(self):
        response, child = self.base_response(code=-1)
        requests, handlers = [], {}

        def request(payload):
            requests.append(payload)
            return response if payload["op"] == "run_spec" else {"ok": True}

        def wait():
            handlers[RUNNER.signal.SIGHUP](RUNNER.signal.SIGHUP, None)
            return -1

        child.wait.side_effect = wait
        with mock.patch.object(RUNNER.sys, "argv", ["omni-runner", "run-5"]), \
                mock.patch.object(RUNNER, "request", side_effect=request), \
                mock.patch.object(RUNNER.subprocess, "Popen", return_value=child), \
                mock.patch.object(RUNNER.signal, "signal",
                                  side_effect=lambda number, handler: handlers.update({number: handler})), \
                mock.patch.object(RUNNER, "end_provider"), \
                mock.patch.object(RUNNER.TurnTracker, "check", return_value=True), \
                mock.patch.object(RUNNER, "start_summarizer") as summarizer, \
                redirect_stdout(io.StringIO()):
            RUNNER.main()

        operations = [payload["op"] for payload in requests]
        self.assertLess(operations.index("runner_turn"), operations.index("runner_finished"))
        summarizer.assert_called_once()
        self.assertEqual(summarizer.call_args.args[0], "run-5")
        self.assertIsInstance(summarizer.call_args.args[2], float)
        self.assertIn({"op": "runner_finished", "run_id": "run-5", "state": "completed",
                       "reason": "agent finished, then tab closed"}, requests)

    def test_failed_run_keeps_the_provider_error(self):
        response, child = self.base_response(code=1)
        requests = []

        def request(payload):
            requests.append(payload)
            return response if payload["op"] == "run_spec" else {"ok": True}

        with mock.patch.object(RUNNER.sys, "argv", ["omni-runner", "run-4"]), \
                mock.patch.object(RUNNER, "request", side_effect=request), \
                mock.patch.object(RUNNER.subprocess, "Popen", return_value=child), \
                mock.patch.object(RUNNER.signal, "signal"), \
                mock.patch.object(RUNNER.ErrorTail, "text", return_value="Session ID in use"), \
                mock.patch.object(RUNNER, "save_run_log") as save_run_log, \
                redirect_stdout(io.StringIO()):
            RUNNER.main()

        save_run_log.assert_called_once_with("run-4", "Session ID in use")
        self.assertIn({"op": "runner_finished", "run_id": "run-4", "state": "failed",
                       "reason": "exit 1: Session ID in use"}, requests)

    def test_unstartable_provider_is_reported_instead_of_crashing_the_runner(self):
        failures = (FileNotFoundError(2, "No such file or directory", "claude"),
                    PermissionError(13, "Permission denied", "claude"))
        for failure in failures:
            with self.subTest(failure=failure):
                response, _ = self.base_response()
                requests = []

                def request(payload):
                    requests.append(payload)
                    return response if payload["op"] == "run_spec" else {"ok": True}

                with mock.patch.object(RUNNER.sys, "argv", ["omni-runner", "run-4"]), \
                        mock.patch.object(RUNNER, "request", side_effect=request), \
                        mock.patch.object(RUNNER, "provider_search_path", return_value="/bin"), \
                        mock.patch.object(RUNNER.subprocess, "Popen", side_effect=failure), \
                        mock.patch.object(RUNNER.time, "monotonic", side_effect=[40, 41]), \
                        redirect_stdout(io.StringIO()) as output:
                    RUNNER.main()

                finished = [item for item in requests if item["op"] == "runner_finished"]
                self.assertEqual(len(finished), 1)
                self.assertEqual(finished[0]["state"], "failed")
                self.assertIn("could not start claude", finished[0]["reason"])
                self.assertNotIn("runner_started", [item["op"] for item in requests])
                self.assertIn("failed: could not start claude", output.getvalue())

    def test_only_claude_runs_record_trust_and_before_the_provider_starts(self):
        for provider, expected in (("claude", 1), ("codex", 0)):
            with self.subTest(provider=provider):
                response, child = self.base_response(code=0)
                response["agent"]["provider"] = provider
                events = []
                RUNNER.ensure_claude_trust.reset_mock()
                RUNNER.ensure_claude_trust.side_effect = lambda *_: events.append("trust")

                def popen(*_, **__):
                    events.append("provider")
                    return child

                with mock.patch.object(RUNNER.sys, "argv", ["omni-runner", "run-6"]), \
                        mock.patch.object(RUNNER, "request",
                                          side_effect=lambda payload: response if payload["op"] == "run_spec"
                                          else {"ok": True}), \
                        mock.patch.object(RUNNER.subprocess, "Popen", side_effect=popen), \
                        mock.patch.object(RUNNER, "codex_session_files", return_value=set()), \
                        mock.patch.object(RUNNER.threading, "Thread"), \
                        mock.patch.object(RUNNER.signal, "signal"), \
                        redirect_stdout(io.StringIO()):
                    RUNNER.main()
                self.assertEqual(RUNNER.ensure_claude_trust.call_count, expected)
                self.assertEqual(events[-1], "provider")

    def test_provider_receives_the_resolved_search_path(self):
        response, child = self.base_response(code=0)

        def request(payload):
            return response if payload["op"] == "run_spec" else {"ok": True}

        with mock.patch.object(RUNNER.sys, "argv", ["omni-runner", "run-5"]), \
                mock.patch.object(RUNNER, "request", side_effect=request), \
                mock.patch.object(RUNNER, "provider_search_path", return_value="/resolved"), \
                mock.patch.object(RUNNER.subprocess, "Popen", return_value=child) as popen, \
                mock.patch.object(RUNNER.signal, "signal"), \
                mock.patch.object(RUNNER.time, "monotonic", side_effect=[50, 51]), \
                redirect_stdout(io.StringIO()):
            RUNNER.main()

        self.assertEqual(popen.call_args.kwargs["env"]["PATH"], "/resolved")

    def test_new_codex_run_discovers_and_reports_provider_session(self):
        with tempfile.TemporaryDirectory() as root:
            old = Path(root) / "old.jsonl"
            new = Path(root) / "new.jsonl"
            old.write_text("{}")
            new.write_text("{}")
            response, child = self.base_response(code=0)
            response["agent"].update(provider="codex", workspace=root)
            response["run"] = {}
            requests = []

            def request(payload):
                requests.append(payload)
                return response if payload["op"] == "run_spec" else {"ok": True}

            trackers = []

            class ImmediateThread:
                def __init__(self, target, daemon, args=()):
                    self.target = target
                    self.daemon = daemon
                    self.args = args

                def start(self):
                    if self.target is RUNNER.watch_turn:
                        trackers.append(self.args[0].tracker)
                    else:
                        self.target()

                def join(self, timeout=None):
                    pass

            with mock.patch.object(RUNNER.sys, "argv", ["omni-runner", "run-3"]), \
                    mock.patch.object(RUNNER, "request", side_effect=request), \
                    mock.patch.object(RUNNER.subprocess, "Popen", return_value=child), \
                    mock.patch.object(RUNNER.signal, "signal"), \
                    mock.patch.object(RUNNER.os, "getpid", return_value=1234), \
                    mock.patch.object(RUNNER, "codex_session_files",
                                      side_effect=[{old}, {old, new}]), \
                    mock.patch.object(RUNNER, "codex_session_id",
                                      return_value="provider-session"), \
                    mock.patch.object(RUNNER, "ErrorTail", return_value=mock.Mock(text=mock.Mock(return_value=""))), \
                mock.patch.object(RUNNER.threading, "Thread", ImmediateThread), \
                    mock.patch.object(RUNNER.time, "monotonic", side_effect=[30, 31]), \
                        redirect_stdout(io.StringIO()):
                RUNNER.main()

        self.assertIn({"op": "runner_session", "run_id": "run-3",
                       "session_id": "provider-session"}, requests)
        self.assertEqual(trackers[0].session_id, "provider-session")


class SessionContinuationTests(unittest.TestCase):
    def agent(self, provider="codex", **values):
        return {"name": "Loop", "provider": provider, "workspace": "/tmp", "kind": "loop",
                "startup_prompt": "do the next unit", **values}

    def test_parallel_codex_sessions_in_same_workspace_bind_only_their_run_note(self):
        with tempfile.TemporaryDirectory() as root:
            paths = []
            for name, run in (("session-a", "run-a-unique"), ("session-b", "run-b-unique")):
                path = Path(root) / (name + ".jsonl")
                path.write_text(json.dumps({"type": "session_meta", "payload": {
                    "cwd": "/tmp/shared", "id": name}}) + "\n" + json.dumps({
                    "type": "response_item", "payload": {"role": "developer", "content": run}}) + "\n")
                paths.append(path)
            self.assertEqual(RUNNER.codex_session_id(paths[0], "/tmp/shared", "run-a-unique"), "session-a")
            self.assertIsNone(RUNNER.codex_session_id(paths[1], "/tmp/shared", "run-a-unique"))
            self.assertIsNone(RUNNER.codex_session_id(paths[0], "/different", "run-a-unique"))

    def test_automatic_resume_gets_new_work_and_current_budget_but_manual_resume_does_not(self):
        for provider in ("codex", "claude"):
            agent = self.agent(provider, run_budget_seconds=900, wrap_up_seconds=60)
            argv = RUNNER.provider_argv(agent, "run", "session", automatic_resume=True)
            self.assertIn("do the next unit", argv[-1])
            self.assertIn("900 seconds", argv[-1])
            self.assertIn("not a kill deadline", argv[-1])
            manual = RUNNER.provider_argv(agent, "run", "session")
            self.assertFalse(any("do the next unit" in part for part in manual))

    def test_resumed_transcript_cannot_finish_new_run_from_old_completion(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "session.jsonl"
            event = {"type": "event_msg", "payload": {"type": "task_complete",
                      "last_agent_message": "old result"}}
            path.write_text(json.dumps(event) + "\n")
            tracker = RUNNER.TurnTracker("codex", "session")
            tracker.path = path
            tracker.baseline()
            self.assertFalse(tracker.check())
            with path.open("a") as out:
                out.write(json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}) + "\n")
                event["payload"]["last_agent_message"] = "new result"
                out.write(json.dumps(event) + "\n")
            self.assertTrue(tracker.check())
            self.assertEqual(tracker.final_message, "new result")

    def test_budget_warns_once_and_never_terminates_busy_provider(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "session.jsonl"
            path.write_text("")
            tracker = RUNNER.TurnTracker("claude", "dc508fe4-8411-4e34-b772-54d9a747b811")
            tracker.path = path
            lifecycle = RUNNER.SessionLifecycle(self.agent("claude"), "run", tracker, 100)
            with mock.patch.object(RUNNER, "request", return_value={"ok": True, "receipt": {"state": "queued"}}) as request:
                lifecycle.poll(579)
                request.assert_not_called()
                lifecycle.poll(580)
                lifecycle.poll(90000)
            messages = [call.args[0] for call in request.call_args_list if call.args[0]["op"] == "send_session_message"]
            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0]["session_id"], f"claude:{tracker.session_id}")
            self.assertEqual(messages[0]["message"], lifecycle.warning_message)
            self.assertNotIn("run_id", messages[0])
            self.assertNotIn("runner_input", [call.args[0]["op"] for call in request.call_args_list])
            self.assertFalse(lifecycle.ended)

    def test_reuse_finishes_idle_provider_once_but_fresh_policy_keeps_it_open(self):
        tracker = mock.Mock(finished=True)
        for policy in ("fresh", "resume"):
            lifecycle = RUNNER.SessionLifecycle(self.agent(kind="scheduled", session_policy=policy), "run", tracker, 0)
            with mock.patch.object(RUNNER, "request", return_value={"ok": True, "state": "delivered"}):
                lifecycle.poll(10)
                lifecycle.poll(20)
            self.assertEqual(lifecycle.ended, policy == "resume")

    def test_reused_session_compacts_only_once_its_context_is_large(self):
        claude = lambda tokens: {"type": "assistant", "message": {"usage": {
            "input_tokens": 2, "cache_read_input_tokens": tokens, "cache_creation_input_tokens": 0}}}
        codex = lambda tokens: {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "last_token_usage": {"input_tokens": tokens}, "model_context_window": 200_000}}}
        cases = (("claude", claude(150_000), {}, True), ("claude", claude(50_000), {}, False),
                 ("claude", claude(150_000), {"session_policy": "fresh"}, False),
                 ("claude", claude(150_000), {"kind": "loop", "auto_compact": False}, False),
                 ("codex", codex(110_000), {}, True), ("codex", codex(90_000), {}, False))
        with tempfile.TemporaryDirectory() as root:
            for provider, event, fields, expected in cases:
                path = Path(root) / "session.jsonl"
                path.write_text(json.dumps(event) + "\n")
                tracker = mock.Mock(finished=True, provider=provider, transcript=lambda: path)
                agent = {key: value for key, value in self.agent(provider, kind="scheduled").items()
                         if key != "session_policy"}
                lifecycle = RUNNER.SessionLifecycle({**agent, **fields}, "run", tracker, 0)
                self.assertEqual(lifecycle.wants_compaction(), expected, (provider, event, fields))

    def test_managed_memory_run_closes_after_its_final_turn_instead_of_idling(self):
        # Nobody answers an unattended housekeeping run, so leaving it at the
        # prompt only showed it as "Idle · waiting for you" until the next
        # occurrence replaced it.
        tracker = mock.Mock(finished=True)
        agent = self.agent(kind="scheduled", session_policy="fresh", omni_managed="memory_consolidator:v1")
        lifecycle = RUNNER.SessionLifecycle(agent, "run", tracker, 0)
        with mock.patch.object(RUNNER, "request", return_value={"ok": True, "state": "delivered"}) as request:
            lifecycle.poll(10)
            lifecycle.poll(20)
        self.assertTrue(lifecycle.ended)
        finishes = [call.args[0] for call in request.call_args_list
                    if call.args[0]["op"] == "runner_input" and call.args[0]["action"] == "finish"]
        self.assertEqual(len(finishes), 1)

    def test_managed_memory_run_waits_for_its_turn_and_loops_never_close(self):
        for finished, kind in ((False, "scheduled"), (True, "loop")):
            with self.subTest(finished=finished, kind=kind):
                tracker = mock.Mock(finished=finished)
                agent = self.agent(kind=kind, session_policy="fresh", run_budget_seconds=0,
                                   omni_managed="memory_consolidator:v1")
                lifecycle = RUNNER.SessionLifecycle(agent, "run", tracker, 0)
                with mock.patch.object(RUNNER, "request", return_value={"ok": True, "state": "delivered"}) as request:
                    lifecycle.poll(10)
                    lifecycle.poll(20)
                self.assertFalse(lifecycle.ended)
                self.assertNotIn("runner_input", [call.args[0]["op"] for call in request.call_args_list])

    def test_compaction_requires_new_persisted_boundary_and_never_repeats(self):
        for provider, boundary in (("codex", {"type": "compacted", "payload": {}}),
                                   ("claude", {"type": "system", "subtype": "compact_boundary"})):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as root:
                path = Path(root) / "session.jsonl"
                path.write_text(json.dumps(boundary) + "\n")
                tracker = mock.Mock(finished=True, provider=provider,
                                    transcript=mock.Mock(return_value=path))
                lifecycle = RUNNER.SessionLifecycle(self.agent(provider, kind="scheduled", session_policy="resume", compact_on_end=True),
                                                    "run", tracker, 0)
                with mock.patch.object(RUNNER, "request", return_value={"ok": True, "state": "delivered"}) as request:
                    lifecycle.poll(10)
                    lifecycle.poll(100000)
                    self.assertFalse(lifecycle.ended)
                    with path.open("a") as output:
                        output.write(json.dumps(boundary) + "\n")
                    lifecycle.poll(100001)
                    lifecycle.poll(100002)
                inputs = [call for call in request.call_args_list if call.args[0]["op"] == "runner_input"]
                self.assertEqual(inputs, [mock.call({"op": "runner_input", "run_id": "run", "action": "compact"}),
                                          mock.call({"op": "runner_input", "run_id": "run", "action": "finish"})])
                self.assertTrue(lifecycle.ended)

    def test_refused_compaction_leaves_session_alive_even_if_boundary_later_appears(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "session.jsonl"
            path.write_text("")
            tracker = mock.Mock(finished=True, provider="codex", transcript=mock.Mock(return_value=path))
            lifecycle = RUNNER.SessionLifecycle(self.agent(compact_on_end=True), "run", tracker, 0)
            with mock.patch.object(RUNNER, "request", return_value={"ok": False}):
                lifecycle.poll(10)
                path.write_text('{"type":"compacted"}\n')
                lifecycle.poll(10000)
            self.assertFalse(lifecycle.ended)

    def test_compaction_without_confirmation_notifies_once_without_killing(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "session.jsonl"
            path.write_text("")
            tracker = mock.Mock(finished=True, provider="codex", transcript=mock.Mock(return_value=path))
            lifecycle = RUNNER.SessionLifecycle(self.agent(compact_on_end=True), "run", tracker, 0)
            with mock.patch.object(RUNNER, "request", return_value={"ok": True, "state": "delivered"}) as request:
                lifecycle.poll(10)
                lifecycle.poll(310)
                lifecycle.poll(400)
            notices = [call.args[0] for call in request.call_args_list if call.args[0]["op"] == "notify"]
            self.assertEqual(len(notices), 1)
            self.assertIn("five minutes", notices[0]["body"])
            self.assertFalse(lifecycle.ended)

    def test_fresh_policy_can_compact_without_closing_session(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "session.jsonl"
            path.write_text("")
            tracker = mock.Mock(finished=True, provider="codex", transcript=mock.Mock(return_value=path))
            lifecycle = RUNNER.SessionLifecycle(self.agent(compact_on_end=True, session_policy="fresh"),
                                                "run", tracker, 0)
            with mock.patch.object(RUNNER, "request", return_value={"ok": True, "state": "delivered"}) as request:
                lifecycle.poll(10)
                path.write_text('{"type":"compacted"}\n')
                lifecycle.poll(20)
            self.assertTrue(lifecycle.compact_complete)
            self.assertTrue(lifecycle.compact_acknowledged)
            self.assertIn(mock.call({"op": "runner_compacted", "run_id": "run"}), request.call_args_list)
            self.assertFalse(lifecycle.ended)

    def test_fast_human_followup_restores_reporting_after_success_or_timeout(self):
        for compact_succeeds in (False, True):
            with self.subTest(compact_succeeds=compact_succeeds), tempfile.TemporaryDirectory() as root:
                path = Path(root) / "session.jsonl"

                def append(*payloads):
                    with path.open("a") as output:
                        for payload in payloads:
                            output.write(json.dumps({"type": "event_msg", "payload": payload}) + "\n")

                append({"type": "task_complete", "last_agent_message": "work result"})
                tracker = RUNNER.TurnTracker("codex", "session")
                tracker.path = path
                tracker.check()
                reporter = RUNNER.TurnReporter("run", tracker)
                lifecycle = RUNNER.SessionLifecycle(self.agent(compact_on_end=True, session_policy="fresh"),
                                                    "run", tracker, 0)
                with mock.patch.object(RUNNER, "request", return_value={"ok": True, "state": "delivered"}) as request:
                    reporter.report()
                    lifecycle.poll(10)
                    reporter.suspended = True
                    if compact_succeeds:
                        with path.open("a") as output:
                            output.write('{"type":"compacted"}\n')
                        append({"type": "task_complete", "last_agent_message": "compact result"})
                    lifecycle.poll(310)
                    reporter.report()
                    self.assertEqual(reporter.reported, (True, "work result"))
                    append({"type": "user_message", "message": "Now explain the change"},
                           {"type": "task_started"},
                           {"type": "task_complete", "last_agent_message": "followup result"})
                    lifecycle.resume_reporting(reporter)
                    reporter.report()
                    lifecycle.poll(320)
                self.assertTrue(lifecycle.human_intervened)
                self.assertFalse(reporter.suspended)
                self.assertEqual(reporter.reported, (True, "followup result"))
                self.assertFalse(lifecycle.ended)
                finishes = [call for call in request.call_args_list
                            if call.args[0].get("action") == "finish"]
                self.assertEqual(finishes, [])

    def test_compaction_synthetic_turn_does_not_resume_human_reporting(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "session.jsonl"
            for provider, events in (
                ("codex", [{"type": "event_msg", "payload": {"type": "task_started"}},
                           {"type": "event_msg", "payload": {"type": "user_message", "message": "/compact"}},
                           {"type": "response_item", "payload": {"type": "message", "role": "user",
                               "content": [{"type": "input_text", "text": "/compact"}]}},
                           {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                               "content": [{"type": "input_text", "text": "quoted human input"}]}}]),
                ("claude", [{"type": "user", "isCompactSummary": True, "message": {"content": "summary"}},
                            {"type": "user", "message": {"content": "<command-name>/compact</command-name>"}}])):
                path.write_text("\n".join(json.dumps(event) for event in events) + "\n")
                self.assertIsNone(RUNNER.user_turn_since(path, 0, provider))

    def test_response_item_followup_after_compaction_is_reported_and_kept_open(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "session.jsonl"
            path.write_text(json.dumps({"type": "event_msg", "payload": {
                "type": "task_complete", "last_agent_message": "original work"}}) + "\n")
            tracker = RUNNER.TurnTracker("codex", "session")
            tracker.path = path
            tracker.check()
            reporter = RUNNER.TurnReporter("run", tracker)
            lifecycle = RUNNER.SessionLifecycle(
                self.agent(kind="scheduled", compact_on_end=True, session_policy="resume"),
                "run", tracker, 0)
            with mock.patch.object(RUNNER, "request", return_value={"ok": True, "state": "delivered"}) as request:
                reporter.report()
                lifecycle.poll(10)
                reporter.suspended = True
                events = [
                    {"type": "compacted"},
                    {"type": "response_item", "payload": {"type": "message", "role": "user",
                        "content": [{"type": "input_text", "text": "Please explain this change"}]}},
                    {"type": "event_msg", "payload": {"type": "task_started"}},
                    {"type": "event_msg", "payload": {"type": "task_complete",
                                                         "last_agent_message": "human followup result"}},
                ]
                with path.open("a") as output:
                    for event in events:
                        output.write(json.dumps(event) + "\n")
                RUNNER.watch_turn(reporter, mock.Mock(wait=mock.Mock(side_effect=[False, True])), lifecycle)
            self.assertEqual(reporter.reported, (True, "human followup result"))
            self.assertTrue(lifecycle.human_intervened)
            self.assertFalse(lifecycle.ended)
            self.assertNotIn("finish", [call.args[0].get("action") for call in request.call_args_list])

    def test_claude_compaction_local_output_keeps_loop_idle_for_continuation(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "session.jsonl"
            path.write_text(json.dumps({"type": "assistant", "message": {
                "stop_reason": "end_turn", "content": "work result"}}) + "\n")
            tracker = RUNNER.TurnTracker("claude", "session")
            tracker.path = path
            tracker.check()
            reporter = RUNNER.TurnReporter("run", tracker)
            lifecycle = RUNNER.SessionLifecycle(self.agent("claude", compact_on_end=True),
                                                "run", tracker, 0)
            with mock.patch.object(RUNNER, "request", return_value={"ok": True, "state": "delivered"}):
                reporter.report()
                lifecycle.poll(10)
                reporter.suspended = True
                events = [
                    {"type": "system", "subtype": "compact_boundary"},
                    {"type": "user", "message": {"content":
                        "<local-command-stdout>\u001b[2mCompacted \u001b[22m</local-command-stdout>"}},
                ]
                with path.open("a") as output:
                    for event in events:
                        output.write(json.dumps(event) + "\n")
                RUNNER.watch_turn(reporter, mock.Mock(wait=mock.Mock(side_effect=[False, True])), lifecycle)
            self.assertFalse(lifecycle.human_intervened)
            self.assertTrue(tracker.finished)
            self.assertTrue(lifecycle.compact_acknowledged)
            self.assertEqual(reporter.reported, (True, "work result"))


class CodexQueuedWarningTests(unittest.TestCase):
    session_id = "dc508fe4-8411-4e34-b772-54d9a747b811"

    def test_fake_queue_receives_only_owned_uuid_message_and_resolved_environment(self):
        with tempfile.TemporaryDirectory() as root:
            binary = Path(root) / "codex"
            capture = Path(root) / "capture.json"
            binary.write_text("#!" + RUNNER.sys.executable + "\n"
                              "import json, os, sys\n"
                              "from pathlib import Path\n"
                              "Path(os.environ['QUEUE_CAPTURE']).write_text(json.dumps({"
                              "'args':sys.argv[1:], 'cwd':os.getcwd(), 'path':os.environ['PATH']}))\n")
            binary.chmod(0o755)
            environment = {"PATH": root, "QUEUE_CAPTURE": str(capture)}
            message = "[OmniTerm wrap-up run] Finish safely; $(never shell)"
            state = RUNNER.queue_codex_warning(self.session_id, message, root, environment)
            self.assertEqual(state, "queued")
            recorded = json.loads(capture.read_text())
            self.assertEqual(recorded["args"], ["queue", "--thread", self.session_id, "--message", message])
            self.assertEqual(recorded["path"], root)
            self.assertEqual(Path(recorded["cwd"]).resolve(), Path(root).resolve())

    def test_queue_rejects_names_and_marks_timeouts_uncertain_without_retry(self):
        with mock.patch.object(RUNNER.subprocess, "run") as run:
            self.assertEqual(RUNNER.queue_codex_warning("session-name", "message", "/tmp", {}), "failed")
            run.assert_not_called()
        for error, state in ((RUNNER.subprocess.TimeoutExpired("codex", 15), "uncertain"),
                             (FileNotFoundError("no codex"), "failed")):
            with mock.patch.object(RUNNER.subprocess, "run", side_effect=error) as run:
                self.assertEqual(RUNNER.queue_codex_warning(self.session_id, "message", "/tmp", {}), state)
                run.assert_called_once()
                self.assertEqual(run.call_args.kwargs["timeout"], RUNNER.QUEUE_TIMEOUT_SECONDS)
                self.assertNotIn("shell", run.call_args.kwargs)

    def test_queued_warning_requires_user_receipt_and_keeps_loop_open_after_completion(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "session.jsonl"

            def append(payload, kind="event_msg"):
                with path.open("a") as output:
                    output.write(json.dumps({"type": kind, "payload": payload}) + "\n")

            append({"type": "task_started"})
            tracker = RUNNER.TurnTracker("codex", self.session_id)
            tracker.path = path
            tracker.check()
            agent = {"name": "Loop", "provider": "codex", "kind": "loop", "workspace": root}
            lifecycle = RUNNER.SessionLifecycle(agent, "unique-run", tracker, 0, {"PATH": "/provider/bin"})
            with mock.patch.object(RUNNER, "queue_codex_warning", return_value="queued") as queue, \
                    mock.patch.object(RUNNER, "request", return_value={"ok": True, "state": "delivered"}) as request:
                lifecycle.poll(480)
                queue.assert_called_once_with(self.session_id, lifecycle.warning_message, root,
                                              {"PATH": "/provider/bin"})
                append({"type": "task_complete", "last_agent_message": "original work finished"})
                tracker.check()
                lifecycle.poll(490)
                self.assertFalse(lifecycle.finish_requested)
                append({"role": "assistant", "content": lifecycle.warning_message}, "response_item")
                lifecycle.poll(495)
                self.assertFalse(lifecycle.warning_consumed)
                append({"type": "user_message", "message": lifecycle.warning_message})
                lifecycle.poll(500)
                self.assertTrue(lifecycle.warning_consumed)
                self.assertFalse(tracker.finished)
                self.assertFalse(lifecycle.finish_requested)
                append({"type": "task_complete", "last_agent_message": "verified handoff"})
                tracker.check()
                lifecycle.poll(510)
                self.assertFalse(lifecycle.finish_requested)
                inputs = [call.args[0]["action"] for call in request.call_args_list
                          if call.args[0]["op"] == "runner_input"]
                self.assertEqual(inputs, [])

    def test_unconsumed_queue_notifies_once_and_never_closes_or_requeues(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "session.jsonl"
            path.write_text("")
            tracker = RUNNER.TurnTracker("codex", self.session_id)
            tracker.path = path
            agent = {"name": "Loop", "provider": "codex", "kind": "loop", "workspace": root}
            lifecycle = RUNNER.SessionLifecycle(agent, "unique-run", tracker, 0, {"PATH": "/provider/bin"})
            with mock.patch.object(RUNNER, "queue_codex_warning", return_value="queued") as queue, \
                    mock.patch.object(RUNNER, "request", return_value={"ok": True}) as request:
                lifecycle.poll(480)
                tracker.finished = True
                lifecycle.poll(780)
                lifecycle.poll(90000)
            queue.assert_called_once()
            self.assertFalse(lifecycle.finish_requested)
            notices = [call.args[0] for call in request.call_args_list if call.args[0]["op"] == "notify"]
            self.assertEqual(len(notices), 1)
            self.assertIn("five minutes", notices[0]["body"])


class ClaudeQueuedWarningTests(unittest.TestCase):
    session_id = "dc508fe4-8411-4e34-b772-54d9a747b811"

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            "warning_test_messages", Path(__file__).parents[1] / "helpers/omni_messages.py")
        cls.messages = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.messages)

    def setup_warning(self, root):
        path = Path(root) / "session.jsonl"
        path.write_text(json.dumps({"type": "user", "message": {"content": "work"}}) + "\n")
        tracker = RUNNER.TurnTracker("claude", self.session_id)
        tracker.path = path
        tracker.check()
        agent = {"name": "Loop", "provider": "claude", "kind": "loop", "workspace": root,
                 "compact_on_end": True}
        return path, RUNNER.SessionLifecycle(agent, "same-run", tracker, 0)

    def append(self, path, *events):
        with path.open("a") as output:
            for event in events:
                output.write(json.dumps(event) + "\n")

    def test_real_peer_schema_and_consumption_gate_before_compaction(self):
        with tempfile.TemporaryDirectory() as root:
            path, lifecycle = self.setup_warning(root)
            messenger = self.messages.Messenger(home=root)
            receipts = []

            def response(payload):
                if payload["op"] == "send_session_message":
                    receipt = messenger.prepare({"id": f"claude:{self.session_id}"}, payload)
                    receipts.append(receipt)
                    return {"ok": True, "receipt": receipt}
                return {"ok": True, "state": "delivered"}

            complete = {"type": "assistant", "message": {"stop_reason": "end_turn", "content": "done"}}
            with mock.patch.object(messenger, "_claude_registry", return_value={}), \
                    mock.patch.object(RUNNER, "request", side_effect=response) as request:
                lifecycle.poll(480)
                self.assertEqual(len(receipts), 1)
                canonical = self.messages.provider_message_text(receipts[0])
                self.append(path, complete, {"type": "assistant", "message": {"content": canonical}})
                lifecycle.tracker.check()
                lifecycle.poll(490)
                self.assertTrue(lifecycle.warning_pending())
                self.assertFalse(lifecycle.compact_requested)
                self.append(path, {"type": "user", "message": {"content": canonical}})
                lifecycle.poll(500)
                self.assertTrue(lifecycle.warning_consumed)
                self.assertFalse(lifecycle.tracker.finished)
                self.assertFalse(lifecycle.compact_requested)
                self.append(path, complete)
                lifecycle.tracker.check()
                lifecycle.poll(510)
            self.assertEqual(len(receipts), 1)
            self.assertEqual([call.args[0]["action"] for call in request.call_args_list
                              if call.args[0]["op"] == "runner_input"], ["compact"])
            self.assertFalse(lifecycle.ended)

    def test_uncertain_peer_send_is_not_retried_and_prevents_compaction(self):
        for outcome in (OSError("lost acknowledgement"), {"ok": True, "receipt": {"state": "unknown"}},
                        {"ok": True, "receipt": []}):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as root:
                _, lifecycle = self.setup_warning(root)

                def response(payload):
                    if payload["op"] == "send_session_message":
                        if isinstance(outcome, Exception):
                            raise outcome
                        return outcome
                    return {"ok": True}

                with mock.patch.object(RUNNER, "request", side_effect=response) as request:
                    lifecycle.poll(480)
                    lifecycle.tracker.finished = True
                    lifecycle.poll(780)
                    lifecycle.poll(90000)
                operations = [call.args[0]["op"] for call in request.call_args_list]
                self.assertEqual(operations.count("send_session_message"), 1)
                self.assertEqual(operations.count("notify"), 1)
                self.assertNotIn("runner_input", operations)
                self.assertTrue(lifecycle.warning_pending())
                self.assertFalse(lifecycle.ended)


class PersistentLoopTests(unittest.TestCase):
    session_id = "dc508fe4-8411-4e34-b772-54d9a747b811"
    token = "a9fb880d-5b8b-40f6-b561-62648bfa2159"

    def setup_loop(self, root, finished=True, **settings):
        path = Path(root) / "session.jsonl"
        path.write_text(json.dumps({"type": "event_msg", "payload": {
            "type": "task_complete" if finished else "task_started", "last_agent_message": "old result"}}) + "\n")
        tracker = RUNNER.TurnTracker("codex", self.session_id)
        tracker.path = path
        tracker.check()
        agent = {"kind": "loop", "name": "Loop", "provider": "codex", "workspace": root,
                 "startup_prompt": "next slice", **settings}
        lifecycle = RUNNER.SessionLifecycle(agent, "one-run", tracker, 0, {"PATH": "/providers"})
        reporter = RUNNER.TurnReporter("one-run", tracker)
        return path, lifecycle, reporter, RUNNER.LoopContinuation(lifecycle, reporter)

    def append(self, path, *payloads):
        with path.open("a") as output:
            for payload in payloads:
                output.write(json.dumps({"type": "event_msg", "payload": payload}) + "\n")

    def test_idle_cycles_keep_one_run_and_queue_once_until_receipt(self):
        with tempfile.TemporaryDirectory() as root:
            path, lifecycle, reporter, loop = self.setup_loop(root)
            pending = {"ok": True, "pending": True, "token": self.token,
                       "startup_prompt": "next slice", "agent": lifecycle.agent}
            def response(payload):
                if payload["op"] in ("runner_loop_poll", "runner_loop_claim"):
                    return pending
                return {"ok": True}
            lifecycle.warned = True
            previous_warning_request = lifecycle.warning_request_id
            with mock.patch.object(RUNNER, "request", side_effect=response) as request, \
                    mock.patch.object(RUNNER, "queue_codex_warning", return_value="queued") as queue:
                loop.poll(10)
                loop.poll(20)
                self.assertEqual(queue.call_count, 1)
                self.assertIsNotNone(loop.claim)
                self.assertEqual(lifecycle.started, 0)
                self.append(path, {"type": "user_message", "message": loop.message},
                            {"type": "task_complete", "last_agent_message": "new result"})
                loop.poll(30)
                self.assertIsNone(loop.claim)
                self.assertEqual(lifecycle.started, 30)
                self.assertFalse(lifecycle.warned)
                self.assertNotEqual(lifecycle.warning_request_id, previous_warning_request)
                reporter.report()
                self.assertEqual(reporter.reported, (True, "new result"))
                lifecycle.poll(31)
            actions = [call.args[0].get("action") for call in request.call_args_list]
            self.assertNotIn("finish", actions)
            self.assertTrue(all(call.args[0].get("run_id") == "one-run" for call in request.call_args_list))

    def test_busy_or_unconfirmed_compaction_cannot_claim_next_cycle(self):
        for settings in ({}, {"compact_on_end": True}):
            with self.subTest(settings=settings), tempfile.TemporaryDirectory() as root:
                _, lifecycle, _, loop = self.setup_loop(root, finished=bool(settings), **settings)
                pending = {"ok": True, "pending": True, "token": self.token,
                           "startup_prompt": "next slice", "agent": lifecycle.agent}
                with mock.patch.object(RUNNER, "request", return_value=pending) as request, \
                        mock.patch.object(RUNNER, "queue_codex_warning") as queue:
                    loop.poll(10)
                self.assertEqual([call.args[0]["op"] for call in request.call_args_list], ["runner_loop_poll"])
                queue.assert_not_called()

    def test_codex_response_item_receipt_acknowledges_only_exact_user_input(self):
        with tempfile.TemporaryDirectory() as root:
            path, lifecycle, _, loop = self.setup_loop(root)
            pending = {"ok": True, "pending": True, "token": self.token,
                       "startup_prompt": "next slice", "agent": lifecycle.agent}
            def response(payload):
                return pending if payload["op"] in ("runner_loop_poll", "runner_loop_claim") else {"ok": True}
            def append_message(role, text):
                with path.open("a") as output:
                    output.write(json.dumps({"type": "response_item", "payload": {
                        "type": "message", "role": role,
                        "content": [{"type": "input_text", "text": text}]}}) + "\n")
            with mock.patch.object(RUNNER, "request", side_effect=response) as request, \
                    mock.patch.object(RUNNER, "queue_codex_warning", return_value="queued") as queue:
                loop.poll(10)
                append_message("assistant", loop.message)
                append_message("user", loop.message + " unrelated suffix")
                loop.poll(20)
                self.assertIsNotNone(loop.claim)
                append_message("user", loop.message)
                self.append(path, {"type": "task_complete", "last_agent_message": "new result"})
                loop.poll(30)
                self.assertIsNone(loop.claim)
                self.assertEqual(lifecycle.started, 30)
                self.assertIn(mock.call({"op": "runner_loop_ack", "run_id": "one-run", "token": self.token}),
                              request.call_args_list)
                queue.assert_called_once()

    def test_claude_loop_request_passes_real_message_schema(self):
        spec = importlib.util.spec_from_file_location(
            "loop_test_messages", Path(__file__).parents[1] / "helpers/omni_messages.py")
        messages = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(messages)
        with tempfile.TemporaryDirectory() as root:
            _, lifecycle, _, loop = self.setup_loop(root, provider="claude")
            lifecycle.tracker.provider = "claude"
            messenger = messages.Messenger(home=root)
            receipts = []

            def response(payload):
                if payload["op"] == "send_session_message":
                    receipt = messenger.prepare({"id": f"claude:{self.session_id}"}, payload)
                    receipts.append(receipt)
                    return {"ok": True, "receipt": receipt}
                return {"ok": True}

            # Only live endpoint discovery is substituted; the production
            # argument validation and receipt construction run unmodified.
            with mock.patch.object(messenger, "_claude_registry", return_value={}), \
                    mock.patch.object(RUNNER, "request", side_effect=response) as request:
                loop.begin({"token": self.token, "startup_prompt": "next slice",
                            "agent": lifecycle.agent}, 10)
            self.assertEqual(len(receipts), 1)
            self.assertEqual(receipts[0]["state"], "queued")
            self.assertEqual(receipts[0]["message"], loop.message)
            self.assertEqual([call.args[0]["op"] for call in request.call_args_list],
                             ["send_session_message"])

    def test_watch_turn_resets_consumed_cycle_before_old_budget_can_warn(self):
        with tempfile.TemporaryDirectory() as root:
            path, lifecycle, reporter, _ = self.setup_loop(root, run_budget_seconds=60,
                                                         wrap_up_seconds=5)
            pending = {"ok": True, "pending": True, "token": self.token,
                       "startup_prompt": "next slice", "agent": lifecycle.agent}

            def response(payload):
                if payload["op"] in ("runner_loop_poll", "runner_loop_claim"):
                    return pending
                return {"ok": True}

            with mock.patch.object(RUNNER, "request", side_effect=response), \
                    mock.patch.object(RUNNER, "queue_codex_warning", return_value="queued") as queue, \
                    mock.patch.object(RUNNER.time, "monotonic", side_effect=[10, 10, 3600, 3600]):
                waits = 0

                def wait(_):
                    nonlocal waits
                    waits += 1
                    if waits == 2:
                        self.append(path, {"type": "user_message", "message": queue.call_args.args[1]},
                                    {"type": "task_started"})
                    return waits > 2

                RUNNER.watch_turn(reporter, mock.Mock(wait=wait), lifecycle)
            queue.assert_called_once()
            self.assertEqual(lifecycle.started, 3600)
            self.assertFalse(lifecycle.warned)
            self.assertFalse(lifecycle.tracker.finished)

    def test_ambiguous_queue_is_never_retried_or_replaced(self):
        with tempfile.TemporaryDirectory() as root:
            _, lifecycle, _, loop = self.setup_loop(root)
            pending = {"ok": True, "pending": True, "token": self.token,
                       "startup_prompt": "next slice", "agent": lifecycle.agent}
            with mock.patch.object(RUNNER, "request", return_value=pending) as request, \
                    mock.patch.object(RUNNER, "queue_codex_warning", return_value="uncertain") as queue:
                loop.poll(10)
                loop.poll(400)
                loop.poll(90000)
            queue.assert_called_once()
            self.assertFalse(lifecycle.ended)
            self.assertEqual(sum(call.args[0]["op"] == "notify" for call in request.call_args_list), 1)

    def test_own_loop_receipt_allows_compaction_again_after_next_turn(self):
        for representation in ("event_msg", "response_item", "claude"):
            with self.subTest(representation=representation), tempfile.TemporaryDirectory() as root:
                path, lifecycle, reporter, _ = self.setup_loop(root, compact_on_end=True)
                if representation == "claude":
                    lifecycle.agent["provider"] = lifecycle.tracker.provider = "claude"
                    path.write_text(json.dumps({"type": "assistant", "message": {
                        "stop_reason": "end_turn", "content": "old result"}}) + "\n")
                    lifecycle.tracker.modified = None
                    lifecycle.tracker.check()
                pending = {"ok": True, "pending": True, "token": self.token,
                           "startup_prompt": "next slice", "agent": lifecycle.agent}
                native = {"turn_finished": False, "compact_state": None, "accepted": 0, "rejected": 0}
                peer_messages = []

                def response(payload):
                    if payload["op"] == "send_session_message":
                        peer_messages.append(payload["message"])
                        return {"ok": True, "receipt": {"state": "queued"}}
                    if payload["op"] == "runner_turn":
                        native["turn_finished"] = payload["finished"]
                    if payload["op"] == "runner_loop_ack":
                        # The real scheduler clears idle status on consumption.
                        # A fast provider may already have completed this turn.
                        native["turn_finished"] = False
                        native["compact_state"] = None
                    if payload["op"] == "runner_input" and payload["action"] == "compact":
                        if not native["turn_finished"]:
                            native["rejected"] += 1
                            return {"ok": False, "error": "run is not idle"}
                        native["accepted"] += 1
                        native["compact_state"] = "delivered"
                    if payload["op"] == "runner_input_status":
                        return {"ok": True, "state": native["compact_state"]}
                    if payload["op"] in ("runner_loop_poll", "runner_loop_claim"):
                        return pending
                    return {"ok": True, "state": "delivered"}

                waits = 0
                first_compact_offset = None

                def wait(_):
                    nonlocal waits, first_compact_offset
                    waits += 1
                    if waits == 2:
                        first_compact_offset = lifecycle.compact_offset
                        with path.open("a") as output:
                            boundary = ({"type": "system", "subtype": "compact_boundary"}
                                        if representation == "claude" else {"type": "compacted"})
                            output.write(json.dumps(boundary) + "\n")
                        self.append(path, {"type": "task_complete", "last_agent_message": "compact result"})
                    if waits == 4:
                        message = peer_messages[0] if representation == "claude" else queue.call_args.args[1]
                        if representation == "claude":
                            with path.open("a") as output:
                                output.write(json.dumps({"type": "user", "message": {"content":
                                    '<cross-session-message from-name="OmniTerm">\n' + message +
                                    '\n</cross-session-message>'}}) + "\n")
                                output.write(json.dumps({"type": "assistant", "message": {
                                    "stop_reason": "end_turn", "content": "next result"}}) + "\n")
                        elif representation == "event_msg":
                            self.append(path, {"type": "user_message", "message": message})
                        else:
                            with path.open("a") as output:
                                output.write(json.dumps({"type": "response_item", "payload": {
                                    "type": "message", "role": "user", "content": [
                                        {"type": "input_text", "text": message}]}}) + "\n")
                        self.append(path, {"type": "task_complete", "last_agent_message": "next result"})
                    return waits > 4

                with mock.patch.object(RUNNER, "request", side_effect=response) as request, \
                        mock.patch.object(RUNNER, "queue_codex_warning", return_value="queued") as queue, \
                        mock.patch.object(RUNNER.time, "monotonic", return_value=100):
                    RUNNER.watch_turn(reporter, mock.Mock(wait=wait), lifecycle)
                if representation == "claude":
                    queue.assert_not_called()
                    self.assertEqual(len(peer_messages), 1)
                else:
                    queue.assert_called_once()
                self.assertFalse(lifecycle.human_intervened)
                self.assertTrue(lifecycle.compact_requested)
                self.assertEqual(native["rejected"], 0)
                self.assertEqual(native["accepted"], 2)
                self.assertGreater(lifecycle.compact_offset, first_compact_offset)
                self.assertEqual([call.args[0]["action"] for call in request.call_args_list
                                  if call.args[0]["op"] == "runner_input"], ["compact", "compact"])

    def test_recovered_claim_observes_receipt_without_resending(self):
        with tempfile.TemporaryDirectory() as root:
            path, lifecycle, _, loop = self.setup_loop(root, finished=False)
            claimed = {"ok": True, "pending": False, "state": "claimed", "token": self.token,
                       "startup_prompt": "next slice", "agent": lifecycle.agent}
            with mock.patch.object(RUNNER, "request", return_value=claimed) as request, \
                    mock.patch.object(RUNNER, "queue_codex_warning") as queue:
                loop.poll(10)
                self.append(path, {"type": "user_message", "message": loop.message})
                loop.poll(20)
            queue.assert_not_called()
            self.assertIn(mock.call({"op": "runner_loop_ack", "run_id": "one-run", "token": self.token}),
                          request.call_args_list)


class UsageFallbackTests(unittest.TestCase):
    CLAUDE_LIMIT = {"type": "assistant", "isApiErrorMessage": True, "error": "rate_limit",
                    "apiError": "usage_limit_reached",
                    "message": {"stop_reason": "stop_sequence", "content": [
                        {"type": "text", "text": "You've hit your Fable limit · resets 1pm (Europe/Paris)."}]}}
    CODEX_LIMIT = {"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": None, "error": {
        "message": "You’ve hit your usage limit.", "codex_error_info": "usage_limit_exceeded"}}}

    def test_spent_allowance_is_a_limit_and_never_a_finished_turn(self):
        self.assertIn("Fable limit", RUNNER.usage_limit_event(self.CLAUDE_LIMIT, "claude"))
        self.assertEqual(RUNNER.usage_limit_event(self.CODEX_LIMIT, "codex"), "You’ve hit your usage limit.")
        self.assertIs(RUNNER.codex_turn_event(self.CODEX_LIMIT), False)
        transient = dict(self.CLAUDE_LIMIT, apiError=None,
                         message={"content": [{"type": "text", "text": "API Error: 429 overloaded"}]})
        self.assertIsNone(RUNNER.usage_limit_event(transient, "claude"))

    def test_only_the_latest_turn_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "t.jsonl"
            path.write_text(json.dumps(self.CLAUDE_LIMIT) + "\n")
            self.assertIn("Fable", RUNNER.last_usage_limit(path, "claude"))
            with path.open("a") as handle:
                handle.write(json.dumps({"type": "user", "message": {"content": "try again"}}) + "\n")
            self.assertIsNone(RUNNER.last_usage_limit(path, "claude"))

    def test_equivalent_model_on_the_other_harness_comes_first(self):
        choices = ["gpt-6.2-sol", "gpt-6.2-astra", "gpt-6-luna"]
        available = lambda name: True
        with mock.patch.object(RUNNER.OMNI, "codex_model_choices", return_value=choices):
            self.assertEqual(RUNNER.fallback_candidates("claude", "fable", {("claude", "fable")}, set(), available),
                             [("codex", "gpt-6.2-astra"), ("claude", "opus")])
            self.assertEqual(RUNNER.fallback_candidates("codex", "gpt-6.2-sol", set(), {"codex"}, available),
                             [("claude", "sonnet")])
            self.assertEqual(RUNNER.fallback_candidates("claude", "haiku", set(), {"claude"},
                                                        lambda name: name != "codex"), [])
        self.assertTrue(RUNNER.limit_is_model_only("claude", self.CLAUDE_LIMIT["message"]["content"][0]["text"]))
        self.assertFalse(RUNNER.limit_is_model_only("claude", "You've hit your weekly limit"))

    def test_moved_agent_keeps_the_task_but_not_harness_specific_settings(self):
        agent = {"provider": "codex", "model": "gpt-6.2-sol", "permission_mode": "dangerous",
                 "advanced_args": ["--search"], "startup_prompt": "do it"}
        moved = RUNNER.fallback_agent(agent, "claude", "sonnet", "codex ran out", "/t.jsonl")
        self.assertEqual((moved["provider"], moved["model"], moved["permission_mode"], moved["advanced_args"]),
                         ("claude", "sonnet", "bypassPermissions", []))
        self.assertTrue(moved["startup_prompt"].startswith("do it\n\n"))
        self.assertIn("/t.jsonl", moved["startup_prompt"])
        self.assertEqual(RUNNER.fallback_agent(dict(agent, permission_mode="never"), "claude", "", "x")["permission_mode"], "")


if __name__ == "__main__":
    unittest.main()
