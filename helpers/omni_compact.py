"""Knowledge index compaction: schema version 2 (one copy of text, vectors as blobs).

The app never runs this. The owner runs scripts/compact-knowledge.py with the app quit, and
scripts/compact-rehearsal.py runs it on a copy. Every step is idempotent and commits in
bounded batches, so an interrupted run leaves a consistent index that a rerun finishes:
readers take transcript text from `messages` and decode JSON and blob vectors alike.
"""

import importlib.util
import json
import sqlite3
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCHEMA_V2 = HERE / "omni_knowledge_schema_v2.sql"
SCHEMA_V3 = HERE / "omni_knowledge_schema_v3.sql"
COMPACTION_KEY = "metadata:knowledge_compaction"
TARGET_VERSION = 2
ENCODINGS = ("int8", "float32")
BATCH_RECORDS = 20000

_SPEC = importlib.util.spec_from_file_location("omni_vectors_for_compaction", HERE / "omni_vectors.py")
VECTORS = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(VECTORS)


def schema_version(connection, schema="main"):
    try:
        row = connection.execute("SELECT max(version) FROM " + schema + ".knowledge_schema").fetchone()
    except sqlite3.DatabaseError:
        return 0
    return row[0] or 0


def compaction_state(connection, schema="main"):
    try:
        row = connection.execute("SELECT value FROM " + schema + ".records WHERE key=?", (COMPACTION_KEY,)).fetchone()
    except sqlite3.DatabaseError:
        return None
    try:
        return json.loads(row[0]) if row else None
    except ValueError:
        return None


def _pack(encoding):
    def pack(raw, dimensions):
        if not isinstance(raw, str):
            return raw  # already a blob
        try:
            vector = json.loads(raw)
        except ValueError:
            return raw
        if not isinstance(vector, list) or len(vector) != dimensions:
            return raw
        return VECTORS.encode_vector(vector, encoding)
    return pack


def _boundaries(connection, low, high, batch):
    """Upper keys of consecutive batches in [low, high)."""
    after = low
    while True:
        row = connection.execute("SELECT key FROM records WHERE key>? AND key<? ORDER BY key LIMIT 1 OFFSET ?",
                                 (after, high, batch - 1)).fetchone()
        if row is None:
            yield after, high
            return
        yield after, row[0]
        after = row[0]


def _checkpoint(connection):
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    except sqlite3.DatabaseError:
        pass


def migrate(connection, encoding="int8", batch=BATCH_RECORDS, progress=None, stop_after=None):
    """Bring a version-1 index to version 2 in place. Returns per-step counts.

    `connection` must be in autocommit mode (isolation_level=None). `stop_after` (tests)
    ends the run after that many batches, as an interruption would.
    """
    if encoding not in ENCODINGS:
        raise ValueError("encoding must be int8 or float32")
    if connection.isolation_level is not None:
        raise ValueError("migrate needs an autocommit connection (isolation_level=None)")
    state = compaction_state(connection) or {}
    if state.get("vector_encoding") and state["vector_encoding"] != encoding:
        encoding = state["vector_encoding"]  # a resumed run keeps the encoding it started with
    connection.create_function("omni_pack_vector", 2, _pack(encoding), deterministic=True)
    report = {"encoding": encoding, "messages_stripped": 0, "vectors_packed": 0, "chunks_stripped": 0, "batches": 0}
    started = time.monotonic()
    connection.execute("BEGIN IMMEDIATE")
    try:
        # A version-3 index keeps its message triggers: the v2 bodies would bring back record rows.
        for statement in (_statements(SCHEMA_V2.read_text()) if schema_version(connection) < 3 else ()):
            connection.execute(statement)
        connection.execute("INSERT OR REPLACE INTO records(key,value) VALUES(?,?)", (COMPACTION_KEY, json.dumps(
            {"version": TARGET_VERSION, "vector_encoding": encoding, "state": "migrating",
             "started": state.get("started") or time.time()}, sort_keys=True)))
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise

    def step(sql, low, high, counter):
        connection.execute("BEGIN IMMEDIATE")
        try:
            changed = connection.execute(sql, (low, high)).rowcount
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        report[counter] += max(changed, 0)
        report["batches"] += 1
        _checkpoint(connection)
        if progress:
            progress(dict(report, seconds=round(time.monotonic() - started, 1), key=high))
        return stop_after is not None and report["batches"] >= stop_after

    for low, high in _boundaries(connection, "message:", "message;", batch):
        if step("UPDATE records SET value=json_remove(value,'$.text') WHERE key>? AND key<=? "
                "AND key<'message;' AND json_type(value,'$.text') IS NOT NULL", low, high, "messages_stripped"):
            return report
    for low, high in _boundaries(connection, "vector:", "vector;", batch):
        if step("UPDATE vectors SET vector=omni_pack_vector(vector,dimensions) WHERE key>? AND key<=? "
                "AND key<'vector;' AND typeof(vector)='text'", low, high, "vectors_packed"):
            return report
        if step("UPDATE records SET value=json_remove(value,'$.vector','$.vector_hex','$.chunk') WHERE key>? AND key<=? "
                "AND key<'vector;' AND json_extract(value,'$.kind')='chunk' AND (json_type(value,'$.vector') IS NOT NULL "
                "OR json_type(value,'$.vector_hex') IS NOT NULL OR json_type(value,'$.chunk') IS NOT NULL)",
                low, high, "chunks_stripped"):
            return report
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("INSERT OR IGNORE INTO knowledge_schema(version) VALUES(?)", (TARGET_VERSION,))
        state = compaction_state(connection) or {}
        state.update(state="done", finished=time.time(), vector_encoding=encoding, version=TARGET_VERSION)
        connection.execute("INSERT OR REPLACE INTO records(key,value) VALUES(?,?)",
                           (COMPACTION_KEY, json.dumps(state, sort_keys=True)))
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    report["seconds"] = round(time.monotonic() - started, 1)
    report["done"] = True
    return report


def migrate_v3(connection, batch=BATCH_RECORDS, progress=None, stop_after=None):
    """Bring a version-2 index to version 3 in place: a message lives in `messages` only.

    The trigger swap and the version mark commit together, so the worker switches to delete
    markers exactly when record rows stop being kept. Then the `message:` record rows are
    deleted in bounded batches (the new delete trigger leaves `messages` alone). Idempotent.
    """
    if connection.isolation_level is not None:
        raise ValueError("migrate_v3 needs an autocommit connection (isolation_level=None)")
    if schema_version(connection) < 2:
        raise ValueError("migrate_v3 needs a version-2 index; run migrate first")
    report = {"message_records_deleted": 0, "batches": 0}
    started = time.monotonic()
    messages_before = connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    connection.execute("BEGIN IMMEDIATE")
    try:
        for statement in _statements(SCHEMA_V3.read_text()):
            connection.execute(statement)
        body = connection.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='records_messages_delete'").fetchone()
        if body is None or "WHEN 0" not in body[0]:
            raise RuntimeError("records_messages_delete was not neutralised")
        connection.execute("INSERT OR IGNORE INTO knowledge_schema(version) VALUES(3)")
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    while True:
        connection.execute("BEGIN IMMEDIATE")
        try:
            deleted = connection.execute(
                "DELETE FROM records WHERE key IN (SELECT key FROM records WHERE key>='message:' AND key<'message;' "
                "ORDER BY key LIMIT ?)", (batch,)).rowcount
            if report["batches"] == 0 and connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] != messages_before:
                raise RuntimeError("deleting message records deleted messages")
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        report["message_records_deleted"] += max(deleted, 0)
        report["batches"] += 1
        _checkpoint(connection)
        if progress:
            progress(dict(report, seconds=round(time.monotonic() - started, 1)))
        if deleted < batch or (stop_after is not None and report["batches"] >= stop_after):
            break
    report["messages"] = connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    if report["messages"] != messages_before:
        raise RuntimeError("message count changed: %d -> %d" % (messages_before, report["messages"]))
    report["seconds"] = round(time.monotonic() - started, 1)
    report["done"] = deleted < batch
    return report


def _statements(script):
    """Split the trusted v2 script into complete statements (triggers contain semicolons)."""
    statements, current = [], ""
    for line in script.splitlines(keepends=True):
        if line.lstrip().startswith("--") and not current.strip():
            continue
        current += line
        if sqlite3.complete_statement(current):
            if current.strip():
                statements.append(current.strip())
            current = ""
    if current.strip():
        raise ValueError("Incomplete statement in a schema script")
    return statements


def compact(connection, into=None):
    """Merge the full-text indexes' segments (rewritten and trimmed rows leave deleted
    entries in them: about 280 MB of 1.3 GB on a live index), switch to incremental
    auto-vacuum and rebuild the file once (in place, or VACUUM INTO)."""
    for fts in ("message_fts", "memory_fts"):
        connection.execute("INSERT INTO " + fts + "(" + fts + ") VALUES('optimize')")
    connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
    if into is None:
        connection.execute("VACUUM")
    else:
        connection.execute("VACUUM INTO ?", (str(into),))


def compact_sidecar(database):
    """Drop filter-sidecar rows whose message the index no longer holds, switch the sidecar
    to incremental auto-vacuum and rebuild it once. Run after the index's own VACUUM.
    Returns {"stale_rows": n, "bytes_before": b, "bytes_after": a}, or None without a sidecar."""
    database = Path(database)
    path = database.with_name(database.stem + "-filters.sqlite")
    if not path.is_file():
        return None
    before = path.stat().st_size
    connection = sqlite3.connect(path.as_uri(), uri=True, isolation_level=None)
    try:
        connection.execute("ATTACH DATABASE ? AS idx", (database.as_uri() + "?mode=ro",))
        stale = connection.execute("DELETE FROM message_filters WHERE NOT EXISTS("
                                   "SELECT 1 FROM idx.messages m WHERE m.rowid=message_filters.rid)").rowcount
        connection.execute("DETACH DATABASE idx")
        connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
        connection.execute("VACUUM")
    finally:
        connection.close()
    return {"stale_rows": stale, "bytes_before": before, "bytes_after": path.stat().st_size}


def verify(connection):
    """Checks a compacted index must pass; returns {name: result}."""
    results = {"integrity_check": connection.execute("PRAGMA integrity_check").fetchone()[0]}
    for fts in ("message_fts", "memory_fts"):
        try:
            connection.execute("INSERT INTO " + fts + "(" + fts + ") VALUES('integrity-check')")
            results[fts] = "ok"
        except sqlite3.DatabaseError as exc:
            results[fts] = str(exc)
    results["schema_version"] = schema_version(connection)
    results["message_records"] = connection.execute(
        "SELECT COUNT(*) FROM records WHERE key>='message:' AND key<'message;'").fetchone()[0]
    results["records_with_text"] = connection.execute(
        "SELECT COUNT(*) FROM records WHERE key>='message:' AND key<'message;' AND json_type(value,'$.text') IS NOT NULL").fetchone()[0]
    results["records_with_vector"] = connection.execute(
        "SELECT COUNT(*) FROM records WHERE key>='vector:' AND key<'vector;' AND (json_type(value,'$.vector') IS NOT NULL "
        "OR json_type(value,'$.vector_hex') IS NOT NULL OR json_type(value,'$.chunk') IS NOT NULL)").fetchone()[0]
    results["text_vectors"] = connection.execute("SELECT COUNT(*) FROM vectors WHERE typeof(vector)='text'").fetchone()[0]
    return results


def counts(connection):
    """Row counts per record namespace and per projection table."""
    result = {}
    for prefix in ("source:", "message:", "memory:", "memory_revision:", "receipt:", "vector:", "metadata:"):
        result["records/" + prefix.rstrip(":")] = connection.execute(
            "SELECT COUNT(*) FROM records WHERE key>=? AND key<?", (prefix, prefix[:-1] + ";")).fetchone()[0]
    result["records/vector_chunks"] = connection.execute(
        "SELECT COUNT(*) FROM records WHERE key>='vector:' AND key<'vector;' AND json_extract(value,'$.kind')='chunk'").fetchone()[0]
    for table in ("sources", "messages", "memories", "vectors", "semantic_chunks"):
        result[table] = connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
    return result
