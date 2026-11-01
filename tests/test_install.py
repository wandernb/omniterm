import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "scripts" / "install.sh"
UNINSTALL = ROOT / "scripts" / "uninstall.sh"
FAKE_OMNI = """#!/bin/sh
echo "$@" >> "$HOME/omni-calls"
"""


@unittest.skipUnless(sys.platform == "darwin", "macOS bundle tools")
class InstallLifecycleTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.destination = self.root / "Applications"
        self.bin_dir = self.home / ".local/bin"
        self.environment = {**os.environ, "HOME": str(self.home)}

    def bundle(self, version, identifier="com.omniterm.app", name="OmniTerm.app"):
        app = self.root / "source" / version / name
        helpers = app / "Contents/Resources/Helpers"
        (app / "Contents/MacOS").mkdir(parents=True)
        helpers.mkdir(parents=True)
        (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": identifier, "CFBundleExecutable": "OmniTerm",
            "CFBundleShortVersionString": version, "CFBundlePackageType": "APPL",
        }))
        shutil.copy("/usr/bin/true", app / "Contents/MacOS/OmniTerm")
        for helper in ("omni", "omni-mcp"):
            (helpers / helper).write_text(FAKE_OMNI)
            (helpers / helper).chmod(0o755)
        subprocess.run(["codesign", "--force", "--sign", "-", str(app)], check=True, capture_output=True)
        return app

    def install(self, app, *arguments):
        return subprocess.run(
            [str(INSTALL), "--app", str(app), "--destination", str(self.destination),
             "--bin-dir", str(self.bin_dir), "--skip-launch", *arguments],
            capture_output=True, text=True, env=self.environment)

    def uninstall(self, *arguments):
        return subprocess.run(
            [str(UNINSTALL), "--destination", str(self.destination), "--bin-dir", str(self.bin_dir), *arguments],
            capture_output=True, text=True, env=self.environment)

    def installed_version(self, name="OmniTerm.app"):
        info = plistlib.loads((self.destination / name / "Contents/Info.plist").read_bytes())
        return info["CFBundleShortVersionString"]

    def user_data(self):
        agent = self.home / ".omni/agents/keep.json"
        history = self.home / "Library/Application Support/OmniTerm/scheduler.sqlite"
        for path in (agent, history):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("keep")
        return agent, history

    def test_fresh_install_creates_app_launchers_and_mcp_registration(self):
        result = self.install(self.bundle("1.0.0"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.installed_version(), "1.0.0")
        for launcher in ("omni", "omni-mcp"):
            self.assertEqual(os.readlink(self.bin_dir / launcher),
                             str(self.destination / f"OmniTerm.app/Contents/Resources/Helpers/{launcher}"))
        self.assertIn("mcp install", (self.home / "omni-calls").read_text())

    def test_install_is_idempotent(self):
        app = self.bundle("1.0.0")
        self.assertEqual(self.install(app).returncode, 0)
        result = self.install(app)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.installed_version(), "1.0.0")
        self.assertEqual(list(self.destination.glob(".OmniTerm.installing*")), [])

    def test_upgrade_keeps_the_previous_version_and_user_data(self):
        agent, history = self.user_data()
        self.assertEqual(self.install(self.bundle("1.0.0")).returncode, 0)
        result = self.install(self.bundle("2.0.0"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.installed_version(), "2.0.0")
        self.assertEqual(self.installed_version("OmniTerm.previous.app"), "1.0.0")
        self.assertEqual((agent.read_text(), history.read_text()), ("keep", "keep"))

    def test_install_refuses_to_replace_an_unrelated_app(self):
        self.assertEqual(self.install(self.bundle("1.0.0")).returncode, 0)
        unrelated = self.bundle("9.0.0", identifier="com.example.other")
        shutil.rmtree(self.destination / "OmniTerm.app")
        shutil.copytree(unrelated, self.destination / "OmniTerm.app", symlinks=True)
        result = self.install(self.bundle("2.0.0"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("non-OmniTerm", result.stderr)
        self.assertEqual(self.installed_version(), "9.0.0")

    def test_install_rejects_a_bundle_whose_signature_is_broken(self):
        app = self.bundle("1.0.0")
        (app / "Contents/Resources/Helpers/omni").write_text("#!/bin/sh\necho tampered\n")
        self.assertNotEqual(self.install(app).returncode, 0)
        self.assertFalse((self.destination / "OmniTerm.app").exists())

    def test_uninstall_removes_both_versions_and_keeps_user_data(self):
        agent, history = self.user_data()
        self.install(self.bundle("1.0.0"))
        self.install(self.bundle("2.0.0"))
        result = self.uninstall()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(path.name for path in self.destination.iterdir()), [])
        self.assertFalse((self.bin_dir / "omni").is_symlink())
        self.assertIn("Preserved", result.stdout)
        self.assertTrue(agent.exists() and history.exists())

    def test_uninstall_deregisters_mcp_before_removing_the_launcher(self):
        self.install(self.bundle("1.0.0"))
        self.uninstall()
        self.assertIn("mcp uninstall", (self.home / "omni-calls").read_text())

    def test_purge_also_removes_user_data(self):
        agent, history = self.user_data()
        self.install(self.bundle("1.0.0"))
        result = self.uninstall("--purge")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(agent.exists() or history.exists())

    def test_uninstall_leaves_unrelated_apps_and_launchers_alone(self):
        self.install(self.bundle("1.0.0"))
        unrelated = self.bundle("9.0.0", identifier="com.example.other")
        shutil.rmtree(self.destination / "OmniTerm.app")
        shutil.copytree(unrelated, self.destination / "OmniTerm.app", symlinks=True)
        (self.bin_dir / "omni-mcp").unlink()
        (self.bin_dir / "omni-mcp").write_text("mine")
        result = self.uninstall()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.destination / "OmniTerm.app").exists())
        self.assertEqual((self.bin_dir / "omni-mcp").read_text(), "mine")

    def test_uninstall_on_a_clean_machine_succeeds(self):
        self.assertEqual(self.uninstall().returncode, 0)

    def test_uninstall_rejects_unknown_options(self):
        self.assertEqual(self.uninstall("--wat").returncode, 2)


if __name__ == "__main__":
    unittest.main()
