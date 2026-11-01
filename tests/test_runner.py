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
        self.assertEqual(argv, [
            "claude", "--system-prompt", "system\nline", "--session-id", "run-id",
            "--model", "model", "--flag", "value with space", "user prompt",
        ])

    def test_codex_roles_and_workspace_are_distinct(self):
        argv = RUNNER.provider_argv(self.base("codex"))
        self.assertEqual(argv[argv.index("-C") + 1], "/tmp/a space")
        self.assertIn("developer_instructions=", argv[argv.index("-c") + 1])
        self.assertEqual(argv[-1], "user prompt")

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
        tracker = mock.Mock()
        tracker.check.side_effect = [False, True, True, True, False]
        stop = mock.Mock()
        stop.wait.side_effect = [False] * 5 + [True]
        sent = []

        def request(payload):
            sent.append(payload["finished"])
            if len(sent) == 1:
                raise OSError("scheduler restarted")

        with mock.patch.object(RUNNER, "request", side_effect=request):
            RUNNER.watch_turn("run-1", tracker, stop)
        self.assertEqual(sent, [True, True, False])


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
                            ("acquire_start_slot", None)):
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

    def test_missing_run_exits_before_starting_provider(self):
        with mock.patch.object(RUNNER.sys, "argv", ["omni-runner", "missing"]), \
                mock.patch.object(RUNNER, "request", return_value={"ok": False,
                                                                    "error": "Run not found"}), \
                mock.patch.object(RUNNER.subprocess, "Popen") as popen, \
                self.assertRaisesRegex(SystemExit, "Run not found"):
            RUNNER.main()
        popen.assert_not_called()

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
                redirect_stdout(io.StringIO()):
            RUNNER.main()

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
                        trackers.append(self.args[1])
                    else:
                        self.target()

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


if __name__ == "__main__":
    unittest.main()
