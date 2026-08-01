import importlib.machinery
import unittest
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

    def test_terminal_resize_is_forwarded_to_provider_group(self):
        with mock.patch.object(RUNNER.os, "killpg") as killpg:
            RUNNER.forward_terminal_resize(1234)
        killpg.assert_called_once_with(1234, RUNNER.signal.SIGWINCH)

    def test_terminal_resize_tolerates_a_finished_provider(self):
        with mock.patch.object(RUNNER.os, "killpg", side_effect=ProcessLookupError):
            RUNNER.forward_terminal_resize(1234)

    def base(self, provider):
        return {"provider": provider, "workspace": "/tmp/a space", "system_prompt": "system\nline",
                "startup_prompt": "user prompt", "model": "model", "permission_mode": "",
                "advanced_args": ["--flag", "value with space"]}

    def test_claude_roles_are_distinct(self):
        argv = RUNNER.provider_argv(self.base("claude"))
        self.assertEqual(argv[argv.index("--system-prompt") + 1], "system\nline")
        self.assertEqual(argv[-1], "user prompt")

    def test_codex_roles_and_workspace_are_distinct(self):
        argv = RUNNER.provider_argv(self.base("codex"))
        self.assertEqual(argv[argv.index("-C") + 1], "/tmp/a space")
        self.assertIn("developer_instructions=", argv[argv.index("-c") + 1])
        self.assertEqual(argv[-1], "user prompt")

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


if __name__ == "__main__":
    unittest.main()
