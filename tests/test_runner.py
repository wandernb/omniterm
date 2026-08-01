import importlib.machinery
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock
from pathlib import Path


RUNNER = importlib.machinery.SourceFileLoader(
    "omni_runner", str(Path(__file__).parents[1] / "helpers/omni-runner")
).load_module()


class ProviderArgumentTests(unittest.TestCase):
    def test_managed_agent_environment_forces_color(self):
        environment = RUNNER.provider_environment({"NO_COLOR": "1", "TERM": "xterm-256color"})
        self.assertNotIn("NO_COLOR", environment)
        self.assertEqual(environment["FORCE_COLOR"], "1")
        self.assertEqual(environment["CLICOLOR"], "1")
        self.assertEqual(environment["COLORTERM"], "truecolor")

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


class RunnerLifecycleTests(unittest.TestCase):
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

    def test_completed_run_reports_lifecycle_and_enters_static_mode(self):
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
                mock.patch.object(RUNNER.time, "sleep", side_effect=KeyboardInterrupt), \
                redirect_stdout(io.StringIO()) as output:
            RUNNER.main()

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
                mock.patch.object(RUNNER.time, "sleep", side_effect=KeyboardInterrupt), \
                redirect_stdout(io.StringIO()) as output:
            RUNNER.main()

        self.assertIn({"op": "runner_finished", "run_id": "run-2", "state": "failed",
                       "reason": "exit 7"}, requests)
        self.assertIn("failed: exit 7 · 3s", output.getvalue())

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

            class ImmediateThread:
                def __init__(self, target, daemon):
                    self.target = target
                    self.daemon = daemon

                def start(self):
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
                    mock.patch.object(RUNNER.threading, "Thread", ImmediateThread), \
                    mock.patch.object(RUNNER.time, "monotonic", side_effect=[30, 31]), \
                    mock.patch.object(RUNNER.time, "sleep", side_effect=KeyboardInterrupt), \
                    redirect_stdout(io.StringIO()):
                RUNNER.main()

        self.assertIn({"op": "runner_session", "run_id": "run-3",
                       "session_id": "provider-session"}, requests)


if __name__ == "__main__":
    unittest.main()
