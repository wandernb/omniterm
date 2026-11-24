import importlib.util
import functools
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock


SPECIFICATION = importlib.util.spec_from_file_location(
    "omni_coverage_tests", Path(__file__).with_name("coverage_report.py"))
COVERAGE = importlib.util.module_from_spec(SPECIFICATION)
SPECIFICATION.loader.exec_module(COVERAGE)


class FakeMonitoring:
    events = types.SimpleNamespace(LINE=32)
    DISABLE = object()

    def __init__(self, occupied=None):
        self.tools = dict(occupied or {})
        self.callbacks = {}
        self.enabled = {}
        self.freed = []

    def get_tool(self, identifier):
        return self.tools.get(identifier)

    def use_tool_id(self, identifier, name):
        if identifier in self.tools:
            raise ValueError("Occupied")
        self.tools[identifier] = name

    def register_callback(self, identifier, event, callback):
        previous = self.callbacks.get(identifier)
        self.callbacks[identifier] = callback
        return previous

    def set_events(self, identifier, events):
        self.enabled[identifier] = events

    def free_tool_id(self, identifier):
        if self.enabled.get(identifier) or self.callbacks.get(identifier) is not None:
            raise AssertionError("Callbacks must be cleared before releasing a tool ID")
        self.freed.append(identifier)
        self.tools.pop(identifier)


class MonitoringOwnershipTests(unittest.TestCase):
    def test_collection_failure_cleans_its_owned_monitoring_slot(self):
        monitoring = FakeMonitoring()
        collector = COVERAGE.MonitoringCollector([], monitoring=monitoring)

        def failing_work():
            raise ValueError("Fixture failure")

        with self.assertRaisesRegex(ValueError, "Fixture failure"):
            collector.run(failing_work)
        self.assertEqual(monitoring.tools, {})
        self.assertEqual(monitoring.freed, [3])
        self.assertIsNone(collector.tool_id)

    def test_existing_owner_rejects_collection_without_changing_monitoring(self):
        monitoring = FakeMonitoring({0: "debugger"})
        collector = COVERAGE.MonitoringCollector([], monitoring=monitoring)
        work = mock.Mock()
        with self.assertRaisesRegex(COVERAGE.MonitoringUnavailable, "must run alone"):
            collector.run(work)
        work.assert_not_called()
        self.assertEqual(monitoring.tools, {0: "debugger"})
        self.assertEqual(monitoring.callbacks, {})
        self.assertEqual(monitoring.enabled, {})
        self.assertEqual(monitoring.freed, [])

    def test_owner_arriving_during_collection_retires_our_slot_without_rearming(self):
        monitoring = FakeMonitoring()
        collector = COVERAGE.MonitoringCollector([], monitoring=monitoring)
        monitoring.set_local_events = mock.Mock()

        def another_tool_attaches():
            monitoring.use_tool_id(0, "debugger")

        with self.assertRaisesRegex(COVERAGE.MonitoringUnavailable, "slot has been retired"):
            collector.run(another_tool_attaches)
        self.assertEqual(monitoring.tools[0], "debugger")
        self.assertEqual(len(monitoring.tools), 2)
        monitoring.set_local_events.assert_not_called()
        self.assertEqual(monitoring.freed, [])
        self.assertEqual(monitoring.enabled[3], 0)
        self.assertIsNone(monitoring.callbacks[3])

    def test_no_available_slot_never_replaces_an_existing_tool(self):
        occupied = {identifier: "owned-" + str(identifier) for identifier in range(6)}
        monitoring = FakeMonitoring(occupied)
        collector = COVERAGE.MonitoringCollector([], monitoring=monitoring)
        work = mock.Mock()
        with self.assertRaises(COVERAGE.MonitoringUnavailable):
            collector.run(work)
        work.assert_not_called()
        self.assertEqual(monitoring.tools, occupied)
        self.assertEqual(monitoring.freed, [])

    def test_failed_registration_releases_the_claim_without_leaving_events(self):
        monitoring = FakeMonitoring()
        original = monitoring.set_events

        def enable_fails(identifier, events):
            original(identifier, events)
            if events:
                raise ValueError("Activation failed")

        monitoring.set_events = enable_fails
        collector = COVERAGE.MonitoringCollector([], monitoring=monitoring)
        with self.assertRaisesRegex(ValueError, "Activation failed"):
            collector.run(mock.Mock())
        self.assertEqual(monitoring.tools, {})
        self.assertEqual(monitoring.freed, [3])


def exclusive_monitoring_case(function):
    @functools.wraps(function)
    def exercise(self):
        if any(sys.monitoring.get_tool(identifier) is not None for identifier in range(6)):
            child = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()),
                 self.__class__.__name__ + "." + function.__name__],
                capture_output=True, text=True)
            self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
            return
        return function(self)
    return exercise


@unittest.skipUnless(hasattr(sys, "monitoring"), "first-hit monitoring requires Python 3.12+")
class FirstHitCoverageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.helper = Path(directory.name) / "helper.py"
        self.helper.write_text(
            "def work(iterations, later):\n"
            "    total = 0\n"
            "    for number in range(iterations):\n"
            "        total += number\n"
            "    if later:\n"
            "        total += 100\n"
            "    return total\n")
        namespace = {}
        exec(compile(self.helper.read_text(), str(self.helper), "exec"), namespace)
        self.work = namespace["work"]

    @exclusive_monitoring_case
    def test_hot_loop_disables_only_seen_locations_and_keeps_the_later_branch(self):
        collector = COVERAGE.MonitoringCollector([self.helper])

        def exercise():
            self.assertEqual(self.work(1000, False), 499500)
            first_events = collector.helper_events
            self.assertNotIn((str(self.helper), 6), collector.counts)
            self.assertEqual(self.work(100000, False), 4999950000)
            self.assertEqual(collector.helper_events, first_events)
            self.assertEqual(self.work(1, True), 100)

        collector.run(exercise)
        self.assertEqual({line for filename, line in collector.counts}, {2, 3, 4, 5, 6, 7})
        self.assertNotIn((str(self.helper), 1), collector.counts)
        self.assertLessEqual(collector.helper_events, 12)

    @exclusive_monitoring_case
    def test_only_named_helpers_count(self):
        other = self.helper.with_name("other.py")
        other_namespace = {}
        exec(compile("def other():\n    return 7\n", str(other), "exec"), other_namespace)
        collector = COVERAGE.MonitoringCollector([self.helper])

        def exercise():
            other_namespace["other"]()
            self.work(2, False)
            self.work(1, True)

        collector.run(exercise)
        self.assertEqual({filename for filename, line in collector.counts}, {str(self.helper)})
        self.assertIn((str(self.helper), 6), collector.counts)

    @exclusive_monitoring_case
    def test_releasing_and_reusing_a_slot_starts_a_fresh_line_measurement(self):
        for _ in range(2):
            collector = COVERAGE.MonitoringCollector([self.helper])
            collector.run(lambda: self.work(2, True))
            self.assertEqual({line for filename, line in collector.counts}, {2, 3, 4, 5, 6, 7})

    @exclusive_monitoring_case
    def test_reused_slot_rearms_each_independently_compiled_code_object(self):
        namespace = {}
        exec(compile(self.helper.read_text(), str(self.helper), "exec"), namespace)
        duplicate_work = namespace["work"]
        first = COVERAGE.MonitoringCollector([self.helper])
        first.run(lambda: (self.work(2, True), duplicate_work(2, True)))
        second = COVERAGE.MonitoringCollector([self.helper])
        second.run(lambda: duplicate_work(2, True))
        self.assertEqual({line for filename, line in second.counts}, {2, 3, 4, 5, 6, 7})

    @exclusive_monitoring_case
    def test_symlinked_helper_keeps_the_declared_filename_for_the_denominator(self):
        alias = self.helper.with_name("alias.py")
        alias.symlink_to(self.helper)
        collector = COVERAGE.MonitoringCollector([alias])
        collector.run(lambda: self.work(2, True))
        self.assertEqual(set(collector.counts), {(str(alias), line) for line in (2, 3, 4, 5, 6, 7)})

    @exclusive_monitoring_case
    def test_nested_collection_is_rejected_without_reenabling_outer_locations(self):
        outer = COVERAGE.MonitoringCollector([self.helper])
        inner = COVERAGE.MonitoringCollector([self.helper])

        def exercise():
            self.work(2, False)
            first_events = outer.helper_events
            with self.assertRaisesRegex(COVERAGE.MonitoringUnavailable, "must run alone"):
                inner.run(lambda: self.work(2, False))
            self.work(10000, False)
            self.assertEqual(outer.helper_events, first_events)
            self.work(1, True)
            self.assertIn((str(self.helper), 6), outer.counts)
            self.assertEqual(inner.counts, {})

        outer.run(exercise)


if __name__ == "__main__":
    unittest.main()
