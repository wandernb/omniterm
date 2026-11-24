#!/usr/bin/env python3
"""Compact OmniTerm's knowledge index to schema version 3 (owner-run, app quit).

Version 3 keeps each message once, in `messages`, with no `records` row behind it.

One stored copy of transcript text (in `messages`), vectors as int8 or float32 blobs, then
incremental auto-vacuum and one VACUUM, and the filter sidecar cleared of rows for deleted
messages and rebuilt. Safe to rerun on a version-2 index: it then only repacks the files
(pages left half empty by trimmed tool output and stripped records). OmniTerm never runs this itself: an app rebuild or
relaunch cannot migrate the index. Rehearse first with scripts/compact-rehearsal.py.

Before running: quit OmniTerm, back the index up somewhere else, and leave at least 15 GB free
beyond the vacuum's working space (about the compacted size, twice in WAL mode).
"""

import argparse
import fcntl
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUPPORT = Path.home() / "Library/Application Support/OmniTerm"
FLOOR_BYTES = 15 * 1000 ** 3

_SPEC = importlib.util.spec_from_file_location("omni_compact", ROOT / "helpers/omni_compact.py")
COMPACT = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(COMPACT)


def app_running(database):
    """True while an OmniTerm scheduler holds the support directory's lock."""
    lock = Path(database).parent / "scheduler.lock"
    if not lock.exists():
        return False
    with open(lock, "a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
    return False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database", default=str(SUPPORT / "knowledge.sqlite"))
    parser.add_argument("--encoding", choices=COMPACT.ENCODINGS, required=True,
                        help="the vector encoding the rehearsal report recommends")
    parser.add_argument("--backup-confirmed", action="store_true", required=True,
                        help="a fresh backup of the index exists outside this directory")
    parser.add_argument("--vacuum", choices=("inplace", "none"), default="inplace")
    parser.add_argument("--batch", type=int, default=COMPACT.BATCH_RECORDS)
    arguments = parser.parse_args(argv)
    database = Path(arguments.database)
    if not database.is_file():
        parser.error("no index at %s" % database)
    if app_running(database):
        parser.error("OmniTerm is running; quit it first")
    if shutil.disk_usage(database.parent).free < FLOOR_BYTES:
        parser.error("less than 15 GB free")
    connection = sqlite3.connect(database, isolation_level=None)
    try:
        connection.execute("PRAGMA busy_timeout=5000")
        version = COMPACT.schema_version(connection)
        print("schema version %d, %.1f GB" % (version, database.stat().st_size / 1e9), file=sys.stderr)
        started = time.monotonic()
        report = COMPACT.migrate(connection, arguments.encoding, arguments.batch,
                                 progress=lambda state: print(json.dumps(state), file=sys.stderr))
        if report.get("done"):
            report["v3"] = COMPACT.migrate_v3(connection, arguments.batch,
                                              progress=lambda state: print(json.dumps(state), file=sys.stderr))
        if arguments.vacuum == "inplace":
            COMPACT.compact(connection)
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            report["filter_sidecar"] = COMPACT.compact_sidecar(database)
        report["checks"] = COMPACT.verify(connection)
        report["total_seconds"] = round(time.monotonic() - started, 1)
        report["size_bytes"] = database.stat().st_size
        print(json.dumps(report, indent=1))
        ok = (report["checks"]["integrity_check"] == "ok" and report["checks"]["message_fts"] == "ok"
              and report["checks"]["message_records"] == 0)
        return 0 if ok and report.get("done") and report.get("v3", {}).get("done") else 1
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
