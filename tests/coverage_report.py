#!/usr/bin/env python3
"""Measure helper line coverage with first-hit stdlib monitoring when available."""

import argparse
import io
from pathlib import Path
import sys
import threading
import trace
import unittest


ROOT = Path(__file__).resolve().parents[1]
HELPERS = tuple(ROOT / "helpers" / name for name in (
    "omni", "omni-runner", "omni-mcp", "omni-knowledge", "omni_knowledge.py",
    "omni_messages.py", "omni_embeddings.py", "omni_semantics.py", "omni_memory.py", "omni_json_stream.py",
    "omni_hippocampus.py", "omni_kb.py", "omni_episodes.py", "omni_redact.py", "omni_concepts.py", "omni_vectors.py", "omni_query.py", "omni_topics.py", "omni_storage.py", "omni_explain.py", "omni_summaries.py", "omni_blocks.py", "omni_unblocker.py", "omni_projects.py"))


class MonitoringUnavailable(RuntimeError):
    pass


def helper_names(helper_paths):
    names = {}
    for path in helper_paths:
        filename = str(path)
        names.setdefault(str(Path(filename).resolve()), []).append(filename)
    return names


class MonitoringCollector:
    def __init__(self, helper_paths, monitoring=None):
        self._helper_names = helper_names(helper_paths)
        self._filename_cache = {}
        self._disabled_codes = {}
        self.monitoring = monitoring if monitoring is not None else getattr(sys, "monitoring", None)
        self.counts = {}
        self.helper_events = 0
        self.tool_id = None
        self._name = "omni-helper-coverage-" + str(id(self))

    def _line(self, code, line_number):
        self._disabled_codes[id(code)] = code
        filename = code.co_filename
        if filename not in self._filename_cache:
            self._filename_cache[filename] = self._helper_names.get(str(Path(filename).resolve()), ())
        matched_names = self._filename_cache[filename]
        if matched_names:
            for helper_name in matched_names:
                self.counts[(helper_name, line_number)] = 1
            self.helper_events += 1
        # This disables only this location, not unvisited branches in the code.
        return self.monitoring.DISABLE

    def start(self):
        if self.tool_id is not None:
            raise RuntimeError("Coverage collector is already running")
        if self.monitoring is None:
            raise MonitoringUnavailable("First-hit monitoring requires Python 3.12 or later")
        if any(self.monitoring.get_tool(identifier) is not None for identifier in range(6)):
            raise MonitoringUnavailable("Helper coverage must run alone, without another monitoring tool")
        # Unreserved IDs first. Claiming is atomic; a concurrent tool may win
        # after get_tool, in which case use_tool_id raises and we try the next.
        for candidate in (3, 4, 1, 5, 2, 0):
            if self.monitoring.get_tool(candidate) is not None:
                continue
            try:
                self.monitoring.use_tool_id(candidate, self._name)
            except ValueError:
                continue
            self.tool_id = candidate
            break
        if self.tool_id is None:
            raise MonitoringUnavailable("No free Python monitoring tool ID is available")
        if self._other_tools():
            self.monitoring.free_tool_id(self.tool_id)
            self.tool_id = None
            raise MonitoringUnavailable("Another monitoring tool attached while helper coverage was starting")
        try:
            self.monitoring.register_callback(self.tool_id, self.monitoring.events.LINE, self._line)
            self.monitoring.set_events(self.tool_id, self.monitoring.events.LINE)
        except BaseException:
            self.stop()
            raise
        return self

    def _other_tools(self, own_identifier=None):
        if own_identifier is None:
            own_identifier = self.tool_id
        return any(identifier != own_identifier and self.monitoring.get_tool(identifier) is not None
                   for identifier in range(6))

    def stop(self):
        identifier = self.tool_id
        if identifier is None:
            return
        self.tool_id = None
        if self.monitoring.get_tool(identifier) != self._name:
            return
        try:
            self.monitoring.set_events(identifier, 0)
        finally:
            try:
                self.monitoring.register_callback(identifier, self.monitoring.events.LINE, None)
            finally:
                if self._other_tools(identifier):
                    # Rearming locations also affects other tools on CPython
                    # 3.12/3.13. Retire our slot until this process exits.
                    self._disabled_codes.clear()
                    raise MonitoringUnavailable(
                        "Another monitoring tool attached during helper coverage; run coverage alone "
                        "in a fresh process (its monitoring slot has been retired)")
                try:
                    # Freeing a 3.12/3.13 tool leaves its disabled locations
                    # behind. Rearm seen code only with exclusive ownership.
                    for code in tuple(self._disabled_codes.values()):
                        self.monitoring.set_local_events(identifier, code, self.monitoring.events.LINE)
                        self.monitoring.set_local_events(identifier, code, 0)
                finally:
                    self._disabled_codes.clear()
                    self.monitoring.free_tool_id(identifier)

    def run(self, function):
        self.start()
        try:
            return function()
        finally:
            self.stop()


def collect(function, helper_paths=HELPERS):
    if getattr(sys, "monitoring", None) is not None:
        collector = MonitoringCollector(helper_paths)
        return collector.run(function), collector.counts
    tracer = trace.Trace(count=True, trace=False)
    previous_trace = sys.gettrace()
    previous_thread_trace = (threading.gettrace() if hasattr(threading, "gettrace")
                             else getattr(threading, "_trace_hook", None))
    try:
        result = tracer.runfunc(function)
    finally:
        sys.settrace(previous_trace)
        threading.settrace(previous_thread_trace)
    names = helper_names(helper_paths)
    counts = {}
    for (filename, line_number), hits in tracer.results().counts.items():
        if hits:
            for helper_name in names.get(str(Path(filename).resolve()), ()):
                counts[(helper_name, line_number)] = 1
    return result, counts


def measure():
    output = io.StringIO()

    def run_suite():
        suite = unittest.defaultTestLoader.discover("tests")
        return unittest.TextTestRunner(stream=output, verbosity=1).run(suite)

    result, counts = collect(run_suite)
    rows = []
    for path in HELPERS:
        filename = str(path)
        executable = set(trace._find_executable_linenos(filename))
        covered = {line for (source, line), count in counts.items()
                   if source == filename and count}
        rows.append((path.name, len(covered & executable), len(executable)))
    return result, output.getvalue(), rows


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--fail-under", type=float, default=0,
                        help="fail when aggregate line coverage is below this percentage")
    args = parser.parse_args(argv)
    result, test_output, rows = measure()
    if not result.wasSuccessful():
        print(test_output, file=sys.stderr, end="")
        return 1
    total_covered = sum(covered for _, covered, _ in rows)
    total_lines = sum(lines for _, _, lines in rows)
    percentage = 100 * total_covered / total_lines
    for name, covered, lines in rows:
        print(f"{name:11} {covered:4}/{lines:<4} {100 * covered / lines:5.1f}%")
    print(f"{'total':11} {total_covered:4}/{total_lines:<4} {percentage:5.1f}%")
    if percentage < args.fail_under:
        print(f"Coverage {percentage:.1f}% is below {args.fail_under:.1f}%", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
