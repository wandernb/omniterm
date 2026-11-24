import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


GENERATOR = Path(__file__).resolve().parents[1] / "scripts/signing-entitlements.sh"
ORDINARY = {
    "com.apple.security.cs.allow-jit": True,
    "com.apple.security.automation.apple-events": True,
}
RESTRICTED = {
    "keychain-access-groups": ["$(TeamIdentifierPrefix)com.googlecode.iterm2"],
    "com.apple.security.application-groups": ["$(TeamIdentifierPrefix)iTerm"],
}


@unittest.skipUnless(sys.platform == "darwin", "macOS plist tools")
class AdHocEntitlementsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="omniterm-entitlements-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "source entitlements.plist"
        self.output = self.root / "generated entitlements.plist"
        home = self.root / "home"
        scratch = self.root / "scratch"
        home.mkdir()
        scratch.mkdir()
        self.environment = {
            key: value for key, value in os.environ.items()
            if key != "OMNITERM_TEAM_ID"
        }
        self.environment.update(HOME=str(home), TMPDIR=str(scratch))

    def generate(self):
        return subprocess.run(
            [str(GENERATOR), str(self.source), str(self.output), "-"],
            capture_output=True, text=True, env=self.environment, timeout=10,
        )

    def test_ad_hoc_normalizes_any_subset_of_restricted_groups(self):
        keychain, application = RESTRICTED
        for keys in ((), (keychain,), (application,), (keychain, application)):
            with self.subTest(restricted=keys):
                source = {**ORDINARY, **{key: RESTRICTED[key] for key in keys}}
                original = plistlib.dumps(source)
                self.source.write_bytes(original)

                result = self.generate()

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(plistlib.loads(self.output.read_bytes()), ORDINARY)
                self.assertEqual(self.source.read_bytes(), original)

    def test_ad_hoc_rejects_invalid_input_and_unexpanded_variables(self):
        inputs = (
            b"not a plist",
            plistlib.dumps(["not an entitlement dictionary"]),
            plistlib.dumps({**ORDINARY, **RESTRICTED, "name": "$(PRODUCT_NAME)"}),
        )
        for source in inputs:
            with self.subTest(source=source):
                self.source.write_bytes(source)

                result = self.generate()

                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertTrue(result.stderr)
                self.assertEqual(self.source.read_bytes(), source)

    def test_ad_hoc_reports_failure_when_output_is_a_directory(self):
        source = plistlib.dumps({**ORDINARY, **RESTRICTED})
        self.source.write_bytes(source)
        self.output.mkdir()
        sentinel = self.output / "keep"
        sentinel.write_bytes(b"unrelated")

        result = self.generate()

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertTrue(result.stderr)
        self.assertEqual(sentinel.read_bytes(), b"unrelated")
        self.assertEqual(self.source.read_bytes(), source)


if __name__ == "__main__":
    unittest.main()
