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
        self.assertEqual(script.count("--entitlements"), 1)
        self.assertNotIn('--entitlements "$project_dir', script)


if __name__ == "__main__":
    unittest.main()
