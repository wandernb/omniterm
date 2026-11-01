import subprocess
import tempfile
import unittest
from pathlib import Path

CHECK_BUNDLE = Path(__file__).resolve().parents[1] / "scripts" / "check-bundle.sh"


class CheckBundleTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.app = Path(directory.name) / "OmniTerm.app"
        self.resources = self.app / "Contents" / "Resources"
        (self.resources / "Base.lproj").mkdir(parents=True)
        (self.resources / "Base.lproj" / "MainMenu.nib").write_bytes(b"current")

    def check(self):
        return subprocess.run(
            [str(CHECK_BUNDLE), str(self.app)],
            capture_output=True,
            text=True,
        )

    def test_accepts_a_bundle_with_only_current_nibs(self):
        (self.resources / "OmniTerm.icns").write_bytes(b"icon")

        self.assertEqual(self.check().returncode, 0)

    def test_rejects_a_root_nib_that_shadows_the_current_one(self):
        (self.resources / "MainMenu.nib").write_bytes(b"from an older build")

        result = self.check()

        self.assertEqual(result.returncode, 1)
        self.assertIn("MainMenu.nib", result.stderr)

    def test_rejects_a_bundle_without_base_resources(self):
        (self.resources / "Base.lproj" / "MainMenu.nib").unlink()
        (self.resources / "Base.lproj").rmdir()

        self.assertEqual(self.check().returncode, 1)


if __name__ == "__main__":
    unittest.main()
