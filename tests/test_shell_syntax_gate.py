import re
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/tests.yml"


def syntax_check_command():
    workflow = WORKFLOW.read_text()
    step = re.search(
        r"(?m)^      - name: Check shell script syntax\n        run: ([^\n]+)\n?",
        workflow,
    )
    if step is None:
        raise ValueError("Shell syntax workflow step is missing or unsupported")
    command = step.group(1).strip()
    if command == "|":
        lines = []
        for line in workflow[step.end():].splitlines():
            if line.strip() and not line.startswith("          "):
                break
            lines.append(line)
        command = textwrap.dedent("\n".join(lines)).strip()
    elif command.startswith(("'", '"', ">", "|")):
        raise ValueError("Unsupported shell syntax workflow command format")
    if not command:
        raise ValueError("Shell syntax workflow command is empty")
    return command


@unittest.skipUnless(Path("/bin/zsh").exists(), "syntax gate requires zsh")
class ShellSyntaxGateTests(unittest.TestCase):
    def test_workflow_checks_every_script_without_executing_them(self):
        command = syntax_check_command()
        names = ("00-first.sh", "01 middle.sh", "02-last.sh")
        for invalid in (None, *names):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory(
                    prefix="omniterm-shell-syntax-") as directory:
                root = Path(directory)
                scripts = root / "scripts"
                scripts.mkdir()
                for name in names:
                    content = "#!/bin/zsh\nprint executed > syntax-script-executed\n"
                    if name == invalid:
                        content += "if then\n"
                    (scripts / name).write_text(content)
                result = subprocess.run(
                    ["/bin/bash", "--noprofile", "--norc", "-e", "-o", "pipefail",
                     "-c", command],
                    cwd=root, capture_output=True, text=True, timeout=10,
                )
                if invalid is None:
                    self.assertEqual(result.returncode, 0, result.stderr)
                else:
                    self.assertNotEqual(result.returncode, 0, result.stderr)
                    self.assertIn(invalid, result.stderr)
                self.assertFalse((root / "syntax-script-executed").exists())


if __name__ == "__main__":
    unittest.main()
