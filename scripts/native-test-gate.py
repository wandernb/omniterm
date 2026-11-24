#!/usr/bin/env python3
"""Select exported Omni XCTest classes and require a passing result for each."""

import argparse
from pathlib import Path
import re


def discover_suites(directory):
    suites = set()
    declaration = re.compile(
        r"(?:\bclass\s+|@interface\s+)(Omni\w+)\s*:\s*(?:XCTest\.)?XCTestCase\b"
    )
    for source in sorted(directory.glob("Omni*Tests.*")):
        if source.suffix not in (".swift", ".m", ".mm"):
            continue
        content = re.sub(r"/\*.*?\*/|//[^\n]*", "", source.read_text(), flags=re.S)
        classes = declaration.findall(content)
        if not classes:
            raise ValueError(f"No Omni XCTest classes found in {source}")
        suites.update(classes)
    if not suites:
        raise ValueError(f"No exported Omni XCTest classes found in {directory}")
    return sorted(suites)


def verify_results(suites, log):
    if not suites:
        raise ValueError("No expected XCTest suites")
    log = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", log).replace("\r", "")
    result = re.compile(
        r"Test Suite '([^']+)' (passed|failed)[^\n]*\n\s*"
        r"Executed (\d+) tests?, with (\d+) failures?"
    )
    observed = {}
    for match in result.finditer(log):
        suite, status, count, failures = match.groups()
        suite = suite.rsplit(".", 1)[-1]
        observed.setdefault(suite, []).append((status, int(count), int(failures)))
    failed = {
        suite.rsplit(".", 1)[-1]
        for suite in re.findall(r"Test Suite '([^']+)' failed\b", log)
    }
    invalid = []
    for suite in suites:
        results = observed.get(suite, [])
        if suite in failed or not results or any(
            status != "passed" or count < 1 or failures != 0
            for status, count, failures in results
        ):
            invalid.append(suite)
    if invalid:
        raise ValueError("Missing, empty or failing XCTest suites: " + ", ".join(invalid))
    return sum(observed[suite][-1][1] for suite in suites)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    select = commands.add_parser("select")
    select.add_argument("directory", type=Path)
    verify = commands.add_parser("verify")
    verify.add_argument("selectors", type=Path)
    verify.add_argument("log", type=Path)
    arguments = parser.parse_args()
    try:
        if arguments.command == "select":
            print("\n".join("ModernTests/" + suite for suite in discover_suites(arguments.directory)))
        else:
            selectors = arguments.selectors.read_text().splitlines()
            if any(not re.fullmatch(r"ModernTests/Omni\w+", selector) for selector in selectors):
                raise ValueError("Invalid XCTest selector manifest")
            suites = [selector.split("/", 1)[1] for selector in selectors]
            count = verify_results(suites, arguments.log.read_text(errors="replace"))
            print(f"Verified {len(suites)} Omni suites, {count} tests executed")
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
