import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/native-test-gate.py"
SPEC = importlib.util.spec_from_file_location("native_test_gate", SCRIPT)
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)


def suite_result(name, count=2, status="passed", failures=0):
    return (f"Test Suite '{name}' {status} at 2026-11-06 18:00:00.\n"
            f"\t Executed {count} tests, with {failures} failures (0 unexpected) in 0.1 seconds\n")


class NativeGateTests(unittest.TestCase):
    def test_selects_every_class_in_exported_swift_and_objective_c_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "OmniFocusTests.swift").write_text(
                "final class OmniFocusTests: XCTestCase {}\n"
                "final class OmniInboxTests: XCTestCase {}\n"
                "// class OmniCommentTests: XCTestCase {}\n")
            (root / "OmniWindowTests.m").write_text(
                "@interface OmniWindowTests : XCTestCase\n@end\n"
                "@interface OmniWindowHelper : NSObject\n@end\n")
            (root / "UpstreamTests.swift").write_text("class UpstreamTests: XCTestCase {}")
            selected = subprocess.run(
                [sys.executable, "-B", str(SCRIPT), "select", directory],
                text=True, capture_output=True,
            )
            self.assertEqual(selected.returncode, 0, selected.stderr)
            self.assertEqual(selected.stdout.splitlines(), [
                "ModernTests/OmniFocusTests", "ModernTests/OmniInboxTests",
                "ModernTests/OmniWindowTests",
            ])

    def test_empty_or_unrecognized_export_fails_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                GATE.discover_suites(Path(directory))
            (Path(directory) / "OmniMysteryTests.swift").write_text("// No XCTest class\n")
            with self.assertRaises(ValueError):
                GATE.discover_suites(Path(directory))

    def test_requires_nonempty_passing_results_for_every_selected_suite(self):
        suites = ["OmniSchedulerTests", "OmniInboxTests"]
        scheduler = suite_result(suites[0])
        self.assertEqual(GATE.verify_results(suites, scheduler + suite_result(suites[1])), 4)
        for incomplete in (
            "", scheduler, scheduler + suite_result(suites[1], count=0),
            scheduler + suite_result(suites[1], status="failed", failures=1),
            scheduler + suite_result(suites[1], failures=1),
            scheduler + suite_result(suites[1], status="failed", failures=1) + suite_result(suites[1]),
            scheduler + suite_result(suites[1]) + f"Test Suite '{suites[1]}' failed at end.\n",
        ):
            with self.subTest(log=incomplete), self.assertRaises(ValueError):
                GATE.verify_results(suites, incomplete)

    def test_verify_cli_rejects_missing_suite_instead_of_trusting_overall_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selectors = root / "selectors.txt"
            log = root / "results.log"
            selectors.write_text("ModernTests/OmniSchedulerTests\nModernTests/OmniInboxTests\n")
            log.write_text(suite_result("OmniSchedulerTests") + suite_result("Selected tests", count=2))
            result = subprocess.run(
                [sys.executable, "-B", str(SCRIPT), "verify", str(selectors), str(log)],
                text=True, capture_output=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("OmniInboxTests", result.stderr)
            log.write_text(suite_result("OmniSchedulerTests") + suite_result("OmniInboxTests"))
            result = subprocess.run(
                [sys.executable, "-B", str(SCRIPT), "verify", str(selectors), str(log)],
                text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
