#!/usr/bin/env python3
"""Measure helper line coverage using Python's standard-library tracer."""

import argparse
import io
from pathlib import Path
import sys
import trace
import unittest


ROOT = Path(__file__).resolve().parents[1]
HELPERS = (ROOT / "helpers/omni", ROOT / "helpers/omni-runner", ROOT / "helpers/omni-mcp")


def measure():
    output = io.StringIO()
    context = {"io": io, "output": output, "unittest": unittest}
    tracer = trace.Trace(count=True, trace=False)
    tracer.runctx(
        "suite = unittest.defaultTestLoader.discover('tests'); "
        "result = unittest.TextTestRunner(stream=output, verbosity=1).run(suite)",
        context,
        context,
    )
    counts = tracer.results().counts
    rows = []
    for path in HELPERS:
        filename = str(path)
        executable = set(trace._find_executable_linenos(filename))
        covered = {line for (source, line), count in counts.items()
                   if source == filename and count}
        rows.append((path.name, len(covered & executable), len(executable)))
    return context["result"], output.getvalue(), rows


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
