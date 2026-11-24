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

    def installation_snapshot(self):
        snapshot = {}
        # The system Python writes its bytecode cache under HOME/Library/Caches;
        # that is the interpreter's state, not the installation's.
        caches = self.home / "Library/Caches"
        for base in (self.destination, self.home):
            for path in (base, *sorted(base.rglob("*"))):
                if path == caches or caches in path.parents:
                    continue
                if path.is_symlink():
                    snapshot[str(path)] = ("link", os.readlink(path))
                elif path.is_file():
                    snapshot[str(path)] = ("file", path.read_bytes())
                elif path.is_dir():
                    snapshot[str(path)] = ("directory",)
        return snapshot

    def assert_conflict_preserves_installation(self, app):
        before = self.installation_snapshot()
        result = self.install(app)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("Refusing", result.stderr)
        self.assertNotIn("Installed OmniTerm", result.stdout)
        self.assertEqual(self.installation_snapshot(), before)

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

    def test_upgrade_refuses_either_foreign_launcher_before_changing_anything(self):
        self.user_data()
        self.assertEqual(self.install(self.bundle("1.0.0")).returncode, 0)
        self.assertEqual(self.install(self.bundle("2.0.0")).returncode, 0)
        app = self.bundle("3.0.0")
        for name in ("omni", "omni-mcp"):
            launcher = self.bin_dir / name
            owned_target = os.readlink(launcher)
            for kind in ("file", "directory", "foreign-link", "dangling-link"):
                with self.subTest(name=name, kind=kind):
                    launcher.unlink()
                    if kind == "file":
                        launcher.write_text("unrelated command")
                    elif kind == "directory":
                        launcher.mkdir()
                        (launcher / "keep").write_text("unrelated directory")
                    else:
                        launcher.symlink_to(self.home / ("omni-calls" if kind == "foreign-link" else "missing"))
                    try:
                        self.assert_conflict_preserves_installation(app)
                    finally:
                        if launcher.is_dir() and not launcher.is_symlink():
                            shutil.rmtree(launcher)
                        else:
                            launcher.unlink()
                        launcher.symlink_to(owned_target)

    def test_install_refuses_foreign_backup_even_without_an_installed_app(self):
        app = self.bundle("2.0.0")
        foreign = self.bundle("9.0.0", identifier="com.example.other")
        self.destination.mkdir()
        backup = self.destination / "OmniTerm.previous.app"
        for installed in (False, True):
            with self.subTest(installed=installed):
                if installed:
                    shutil.copytree(app, self.destination / "OmniTerm.app", symlinks=True)
                shutil.copytree(foreign, backup, symlinks=True)
                try:
                    self.assert_conflict_preserves_installation(app)
                finally:
                    shutil.rmtree(backup)
                    installed_app = self.destination / "OmniTerm.app"
                    if installed_app.exists():
                        shutil.rmtree(installed_app)

    def test_install_refuses_unowned_app_path_types_including_dangling_links(self):
        app = self.bundle("2.0.0")
        self.destination.mkdir()
        for name in ("OmniTerm.app", "OmniTerm.previous.app"):
            for kind in ("file", "directory", "owned-bundle-link", "dangling-link"):
                with self.subTest(name=name, kind=kind):
                    target = self.destination / name
                    if kind == "file":
                        target.write_text("unrelated file")
                    elif kind == "directory":
                        target.mkdir()
                        (target / "keep").write_text("unrelated directory")
                    else:
                        target.symlink_to(app if kind == "owned-bundle-link" else self.root / "missing")
                    try:
                        self.assert_conflict_preserves_installation(app)
                    finally:
                        if target.is_dir() and not target.is_symlink():
                            shutil.rmtree(target)
                        else:
                            target.unlink()

    def test_repeated_upgrade_rotates_only_owned_backup(self):
        for version in ("1.0.0", "2.0.0", "3.0.0"):
            result = self.install(self.bundle(version))
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.installed_version(), "3.0.0")
        self.assertEqual(self.installed_version("OmniTerm.previous.app"), "2.0.0")

    def assert_upgrade_rename_failure_restores_installation(self, phase, exit_code,
                                                           fail_recovery=False,
                                                           fail_previous_recovery=False):
        agent, history = self.user_data()
        for version in ("1.0.0", "2.0.0"):
            result = self.install(self.bundle(version), "--skip-mcp")
            self.assertEqual(result.returncode, 0, result.stderr)
        app = self.bundle("3.0.0")
        before = self.installation_snapshot()
        boundary = self.root / "rename-bin"
        boundary.mkdir()
        marker = self.root / "rename-failure-used"
        rename = boundary / "mv"
        rename.write_text(f"#!{sys.executable}\n" + '''import os
import sys
from pathlib import Path

source, destination = map(Path, sys.argv[-2:])
installed = Path(os.environ["INSTALL_RENAME_DESTINATION"]) / "OmniTerm.app"
backup = installed.with_name("OmniTerm.previous.app")
phase = os.environ["INSTALL_RENAME_PHASE"]
marker = Path(os.environ["INSTALL_RENAME_MARKER"])
if (os.environ.get("INSTALL_RENAME_RECOVERY_FAILURE") == "1" and marker.exists()
        and source == backup and destination == installed):
    print("injected recovery rename failure", file=sys.stderr)
    sys.exit(26)
if (os.environ.get("INSTALL_RENAME_PREVIOUS_RECOVERY_FAILURE") == "1" and marker.exists()
        and source.name == "previous.app"
        and source.parent.name.startswith(".OmniTerm.rollback.") and destination == backup):
    print("injected previous recovery rename failure", file=sys.stderr)
    sys.exit(26)
selected = (phase == "backup" and source == installed and destination == backup
            or phase == "publish" and source.name.startswith(".OmniTerm.installing.")
            and destination == installed)
if selected and not marker.exists():
    marker.write_text("failed once")
    print("injected " + phase + " rename failure", file=sys.stderr)
    sys.exit(int(os.environ["INSTALL_RENAME_EXIT"]))
os.execv("/bin/mv", ["/bin/mv", *sys.argv[1:]])
''')
        rename.chmod(0o755)
        self.environment.update(
            PATH=str(boundary) + os.pathsep + self.environment["PATH"],
            INSTALL_RENAME_DESTINATION=str(self.destination),
            INSTALL_RENAME_PHASE=phase, INSTALL_RENAME_MARKER=str(marker),
            INSTALL_RENAME_EXIT=str(exit_code),
            INSTALL_RENAME_RECOVERY_FAILURE="1" if fail_recovery else "0",
            INSTALL_RENAME_PREVIOUS_RECOVERY_FAILURE="1" if fail_previous_recovery else "0",
        )

        failed = self.install(app, "--skip-mcp")

        self.assertEqual(failed.returncode, exit_code, failed.stderr)
        self.assertIn(f"injected {phase} rename failure", failed.stderr)
        self.assertEqual(failed.stdout, "")
        self.assertTrue(marker.exists())
        if fail_recovery or fail_previous_recovery:
            diagnostic = ("injected previous recovery rename failure" if fail_previous_recovery
                          else "injected recovery rename failure")
            self.assertIn(diagnostic, failed.stderr)
            retained = {}
            for info_path in self.destination.rglob("Info.plist"):
                version = plistlib.loads(info_path.read_bytes())["CFBundleShortVersionString"]
                retained[version] = info_path.parent.parent
            self.assertEqual(set(retained), {"1.0.0", "2.0.0"})
            for version, original in (("1.0.0", "OmniTerm.previous.app"),
                                      ("2.0.0", "OmniTerm.app")):
                original_path = self.destination / original
                for path, content in before.items():
                    if Path(path).is_relative_to(original_path) and content[0] == "file":
                        relative = Path(path).relative_to(original_path)
                        self.assertEqual((retained[version] / relative).read_bytes(), content[1])
                if version == "1.0.0" or fail_recovery:
                    self.assertIn(str(retained[version]), failed.stderr)
            if fail_previous_recovery:
                self.assertEqual(retained["2.0.0"], self.destination / "OmniTerm.app")
                self.assertFalse((self.destination / "OmniTerm.previous.app").exists())
                after = self.installation_snapshot()
                self.assertEqual(
                    {path: content for path, content in after.items()
                     if Path(path).is_relative_to(self.home)},
                    {path: content for path, content in before.items()
                     if Path(path).is_relative_to(self.home)})
                for launcher in ("omni", "omni-mcp"):
                    self.assertTrue((self.bin_dir / launcher).is_file())
            self.assertEqual((agent.read_text(), history.read_text()), ("keep", "keep"))
            self.assertEqual(list(self.destination.glob(".OmniTerm.installing*")), [])
            return
        self.assertEqual(self.installation_snapshot(), before)
        self.assertEqual(sorted(path.name for path in self.destination.iterdir()),
                         ["OmniTerm.app", "OmniTerm.previous.app"])

        recovered = self.install(app, "--skip-mcp")

        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertIn("Installed OmniTerm", recovered.stdout)
        self.assertEqual(self.installed_version(), "3.0.0")
        self.assertEqual(self.installed_version("OmniTerm.previous.app"), "2.0.0")
        self.assertEqual((agent.read_text(), history.read_text()), ("keep", "keep"))
        for launcher in ("omni", "omni-mcp"):
            self.assertEqual(os.readlink(self.bin_dir / launcher),
                             str(self.destination / f"OmniTerm.app/Contents/Resources/Helpers/{launcher}"))
        self.assertEqual(sorted(path.name for path in self.destination.iterdir()),
                         ["OmniTerm.app", "OmniTerm.previous.app"])

    def test_failed_backup_rename_restores_both_versions_and_all_user_state(self):
        self.assert_upgrade_rename_failure_restores_installation("backup", 24)

    def test_failed_publish_rename_restores_both_versions_and_all_user_state(self):
        self.assert_upgrade_rename_failure_restores_installation("publish", 25)

    def test_failed_rollback_keeps_recoverable_versions_and_reports_paths(self):
        self.assert_upgrade_rename_failure_restores_installation("publish", 25,
                                                               fail_recovery=True)

    def test_failed_previous_restore_keeps_older_copy_after_active_app_is_restored(self):
        self.assert_upgrade_rename_failure_restores_installation("publish", 25,
                                                               fail_previous_recovery=True)

    def test_fresh_install_repairs_dangling_launchers_owned_by_destination(self):
        self.bin_dir.mkdir(parents=True)
        for name in ("omni", "omni-mcp"):
            (self.bin_dir / name).symlink_to(
                self.destination / f"OmniTerm.app/Contents/Resources/Helpers/{name}")
        result = self.install(self.bundle("1.0.0"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.bin_dir / "omni").is_file())

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

    def test_failed_mcp_uninstall_preserves_installation_and_allows_retry(self):
        apps = [self.bundle(version) for version in ("1.0.0", "2.0.0")]
        boundary = self.root / "uninstall-bin"
        boundary.mkdir()
        process_lookup = boundary / "pgrep"
        process_lookup.write_text("#!/bin/sh\nexit 1\n")
        process_lookup.chmod(0o755)
        self.environment["PATH"] = str(boundary) + os.pathsep + self.environment["PATH"]
        for arguments in ((), ("--purge",)):
            with self.subTest(arguments=arguments):
                agent, history = self.user_data()
                for app in apps:
                    installed = self.install(app)
                    self.assertEqual(installed.returncode, 0, installed.stderr)
                helper = self.bin_dir / "omni"
                original = helper.read_bytes()
                helper.write_text('#!/bin/sh\necho "injected MCP failure: $*" >&2\nexit 37\n')
                before = self.installation_snapshot()

                failed = self.uninstall(*arguments)

                self.assertEqual(failed.returncode, 37, failed.stderr)
                self.assertIn("injected MCP failure: mcp uninstall", failed.stderr)
                self.assertEqual(failed.stdout, "")
                self.assertEqual(self.installation_snapshot(), before)
                helper.write_bytes(original)

                retried = self.uninstall(*arguments)

                self.assertEqual(retried.returncode, 0, retried.stderr)
                self.assertEqual(list(self.destination.iterdir()), [])
                for launcher in ("omni", "omni-mcp"):
                    self.assertFalse((self.bin_dir / launcher).is_symlink())
                if arguments:
                    self.assertFalse(agent.exists() or history.exists())
                else:
                    self.assertEqual((agent.read_text(), history.read_text()), ("keep", "keep"))

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
