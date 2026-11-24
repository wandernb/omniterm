import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPORTER = ROOT / "scripts/iterm2-fork.sh"


@unittest.skipUnless(shutil.which("zsh"), "fork exporter requires zsh")
class ForkExportTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="omniterm-fork-export-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.fork = self.root / "fork"
        self.fork.mkdir()
        self.patch = self.root / "omniterm.patch"
        # Reuse an existing immutable commit as the fixture baseline. Never create
        # commits or change the real checkout's index, refs, or working files.
        objects = subprocess.check_output(
            ["git", "rev-parse", "--path-format=absolute", "--git-path", "objects"],
            cwd=ROOT, text=True,
        ).strip()
        head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
        ).strip()
        self.git("init", "--quiet")
        (self.fork / ".git/objects/info/alternates").write_text(objects + "\n")
        self.git("update-ref", "HEAD", head)

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.fork), *args],
            check=True, capture_output=True, text=True,
        )

    def add_file(self, name, content, staged=True):
        path = self.fork / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        if staged:
            self.git("add", "--", name)

    def export_environment(self, **overrides):
        return {**os.environ, "OMNITERM_FORK_DIR": str(self.fork),
                "OMNITERM_PATCH_FILE": str(self.patch), **overrides}

    def export(self, **overrides):
        return subprocess.run(
            ["zsh", str(EXPORTER), "export"], capture_output=True, text=True,
            env=self.export_environment(**overrides), timeout=15,
        )

    def controlled_git(self):
        """Pause a real diff mid-write, without changing its snapshot bytes."""
        shim_directory = self.root / "bin"
        shim_directory.mkdir(exist_ok=True)
        shim = shim_directory / "git"
        shim.write_text(f"#!{sys.executable}\n" + '''import os
import subprocess
import sys
import time
from pathlib import Path

arguments = sys.argv[1:]
if "diff" not in arguments:
    os.execv(os.environ["REAL_GIT"], [os.environ["REAL_GIT"], *arguments])
result = subprocess.run([os.environ["REAL_GIT"], *arguments], capture_output=True)
if result.returncode:
    sys.stderr.buffer.write(result.stderr)
    sys.exit(result.returncode)
split = len(result.stdout) // 2
sys.stdout.buffer.write(result.stdout[:split])
sys.stdout.buffer.flush()
Path(os.environ["DIFF_READY"]).touch()
deadline = time.monotonic() + 10
while not Path(os.environ["DIFF_RELEASE"]).exists():
    if time.monotonic() > deadline:
        sys.exit("diff barrier timed out")
    time.sleep(0.01)
sys.stdout.buffer.write(result.stdout[split:])
sys.stdout.buffer.flush()
sys.exit(int(os.environ["DIFF_EXIT"]))
''')
        shim.chmod(0o755)
        return {"PATH": str(shim_directory) + os.pathsep + os.environ["PATH"],
                "REAL_GIT": shutil.which("git"),
                "DIFF_READY": str(self.root / "ready"),
                "DIFF_RELEASE": str(self.root / "release")}

    def test_overlapping_exports_publish_only_complete_private_snapshots(self):
        for first_exit in (0, 23):
            with self.subTest(first_exit=first_exit):
                self.add_file("sources/OmniExport.m", "// Snapshot A\n" * 30)
                snapshot_a = self.git("diff", "--binary", "--full-index", "HEAD",
                                      "--", "sources").stdout
                environment = self.controlled_git()
                ready = Path(environment["DIFF_READY"])
                release = Path(environment["DIFF_RELEASE"])
                ready.unlink(missing_ok=True)
                release.unlink(missing_ok=True)
                first = subprocess.Popen(
                    ["zsh", str(EXPORTER), "export"],
                    env=self.export_environment(**environment, DIFF_EXIT=str(first_exit)),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                try:
                    deadline = time.monotonic() + 10
                    while not ready.exists() and first.poll() is None:
                        self.assertLess(time.monotonic(), deadline, "diff never reached barrier")
                        time.sleep(0.01)
                    self.assertTrue(ready.exists(), "first exporter exited before barrier")
                    self.add_file("sources/OmniExport.m", "// Snapshot B\n" * 50)
                    second = self.export()
                    self.assertEqual(second.returncode, 0, second.stderr)
                    snapshot_b = self.patch.read_text()
                    self.assertNotEqual(snapshot_a, snapshot_b)
                    release.touch()
                    _, errors = first.communicate(timeout=15)
                    if first_exit:
                        self.assertNotEqual(first.returncode, 0, errors)
                        self.assertEqual(self.patch.read_text(), snapshot_b)
                    else:
                        self.assertEqual(first.returncode, 0, errors)
                        self.assertEqual(self.patch.read_text(), snapshot_a)
                    self.assertEqual(list(self.root.glob("omniterm.patch.tmp*")), [])
                finally:
                    release.touch()
                    if first.poll() is None:
                        first.kill()
                    first.communicate(timeout=15)

    def test_failed_diff_preserves_previous_patch_and_removes_partial_output(self):
        self.add_file("sources/OmniExport.m", "// Native change\n")
        self.patch.write_text("previous reviewed patch\n")
        environment = self.controlled_git()
        Path(environment["DIFF_RELEASE"]).touch()

        result = self.export(**environment, DIFF_EXIT="23")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.patch.read_text(), "previous reviewed patch\n")
        self.assertEqual(list(self.root.glob("omniterm.patch.tmp*")), [])

    def test_empty_diff_preserves_previous_patch_and_leaves_no_temporary_file(self):
        # A tracked fixture outside the export paths produces an empty export.
        self.add_file("Unrelated.txt", "// Outside native export paths\n")
        self.patch.write_text("previous reviewed patch\n")

        result = self.export()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Refusing to write an empty patch", result.stderr)
        self.assertEqual(self.patch.read_text(), "previous reviewed patch\n")
        self.assertEqual(list(self.root.glob("omniterm.patch.tmp*")), [])

    def test_export_replays_all_omni_test_classes_without_generated_dependencies(self):
        files = {
            "ModernTests/OmniSchedulerTests.m": "// Scheduler regression\n",
            "ModernTests/OmniFocusZoneTests.swift": "// Keyboard regression\n",
            "ModernTests/OmniWindowTypeTests.m": "// Window regression\n",
        }
        for name, content in files.items():
            self.add_file(name, content)
        self.add_file("ModernTests/UpstreamTests.swift", "// Unrelated upstream edit\n")
        self.add_file("ThirdParty/generated.a", "Generated dependency output\n")

        result = self.export()
        self.assertEqual(result.returncode, 0, result.stderr)
        # Replay the actual patch into a fresh tree, rather than inspecting its text.
        clean = self.root / "clean"
        clean.mkdir()
        subprocess.run(["git", "apply", str(self.patch)], cwd=clean, check=True,
                       capture_output=True, text=True)
        for name, content in files.items():
            self.assertTrue((clean / name).is_file(), f"Export lost {name}")
            self.assertEqual((clean / name).read_text(), content)
        self.assertFalse((clean / "ModernTests/UpstreamTests.swift").exists())
        self.assertFalse((clean / "ThirdParty/generated.a").exists())

    def test_unstaged_new_test_refuses_export_and_preserves_previous_patch(self):
        self.add_file("ModernTests/OmniSchedulerTests.m", "// Scheduler\n")
        missing = "ModernTests/OmniTerminalSnapshotTests.swift"
        self.add_file(missing, "// Not yet staged\n", staged=False)
        self.patch.write_text("previous reviewed patch\n")

        result = self.export()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(missing, result.stderr)
        self.assertEqual(self.patch.read_text(), "previous reviewed patch\n")
        self.assertFalse(self.patch.with_suffix(".patch.tmp").exists())


if __name__ == "__main__":
    unittest.main()
