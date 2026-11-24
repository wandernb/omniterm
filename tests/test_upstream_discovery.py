import re
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/iterm2-update.yml"
CURRENT = "a" * 40
NIGHTLY = "v3.7.20261107-nightly"
SENTINEL = "existing=keep\n"
CURRENT_CALL = "git -C Vendor/iTerm2 rev-parse HEAD"
LATEST_CALL = "scripts/iterm2-fork.sh latest"


def discovery_command():
    workflow = WORKFLOW.read_text()
    step = re.search(
        r"(?m)^      - name: Find the newest upstream nightly\n"
        r"        id: upstream\n        run: \|\n",
        workflow,
    )
    if step is None:
        raise ValueError("Upstream discovery workflow step is missing or unsupported")
    lines = []
    for line in workflow[step.end():].splitlines():
        if line.strip() and not line.startswith("          "):
            break
        lines.append(line)
    command = textwrap.dedent("\n".join(lines)).strip()
    if not command:
        raise ValueError("Upstream discovery workflow command is empty")
    return command


@unittest.skipUnless(Path("/bin/bash").exists(), "workflow requires Bash")
class UpstreamDiscoveryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="omniterm-upstream-discovery-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "project with spaces"
        self.root.mkdir()
        self.output = self.root / "workflow output"
        self.calls = self.root / "command calls"
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        scripts = self.root / "scripts"
        scripts.mkdir()
        for name in ("home", "scratch"):
            (self.root / name).mkdir()
        git = self.bin_dir / "git"
        git.write_text("""#!/bin/sh
printf 'git %s\\n' "$*" >> "$CALLS"
[ "$*" = '-C Vendor/iTerm2 rev-parse HEAD' ] || exit 97
if [ "$CURRENT_STATUS" -ne 0 ]; then
    printf 'injected current failure\\n' >&2
    exit "$CURRENT_STATUS"
fi
printf '%s\\n' "$CURRENT_VALUE"
""")
        latest = scripts / "iterm2-fork.sh"
        latest.write_text("""#!/bin/sh
printf 'scripts/iterm2-fork.sh %s\\n' "$*" >> "$CALLS"
[ "$*" = latest ] || exit 98
if [ "$LATEST_STATUS" -ne 0 ]; then
    printf 'injected latest failure\\n' >&2
    exit "$LATEST_STATUS"
fi
printf '%s\\n' "$LATEST_VALUE"
""")
        git.chmod(0o755)
        latest.chmod(0o755)

    def discover(self, current_status=0, latest_status=0, current=CURRENT, nightly=NIGHTLY):
        self.output.write_text(SENTINEL)
        self.calls.write_text("")
        environment = {
            "PATH": str(self.bin_dir),
            "HOME": str(self.root / "home"),
            "TMPDIR": str(self.root / "scratch"),
            "GITHUB_OUTPUT": str(self.output),
            "CALLS": str(self.calls),
            "CURRENT_STATUS": str(current_status),
            "LATEST_STATUS": str(latest_status),
            "CURRENT_VALUE": current,
            "LATEST_VALUE": nightly,
        }
        return subprocess.run(
            ["/bin/bash", "--noprofile", "--norc", "-e", "-o", "pipefail",
             "-c", discovery_command()],
            cwd=self.root, env=environment, capture_output=True, text=True, timeout=10,
        )

    def test_successful_discovery_publishes_both_values(self):
        result = self.discover()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.output.read_text(),
                         SENTINEL + f"current={CURRENT}\ntag={NIGHTLY}\n")
        self.assertEqual(self.calls.read_text().splitlines(), [CURRENT_CALL, LATEST_CALL])

    def test_failed_or_empty_discovery_stops_without_publishing_metadata(self):
        cases = (
            (41, 0, CURRENT, NIGHTLY, 41, [CURRENT_CALL], "current failure"),
            (0, 42, CURRENT, NIGHTLY, 42, [CURRENT_CALL, LATEST_CALL], "latest failure"),
            (41, 42, CURRENT, NIGHTLY, 41, [CURRENT_CALL], "current failure"),
            (0, 0, CURRENT, "", 1, [CURRENT_CALL, LATEST_CALL], "nightly"),
            (0, 0, "", NIGHTLY, 1, [CURRENT_CALL], "revision"),
        )
        for current_status, latest_status, current, nightly, status, calls, diagnostic in cases:
            with self.subTest(current_status=current_status, latest_status=latest_status,
                              current=current, nightly=nightly):
                result = self.discover(current_status, latest_status, current, nightly)

                self.assertEqual(result.returncode, status, result.stderr)
                self.assertIn(diagnostic, result.stderr.lower())
                self.assertEqual(result.stdout, "")
                self.assertEqual(self.output.read_text(), SENTINEL)
                self.assertEqual(self.calls.read_text().splitlines(), calls)


if __name__ == "__main__":
    unittest.main()
