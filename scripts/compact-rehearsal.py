#!/usr/bin/env python3
"""Rehearse the knowledge compaction (schema version 2) on a copy of the live index.

1. Copies the live index read-only with VACUUM INTO into tmp/rehearsal/ (free pages are
   dropped, so the copy is the live content without the freelist) and ledgers it.
2. Baseline on the copy: integrity, per-namespace counts, FTS integrity, sizes, and the eval
   queries (memory-eval's search, cached judgments only) for every corpus and mode.
3. Measures recall@20 of int8 against float vectors on the eval queries and picks the
   encoding (int8 at >= 0.99, else float32).
4. Migrates the copy in place (helpers/omni_compact.py), then VACUUM INTO a second file with
   incremental auto-vacuum, and deletes the first copy.
5. Repeats the checks and the eval on the result, compares top-10 ids, writes the report
   to tmp/compact-rehearsal-report.md, and deletes the copies.

The live index is only ever opened with mode=ro.
"""

import argparse
import datetime
import importlib.util
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUPPORT = Path.home() / "Library/Application Support/OmniTerm"
FLOOR_GB = 15.0
RECALL_FLOOR = 0.99


def load(name, path):
    specification = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


COMPACT = load("omni_compact", ROOT / "helpers/omni_compact.py")
EVAL = load("memory_eval", ROOT / "scripts/memory-eval.py")
VECTORS = load("omni_vectors_rehearsal", ROOT / "helpers/omni_vectors.py")


def log(message):
    print("[%s] %s" % (datetime.datetime.now().strftime("%H:%M:%S"), message), file=sys.stderr, flush=True)


def free_gb():
    output = subprocess.run(["forge", "storage", "free"], capture_output=True, text=True).stdout
    match = re.search(r"([\d.]+) GB available", output)
    if match:
        return float(match.group(1))
    stat = os.statvfs(str(ROOT))
    return stat.f_bavail * stat.f_frsize / 1e9


LEDGERED = set()


def ledger_add(path, why):
    """Ledger a copy as it is created: an empty file now (VACUUM INTO accepts one), filled next."""
    Path(path).touch()
    LEDGERED.add(str(path))
    subprocess.run(["forge", "storage", "add", str(path), "--kind", "dump", "--why", why,
                    "--until", "end of session"], check=False)


def ledger_done(path):
    if str(path) in LEDGERED:
        LEDGERED.discard(str(path))
        subprocess.run(["forge", "storage", "done", str(path)], check=False)


def remove_database(path):
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            os.remove(str(path) + suffix)
        except FileNotFoundError:
            pass
    for side in Path(path).parent.glob(Path(path).stem + "-vectors-*"):
        side.unlink()
    ledger_done(path)


def size(path):
    return sum(os.path.getsize(str(path) + suffix) for suffix in ("", "-wal") if os.path.exists(str(path) + suffix))


def table_bytes(connection):
    try:
        rows = connection.execute("SELECT name,SUM(pgsize) FROM dbstat GROUP BY name ORDER BY 2 DESC").fetchall()
    except sqlite3.DatabaseError as exc:
        return {"error": str(exc)}
    return {name: total for name, total in rows}


def checks(path, report, label):
    started = time.monotonic()
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        report[label + "_counts"] = COMPACT.counts(connection)
        report[label + "_verify"] = COMPACT.verify(connection)
        report[label + "_pages"] = {name: connection.execute("PRAGMA " + name).fetchone()[0]
                                    for name in ("page_count", "freelist_count", "page_size", "auto_vacuum")}
        report[label + "_schema_version"] = COMPACT.schema_version(connection)
        report[label + "_table_bytes"] = table_bytes(connection)
    finally:
        connection.close()
    report[label + "_check_seconds"] = round(time.monotonic() - started, 1)
    log("%s checks: %s in %.0fs" % (label, report[label + "_verify"], time.monotonic() - started))


def run_eval(path, queries, judgments):
    started = time.monotonic()
    store = EVAL.open_store(path)
    load_seconds = time.monotonic() - started
    try:
        runs, _ = EVAL.evaluate(store, queries, judgments, None, max_seconds=60.0, progress=log)
        int8 = int8_recall(store, queries)
    finally:
        store.close()
    return runs, round(load_seconds, 1), int8


def int8_recall(store, queries, k=20):
    """Mean recall@k of int8-quantized matrices against the loaded float matrices, per corpus."""
    semantics = store.semantics
    vectors = VECTORS
    result = {}
    for corpus, matrix in semantics._matrices.items():
        if not len(matrix):
            continue
        metadata, data = matrix.to_snapshot()
        scales, quantized = vectors.quantize_rows(data, matrix.dimensions)
        approximate = vectors.VectorMatrix.from_snapshot(metadata, vectors.dequantize_rows(scales, quantized,
                                                                                            matrix.dimensions))
        recalls = []
        for entry in queries:
            query = semantics.embedding.embed(entry["query"])
            exact = {item[1] for item in matrix.top(query, k)}
            if exact:
                recalls.append(len(exact & {item[1] for item in approximate.top(query, k)}) / len(exact))
        result[corpus] = {"queries": len(recalls), "mean_recall": round(sum(recalls) / len(recalls), 4) if recalls else None,
                          "min_recall": round(min(recalls), 4) if recalls else None, "vectors": len(matrix)}
    return result


def parity(before, after, k=10):
    rows = {}
    keyed = {(run["corpus"], run["mode"], run["query_id"]): run for run in after}
    for run in before:
        other = keyed.get((run["corpus"], run["mode"], run["query_id"]))
        if other is None:
            continue
        a, b = run["ranked"][:k], other["ranked"][:k]
        overlap = len(set(a) & set(b)) / max(1, len(set(a) | set(b))) if (a or b) else 1.0
        bucket = rows.setdefault((run["corpus"], run["mode"]), {"queries": 0, "identical": 0, "jaccard": []})
        bucket["queries"] += 1
        bucket["identical"] += a == b
        bucket["jaccard"].append(overlap)
    return {"%s/%s" % key: {"queries": value["queries"], "identical_top10": value["identical"],
                            "mean_jaccard_top10": round(sum(value["jaccard"]) / len(value["jaccard"]), 4),
                            "min_jaccard_top10": round(min(value["jaccard"]), 4)}
            for key, value in sorted(rows.items())}


def gb(value):
    return "%.2f GB" % (value / 1e9)


def render(report):
    lines = ["# Knowledge compaction rehearsal", "",
             "Run %s on a read-only copy of `%s`. The live index was not modified." % (report["started"], report["live"]), ""]
    lines += ["## Result", "",
              "| | Size | Pages | Free pages | auto_vacuum |", "|---|---|---|---|---|",
              "| Live index (file + WAL) | %s | | | |" % gb(report["live_bytes"])]
    for label, name in (("v1", "Copy (VACUUM INTO, before migration)"), ("v2", "Compacted (after migration + VACUUM INTO)")):
        pages = report.get(label + "_pages", {})
        lines.append("| %s | %s | %s | %s | %s |" % (name, gb(report.get(label + "_bytes", 0)), pages.get("page_count"),
                                                   pages.get("freelist_count"), pages.get("auto_vacuum")))
    lines += ["", "## Durations (seconds)", "", "```", json.dumps(report["durations"], indent=1), "```", "",
              "## Integrity", "", "```", json.dumps({"v1": report.get("v1_verify"), "v2": report.get("v2_verify")}, indent=1),
              "```", "", "## Row counts", "", "| | v1 | v2 |", "|---|---|---|"]
    for key in report.get("v1_counts", {}):
        lines.append("| %s | %s | %s |" % (key, report["v1_counts"][key], report.get("v2_counts", {}).get(key)))
    lines += ["", "## Bytes per table (dbstat)", "", "| Table | v1 | v2 |", "|---|---|---|"]
    names = sorted(set(report.get("v1_table_bytes", {})) | set(report.get("v2_table_bytes", {})),
                   key=lambda name: -(report.get("v1_table_bytes", {}).get(name) or 0))
    for name in names[:20]:
        lines.append("| %s | %s | %s |" % (name, gb(report.get("v1_table_bytes", {}).get(name) or 0),
                                           gb(report.get("v2_table_bytes", {}).get(name) or 0)))
    lines += ["", "## int8 against float recall@20 (eval queries)", "", "```",
              json.dumps(report.get("int8_recall"), indent=1), "```", "",
              "Chosen encoding: **%s** (int8 needs mean recall@20 >= %.2f on the transcript corpus)." % (
                  report.get("encoding"), RECALL_FLOOR), "",
              "## Search parity (top-10 ids, v1 vs v2)", "", "```", json.dumps(report.get("parity"), indent=1), "```", "",
              "## Eval (cached judgments, no new judging)", "", "### v1", "", report.get("v1_eval_table", ""), "",
              "### v2", "", report.get("v2_eval_table", ""), "",
              "## Migration report", "", "```", json.dumps(report.get("migration"), indent=1), "```", "",
              "## Notes", ""] + ["- " + note for note in report["notes"]]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", default=str(SUPPORT / "knowledge.sqlite"))
    parser.add_argument("--work", default=str(ROOT / "tmp/rehearsal"))
    parser.add_argument("--report", default=str(ROOT / "tmp/compact-rehearsal-report.md"))
    parser.add_argument("--eval-dir", default=str(SUPPORT / "eval"))
    parser.add_argument("--judge-model", default="gpt-6-luna")
    parser.add_argument("--keep", action="store_true", help="keep the compacted copy (debugging)")
    arguments = parser.parse_args(argv)
    live, work = Path(arguments.live), Path(arguments.work)
    work.mkdir(parents=True, exist_ok=True)
    first, second = work / "knowledge-v1.sqlite", work / "knowledge-v2.sqlite"
    golden = EVAL.load_json(Path(arguments.eval_dir) / "golden.json", {"queries": []})
    judgments = EVAL.load_json(Path(arguments.eval_dir) / ("judgments-%s.json" % arguments.judge_model), {})
    report = {"started": datetime.datetime.now().isoformat(timespec="seconds"), "live": str(live),
              "live_bytes": size(live), "durations": {}, "notes": []}
    estimate = report["live_bytes"] / 1e9
    available = free_gb()
    report["free_gb_at_start"] = available
    log("live index %s, %.1f GB available" % (gb(report["live_bytes"]), available))
    if available - estimate < FLOOR_GB:
        raise SystemExit("not enough free space for a copy: %.1f GB free" % available)
    try:
        for path in (first, second):
            remove_database(path) if path.exists() else None
        ledger_add(first, "compaction rehearsal copy (v1)")
        started = time.monotonic()
        source = sqlite3.connect(live.as_uri() + "?mode=ro", uri=True)
        try:
            source.execute("VACUUM INTO ?", (str(first),))
        finally:
            source.close()
        report["durations"]["copy_vacuum_into"] = round(time.monotonic() - started, 1)
        report["v1_bytes"] = size(first)
        log("copy %s in %.0fs" % (gb(report["v1_bytes"]), report["durations"]["copy_vacuum_into"]))
        checks(first, report, "v1")
        started = time.monotonic()
        before, report["v1_store_load_seconds"], report["int8_recall"] = run_eval(first, golden["queries"], judgments)
        report["durations"]["v1_eval"] = round(time.monotonic() - started, 1)
        report["v1_eval_table"] = EVAL.render(EVAL.summarize(before, judgments), "v1")
        transcript = (report["int8_recall"].get("transcript") or {}).get("mean_recall")
        memory = (report["int8_recall"].get("memory") or {}).get("mean_recall")
        encoding = "int8" if transcript is not None and transcript >= RECALL_FLOOR and \
            (memory is None or memory >= RECALL_FLOOR) else "float32"
        report["encoding"] = encoding
        log("int8 recall %s -> %s" % (report["int8_recall"], encoding))
        connection = sqlite3.connect(first, isolation_level=None)
        try:
            started = time.monotonic()
            report["migration"] = COMPACT.migrate(connection, encoding, progress=lambda state: log(
                "migrate %(batches)d batches, %(messages_stripped)d messages, %(vectors_packed)d vectors, "
                "%(chunks_stripped)d chunks, %(seconds).0fs" % state) if state["batches"] % 25 == 0 else None)
            report["migration_v3"] = COMPACT.migrate_v3(connection)
            report["durations"]["migrate"] = round(time.monotonic() - started, 1)
            report["after_migrate_bytes_in_place"] = size(first)
            if free_gb() - report["v1_bytes"] / 1e9 * 0.8 < FLOOR_GB:
                raise SystemExit("not enough free space for VACUUM INTO")
            ledger_add(second, "compaction rehearsal copy (v2)")
            started = time.monotonic()
            COMPACT.compact(connection, into=second)
            report["durations"]["vacuum_into"] = round(time.monotonic() - started, 1)
        finally:
            connection.close()
        report["min_free_gb_seen"] = free_gb()
        remove_database(first)
        report["v2_bytes"] = size(second)
        log("compacted %s" % gb(report["v2_bytes"]))
        checks(second, report, "v2")
        started = time.monotonic()
        after, report["v2_store_load_seconds"], _ = run_eval(second, golden["queries"], judgments)
        report["durations"]["v2_eval"] = round(time.monotonic() - started, 1)
        report["v2_eval_table"] = EVAL.render(EVAL.summarize(after, judgments), "v2")
        report["parity"] = parity(before, after)
        report["notes"] += [
            "The copy was made with VACUUM INTO on a mode=ro connection, not the backup API: the backup API "
            "copies the 8.9 GB freelist too, and copy plus compacted output would have broken the 15 GB floor.",
            "v1 sizes are therefore without the freelist; the live file carries it on top.",
            "The live run would use an in-place VACUUM (scripts/compact-knowledge.py); this rehearsal used VACUUM INTO.",
            "records/metadata grows by one in v2: the metadata:knowledge_compaction document.",
            "Lexical results come from FTS over `messages`, which the migration does not change; semantic and hybrid "
            "results may move where int8 rounding reorders near-ties.",
        ]
    finally:
        Path(arguments.report).write_text(render(report))
        log("report written to %s" % arguments.report)
        remove_database(first) if first.exists() else None
        if not arguments.keep and second.exists():
            remove_database(second)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
