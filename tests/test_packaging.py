import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "scripts" / "signing-entitlements.sh"
PACKAGE = ROOT / "scripts" / "package-release.sh"
TEAM = "ABCDE12345"
SOURCE = {
    "com.apple.security.application-groups": ["$(TeamIdentifierPrefix)iTerm"],
    "keychain-access-groups": ["$(TeamIdentifierPrefix)com.googlecode.iterm2"],
    "com.apple.security.cs.allow-jit": True,
    "com.apple.security.automation.apple-events": True,
}


@unittest.skipUnless(sys.platform == "darwin", "macOS signing tools")
class SigningEntitlementsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "source.entitlements"
        self.source.write_bytes(plistlib.dumps(SOURCE))
        self.output = self.root / "output.entitlements"

    def generate(self, identity, environment=None):
        return subprocess.run([str(GENERATOR), str(self.source), str(self.output), identity],
                              capture_output=True, text=True,
                              env={**os.environ, **(environment or {})})

    def test_ad_hoc_keeps_ordinary_entitlements_and_drops_restricted_ones(self):
        self.assertEqual(self.generate("-").returncode, 0)
        self.assertEqual(plistlib.loads(self.output.read_bytes()), {
            "com.apple.security.cs.allow-jit": True,
            "com.apple.security.automation.apple-events": True,
        })

    def test_developer_id_expands_the_team_prefix_from_the_identity(self):
        result = self.generate(f"Developer ID Application: Example ({TEAM})")
        self.assertEqual(result.returncode, 0, result.stderr)
        entitlements = plistlib.loads(self.output.read_bytes())
        self.assertEqual(entitlements["keychain-access-groups"], [f"{TEAM}.com.googlecode.iterm2"])
        self.assertEqual(entitlements["com.apple.security.application-groups"], [f"{TEAM}.iTerm"])

    def test_team_id_can_be_given_for_an_identity_named_by_hash(self):
        result = self.generate("0123456789ABCDEF", {"OMNITERM_TEAM_ID": TEAM})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(TEAM, self.output.read_text())

    def test_identity_without_a_team_fails_instead_of_shipping_a_literal_variable(self):
        result = self.generate("0123456789ABCDEF")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("team ID", result.stderr)

    def test_any_other_unexpanded_variable_fails(self):
        self.source.write_bytes(plistlib.dumps({"group": "$(PRODUCT_NAME)"}))
        result = self.generate(f"Developer ID Application: Example ({TEAM})")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unexpanded", result.stderr)

    @unittest.skipUnless(shutil.which("clang") and shutil.which("codesign"), "needs clang and codesign")
    def test_macos_runs_ad_hoc_code_only_with_the_generated_entitlements(self):
        """The real failure: launchd error 163, because AMFI kills ad-hoc restricted code."""
        program = self.root / "program"
        code = self.root / "program.c"
        code.write_text("int main(void) { return 0; }\n")
        subprocess.run(["clang", str(code), "-o", str(program)], check=True)
        self.assertEqual(self.generate("-").returncode, 0)

        def run_signed_with(entitlements):
            subprocess.run(["codesign", "--force", "--sign", "-", "--entitlements",
                            str(entitlements), str(program)], check=True, capture_output=True)
            copy = self.root / "signed"  # A fresh inode: the kernel caches signatures per vnode.
            copy.unlink(missing_ok=True)
            shutil.copy2(program, copy)
            return subprocess.run([str(copy)]).returncode

        self.assertEqual(run_signed_with(self.output), 0)
        self.assertEqual(run_signed_with(self.source), -9)


class PackageScriptTests(unittest.TestCase):
    def test_nested_code_is_signed_before_the_app_executable(self):
        script = PACKAGE.read_text()
        self.assertIn('! -path "$app/Contents/MacOS/OmniTerm"', script)
        self.assertLess(script.index('find "$app/Contents" -type f -perm -111'),
                        script.index('--entitlements "$entitlements" "$app"'))

    def test_release_uses_generated_entitlements_never_the_raw_file(self):
        script = PACKAGE.read_text()
        self.assertEqual(script.count('--entitlements "$entitlements"'), 1)
        self.assertNotIn("iTerm2.entitlements\" \"$app", script)
        # The only other entitlements are the widget's own, applied to the widget.
        others = [line for line in script.splitlines()
                  if "--entitlements" in line and '"$entitlements"' not in line]
        self.assertEqual(len(others), 1)
        self.assertIn("widget/OmniInboxWidget.entitlements", others[0])
        self.assertIn('"$item"', others[0])

    def test_widget_is_signed_sandboxed_before_the_app(self):
        script = PACKAGE.read_text()
        self.assertLess(script.index("OmniInboxWidget.entitlements"),
                        script.index('--entitlements "$entitlements" "$app"'))
        entitlements = (Path(__file__).parents[1] / "widget/OmniInboxWidget.entitlements").read_text()
        self.assertIn("com.apple.security.app-sandbox", entitlements)
        self.assertIn("read-only", entitlements)
        self.assertNotIn("read-write", entitlements)


RELEASE_PATHS = ROOT / "scripts" / "release-paths.sh"


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("zsh") and shutil.which("xcrun"),
                     "macOS compilers and zsh")
class ReleasePathTests(unittest.TestCase):
    """Release binaries must not carry the build machine's home folder."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="omniterm-release-paths-")
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name).resolve() / "home"
        self.project = self.home / "Projects/OmniTerm"
        self.project.mkdir(parents=True)
        self.environment = {**os.environ, "HOME": str(self.home)}

    def run_script(self, *arguments):
        return subprocess.run([shutil.which("zsh"), str(RELEASE_PATHS), *arguments], env=self.environment,
                              capture_output=True, text=True, timeout=120)

    def settings(self):
        output = self.home / "build/release-paths.xcconfig"
        result = self.run_script("xcconfig", str(self.project), str(output))
        self.assertEqual(result.returncode, 0, result.stderr)
        return dict(line.split(" = ", 1) for line in output.read_text().splitlines() if " = " in line)

    def compile_and_run(self, compiler, source, flags, output):
        command = ["xcrun", compiler, *flags, str(source), "-o", str(output)]
        compiled = subprocess.run(command, capture_output=True, text=True, timeout=300)
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
        self.assertNotIn(str(self.home), output.read_bytes().decode("latin-1"))
        return subprocess.run([str(output)], capture_output=True, text=True, timeout=30).stdout.strip()

    def test_config_keeps_target_flags_and_selects_swift_option_by_language_mode(self):
        settings = self.settings()
        self.assertTrue(settings["OTHER_CFLAGS"].startswith("$(inherited) "))
        self.assertTrue(settings["OTHER_SWIFT_FLAGS"].startswith(
            "$(inherited) $(OMNITERM_CONCISE_FILE_$(SWIFT_VERSION:base)) "))
        self.assertEqual(settings["OMNITERM_CONCISE_FILE_5"], "-enable-upcoming-feature ConciseMagicFile")
        self.assertNotIn("OMNITERM_CONCISE_FILE_6", settings, "Swift 6 warns, and some targets fail on warnings")
        self.assertEqual(settings["MTL_ENABLE_DEBUG_INFO"], "NO")

    def test_configured_flags_remove_the_home_folder_from_compiled_file_names(self):
        settings = self.settings()
        c_flags = [flag.strip('"') for flag in settings["OTHER_CFLAGS"].split()[1:]]
        source = self.project / "sources/probe.c"
        source.parent.mkdir()
        source.write_text('#include <stdio.h>\nint main(void) { puts(__FILE__); return 0; }\n')
        self.assertEqual(self.compile_and_run("clang", source, c_flags, self.home / "c-probe"),
                         "OmniTerm/sources/probe.c")
        swift_flags = (settings["OMNITERM_CONCISE_FILE_5"].split() +
                       [flag.strip('"') for flag in settings["OTHER_SWIFT_FLAGS"].split()[2:]])
        source = self.project / "sources/probe.swift"
        source.write_text("print(#file)\n")
        self.assertEqual(self.compile_and_run("swiftc", source, ["-swift-version", "5", "-module-name", "Probe",
                                                                 *swift_flags], self.home / "swift-probe"),
                         "Probe/probe.swift")

    def test_strip_removes_the_debug_map_and_keeps_the_program_working(self):
        source = self.project / "probe.c"
        source.write_text('#include <stdio.h>\nint main(void) { puts("ok"); return 0; }\n')
        objects = self.project / "probe.o"
        compiled = subprocess.run(["xcrun", "clang", "-g", "-c", str(source), "-o", str(objects)],
                                  capture_output=True, text=True, timeout=120)
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
        app = self.home / "OmniTerm.app/Contents/MacOS"
        app.mkdir(parents=True)
        program = app / "OmniTerm"
        linked = subprocess.run(["xcrun", "clang", str(objects), "-o", str(program)],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(linked.returncode, 0, linked.stderr)
        (app.parent / "Info.plist").write_text("not code")
        self.assertIn(str(objects), program.read_bytes().decode("latin-1"), "the debug map names the object")

        result = self.run_script("strip", str(app.parents[1]))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("from 1 Mach-O files", result.stdout)
        self.assertNotIn(str(self.home), program.read_bytes().decode("latin-1"))
        self.assertEqual(result.stderr, "")
        self.assertEqual(subprocess.run([str(program)], capture_output=True, text=True).stdout, "ok\n")
        self.assertEqual((app.parent / "Info.plist").read_text(), "not code")

    def test_strip_reports_files_that_still_name_the_home_folder(self):
        app = self.home / "OmniTerm.app/Contents/Resources"
        app.mkdir(parents=True)
        (app / "notes.txt").write_text("built in " + str(self.project))
        result = self.run_script("strip", str(app.parents[1]))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("notes.txt", result.stderr)

    def test_release_script_builds_with_the_config_and_strips_before_signing(self):
        script = PACKAGE.read_text()
        self.assertEqual(script.count('XCODE_XCCONFIG_FILE="$path_config"'), 2)
        self.assertLess(script.index('release-paths.sh" xcconfig'), script.index("build-app.sh"))
        self.assertLess(script.index("build-app.sh"), script.index('release-paths.sh" strip "$app"'))
        self.assertLess(script.index('release-paths.sh" strip "$app"'), script.index("codesign"))


@unittest.skipUnless(shutil.which("zsh"), "native build entry point uses zsh")
class BuildVerificationModeTests(unittest.TestCase):
    def test_failed_verification_cleans_only_its_products_and_preserves_existing_install(self):
        # Xcode can register even a failed build product. Verification cleans
        # its own temporary associations; canonical app/launcher files stay.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            scripts = project / "scripts"
            tools = project / "Vendor/iTerm2/tools"
            scripts.mkdir(parents=True)
            tools.mkdir(parents=True)
            entry = scripts / "build-app.sh"
            shutil.copy2(ROOT / "scripts/build-app.sh", entry)
            export = scripts / "iterm2-fork.sh"
            export.write_text("#!/bin/sh\nexit 0\n")
            export.chmod(0o755)
            build = tools / "build.sh"
            build.write_text(
                '#!/bin/sh\n'
                'mkdir -p "$BUILD_DIR/$1/OmniTerm.app"\n'
                'printf "fresh product\\n" > "$BUILD_DIR/$1/OmniTerm.app/new"\n'
                'printf "%s" "$BUILD_DIR" > "$OMNITERM_TEST_BUILD_MARKER"\n'
                'exit 23\n')
            build.chmod(0o755)

            installed = project / "dist/OmniTerm.app"
            installed.mkdir(parents=True)
            (installed / "live").write_text("running product")
            foreign = root / "existing-products/Development/OmniTerm.app"
            foreign.mkdir(parents=True)
            (foreign / "keep").write_text("previous unrelated product")
            home = root / "home"
            launchers = home / ".local/bin"
            launchers.mkdir(parents=True)
            launcher = launchers / "omni"
            launcher.symlink_to(installed / "live")
            staging = root / "staging"
            staging.mkdir()
            (staging / "keep").write_text("another session's staging file")
            marker = root / "build-root.txt"
            environment = {**os.environ, "HOME": str(home), "OMNITERM_VERIFY_ONLY": "1",
                           "OMNITERM_CONFIGURATION": "Development",
                           "OMNITERM_BUILD_DIR": str(root / "existing-products"),
                           "OMNITERM_VERIFY_STAGING_ROOT": str(staging),
                           "OMNITERM_TEST_BUILD_MARKER": str(marker)}
            result = subprocess.run([shutil.which("zsh"), str(entry)], env=environment,
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 23, result.stdout + result.stderr)
            fresh_products = Path(marker.read_text())
            self.assertEqual(fresh_products.parent.parent, staging)
            self.assertFalse(fresh_products.parent.exists())
            self.assertEqual((installed / "live").read_text(), "running product")
            self.assertEqual((foreign / "keep").read_text(), "previous unrelated product")
            self.assertEqual(launcher.readlink(), installed / "live")
            self.assertEqual(sorted(path.name for path in staging.iterdir()), ["keep"])


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("zsh"), "release script uses macOS and zsh")
class BinaryReleasePreservationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="omniterm-binary-publication-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.project = self.root / "fixture project"
        scripts = self.project / "scripts"
        scripts.mkdir(parents=True)
        self.entry = scripts / "package-release.sh"
        shutil.copy2(PACKAGE, self.entry)
        for name, contents in {
            "scripts/build-app.sh": "#!/bin/sh\nexit 0\n",
            "scripts/release-paths.sh": "#!/bin/sh\nexit 0\n",
            "scripts/signing-entitlements.sh": '#!/bin/sh\ncp "$1" "$2"\n',
            "scripts/install.sh": "#!/bin/sh\nexit 0\n",
            "LICENSE": "fixture license", "LICENSES/GPL-3.0.txt": "fixture GPL",
            "THIRD_PARTY_NOTICES.md": "fixture notices", "README.md": "fixture readme",
            "CHANGELOG.md": "fixture changes", "Vendor/iTerm2/LICENSE": "fixture upstream",
            "Vendor/iTerm2/COPYING": "fixture copying", "Vendor/iTerm2/iTerm2.entitlements": "fixture",
        }.items():
            target = self.project / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(contents)
            if name.endswith(".sh"):
                target.chmod(0o755)
        for name in ("Vendor/iTerm2/ThirdParty", "Vendor/iTerm2/submodules",
                     "dist/OmniTerm.app/Contents/PlugIns", "home", "scratch", "bin"):
            (self.project / name).mkdir(parents=True, exist_ok=True)
        (self.project / "dist/OmniTerm.app/Contents/Info.plist").write_bytes(plistlib.dumps({
            "CFBundleShortVersionString": "0.0.0", "CFBundleVersion": "0.0.0",
            "NSHumanReadableCopyright": "fixture",
        }))
        boundary = self.project / "bin"
        stub = boundary / "tool"
        stub.write_text(f"#!{sys.executable}\n" + r'''
import hashlib
import os
import shutil
import sys
from pathlib import Path

tool = Path(sys.argv[0]).name
arguments = sys.argv[1:]
failure = os.environ.get("FIXTURE_FAILURE", "")
destination = Path(arguments[-1]) if arguments else None
stage = tool
if tool == "codesign":
    stage = ("dmg_verify" if "--verify" in arguments else "dmg_sign") if destination.suffix == ".dmg" else "app_sign"
elif tool == "xcrun":
    stage = "notary" if arguments[0] == "notarytool" else "staple"
elif tool == "ditto":
    stage = "zip" if "-c" in arguments else "copy"
elif tool == "shasum":
    stage = "checksum"
if failure == stage:
    print("injected " + stage + " failure", file=sys.stderr)
    sys.exit(37)
if tool == "hdiutil":
    source = Path(arguments[arguments.index("-srcfolder") + 1])
    assert source not in destination.parents, "candidate is inside the disk-image source"
    assert source.stat().st_dev == destination.parent.stat().st_dev
    destination.write_bytes(b"new DMG")
elif tool == "ditto":
    if "-c" in arguments:
        destination.write_bytes(b"new ZIP")
    else:
        shutil.copytree(arguments[-2], destination)
elif tool == "shasum":
    if failure != "empty_checksum":
        for name in arguments[2:]:
            print(hashlib.sha256(Path(name).read_bytes()).hexdigest() + "  " + name)
elif tool == "mv":
    source = Path(arguments[-2])
    final = Path(os.environ["FIXTURE_RELEASE"])
    marker = final.parent / "rename-failed"
    if (failure in ("publish", "restore") and destination == final / "OmniTerm-1.2.3.zip"
            and source.read_bytes() == b"new ZIP" and not marker.exists()):
        marker.touch()
        print("injected publication failure", file=sys.stderr)
        sys.exit(41)
    if (failure == "restore" and marker.exists() and destination == final / "OmniTerm-1.2.3.dmg"
            and source.read_bytes() == b"old opaque DMG\x00"):
        print("injected restoration failure", file=sys.stderr)
        sys.exit(42)
    os.replace(source, destination)
''')
        stub.chmod(0o755)
        for name in ("codesign", "hdiutil", "ditto", "xcrun", "shasum", "mv"):
            (boundary / name).symlink_to(stub)
        self.release = self.project / "dist/release"
        self.release.mkdir()
        self.previous = {"OmniTerm-1.2.3.dmg": b"old opaque DMG\x00",
                         "OmniTerm-1.2.3.zip": b"old opaque ZIP\xff",
                         "SHA256SUMS": b"old opaque checksum file\n",
                         "foreign.marker": b"another session's release file"}
        for name, content in self.previous.items():
            (self.release / name).write_bytes(content)
        self.environment = {"PATH": str(boundary) + ":/usr/bin:/bin", "HOME": str(self.project / "home"),
                            "TMPDIR": str(self.project / "scratch"), "FIXTURE_RELEASE": str(self.release),
                            "OMNITERM_NOTARY_PROFILE": "fixture-notary"}

    def package(self, failure=""):
        return subprocess.run([shutil.which("zsh"), str(self.entry), "1.2.3"],
                              env={**self.environment, "FIXTURE_FAILURE": failure},
                              capture_output=True, text=True, timeout=15)

    def release_bytes(self):
        return {item.name: item.read_bytes() for item in self.release.iterdir() if item.is_file()}

    def assert_clean_scratch(self):
        self.assertEqual(list((self.project / "scratch").iterdir()), [])
        self.assertFalse(any(item.is_dir() for item in self.release.iterdir()))

    def test_generation_failures_preserve_the_entire_previous_release(self):
        for failure in ("hdiutil", "zip", "dmg_sign", "dmg_verify", "notary", "staple", "checksum",
                        "empty_checksum"):
            with self.subTest(failure=failure):
                for name, content in self.previous.items():
                    (self.release / name).write_bytes(content)
                result = self.package(failure)
                self.assertEqual(result.returncode, 1 if failure == "empty_checksum" else 37, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(self.release_bytes(), self.previous)
                self.assert_clean_scratch()

    def test_success_publishes_the_complete_candidate_and_final_paths(self):
        import hashlib

        result = self.package()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [str(self.release / "OmniTerm-1.2.3.dmg"),
                                                     str(self.release / "OmniTerm-1.2.3.zip")])
        expected = {"OmniTerm-1.2.3.dmg": b"new DMG", "OmniTerm-1.2.3.zip": b"new ZIP"}
        checksums = "".join(hashlib.sha256(content).hexdigest() + "  " + name + "\n"
                            for name, content in expected.items()).encode()
        self.assertEqual(self.release_bytes(), {**expected, "SHA256SUMS": checksums,
                                               "foreign.marker": self.previous["foreign.marker"]})
        self.assert_clean_scratch()

    def test_later_publication_failure_restores_previous_files_and_absence_then_allows_retry(self):
        for missing in (None, "OmniTerm-1.2.3.dmg"):
            with self.subTest(missing=missing):
                for name, content in self.previous.items():
                    (self.release / name).write_bytes(content)
                if missing:
                    (self.release / missing).unlink()
                marker = self.release.parent / "rename-failed"
                marker.unlink(missing_ok=True)
                expected = self.release_bytes()
                failed = self.package("publish")
                self.assertEqual(failed.returncode, 41, failed.stderr)
                self.assertEqual(failed.stdout, "")
                self.assertEqual(self.release_bytes(), expected)
                self.assert_clean_scratch()
                self.assertEqual(self.package().returncode, 0)
                self.assert_clean_scratch()

    def test_failed_restoration_keeps_old_artifacts_and_prints_recovery_location(self):
        result = self.package("restore")
        self.assertEqual(result.returncode, 41, result.stderr)
        self.assertEqual(result.stdout, "")
        recoverable = [item for item in self.release.rglob("*")
                       if item.is_file() and item.read_bytes() == self.previous["OmniTerm-1.2.3.dmg"]]
        self.assertTrue(recoverable, result.stderr)
        complete_backups = [item.parent for item in recoverable
                            if all((item.parent / name).is_file()
                                   and (item.parent / name).read_bytes() == content
                                   for name, content in self.previous.items() if name != "foreign.marker")]
        self.assertTrue(complete_backups, result.stderr)
        self.assertTrue(any(str(directory) in result.stderr for directory in complete_backups))
        self.assertEqual((self.release / "foreign.marker").read_bytes(), self.previous["foreign.marker"])
        self.assertEqual(list((self.project / "scratch").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
