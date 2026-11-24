import importlib.util
import json
import math
import random
import sqlite3
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(name, filename):
    specification = importlib.util.spec_from_file_location(name, ROOT / "helpers" / filename)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


KNOWLEDGE = load("omni_knowledge_for_compaction_tests", "omni_knowledge.py")
SEMANTICS = load("omni_semantics_for_compaction_tests", "omni_semantics.py")
COMPACT = load("omni_compact_for_tests", "omni_compact.py")
VECTORS = load("omni_vectors_for_compaction_tests", "omni_vectors.py")
SCHEMA = (ROOT / "helpers/omni_knowledge_schema.sql").read_text()
DIMENSIONS = 8


class Embedding:
    available = True
    model_id = "compaction-test:d8"
    dimensions = DIMENSIONS

    def status(self):
        return {"available": True, "model_id": self.model_id, "dimensions": self.dimensions}

    def embed(self, text):
        generator = random.Random(" ".join(sorted(set(text.lower().split()))))
        vector = [generator.gauss(0, 1) for _ in range(self.dimensions)]
        norm = math.sqrt(sum(value * value for value in vector))
        return [value / norm for value in vector]


TEXTS = {"m1": "database connection pool exhausted", "m2": "fresh bread recipe", "m3": "deploy the release",
         "m4": "keychain stores the token"}


def message(identifier, text):
    return {"id": identifier, "source_key": "source:fixture", "session_id": "claude:fixture", "provider": "claude",
            "workspace": "/w", "source_path": "/fixture.jsonl", "source_line": 1, "part": 0, "role": "assistant",
            "kind": "message", "text": text, "timestamp": "2026-11-01T00:00:00Z"}


def vector_document(identifier, text, embedding):
    prefix = SEMANTICS.vector_prefix("transcript", identifier)
    return {"key": prefix + "0", "kind": "chunk", "corpus": "transcript", "record_id": identifier, "chunk_index": 0,
            "chunk": text, "content_hash": SEMANTICS.content_hash(text), "model": embedding.model_id,
            "dimensions": embedding.dimensions, "vector": embedding.embed(text)}


class Fixture:
    """A version-1 native index written the way OmniScheduler.m writes it."""

    def __init__(self, test):
        directory = tempfile.TemporaryDirectory()
        test.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.path = self.directory / "knowledge.sqlite"
        self.embedding = Embedding()
        self.native = sqlite3.connect(self.path, isolation_level=None)
        test.addCleanup(self.native.close)
        self.native.execute("PRAGMA journal_mode=WAL")
        self.native.execute("CREATE TABLE IF NOT EXISTS records (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.native.executescript(SCHEMA)
        self.write("source:fixture", {"key": "source:fixture", "session_id": "claude:fixture", "provider": "claude",
                                      "path": "/fixture.jsonl"})
        for identifier, text in TEXTS.items():
            self.write("message:" + identifier, message(identifier, text))
            vector = vector_document(identifier, text, self.embedding)
            self.write(vector["key"], vector)
            manifest = {name: value for name, value in vector.items() if name not in ("vector", "chunk")}
            manifest.update(key=SEMANTICS.vector_prefix("transcript", identifier) + "manifest", kind="manifest",
                            vector_keys=[vector["key"]])
            self.write(manifest["key"], manifest)

    def write(self, key, value):
        self.native.execute("BEGIN IMMEDIATE")
        if value is None:
            self.native.execute("DELETE FROM records WHERE key=?", (key,))
        else:
            self.native.execute("INSERT OR REPLACE INTO records(key,value) VALUES(?,?)", (key, json.dumps(value)))
        self.native.execute("COMMIT")

    def record(self, key):
        row = self.native.execute("SELECT value FROM records WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def migrate(self, **arguments):
        return COMPACT.migrate(self.native, **arguments)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture(self)
        self.native = self.fixture.native

    def triggers(self):
        return dict(self.native.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'"))

    def test_trigger_swap_is_versioned_and_survives_the_apps_startup_schema(self):
        before = self.triggers()
        self.assertEqual(COMPACT.schema_version(self.native), 1)
        report = self.fixture.migrate()
        self.assertTrue(report["done"])
        self.assertEqual(COMPACT.schema_version(self.native), 2)
        state = COMPACT.compaction_state(self.native)
        self.assertEqual((state["state"], state["vector_encoding"]), ("done", "int8"))
        after = self.triggers()
        self.assertNotEqual(before["records_messages_insert"], after["records_messages_insert"])
        self.assertIn("json_remove", after["records_messages_insert"])
        self.assertIn("unhex", after["records_vectors_insert"])
        changes = self.native.total_changes
        self.native.executescript(SCHEMA)  # the next app launch
        self.assertEqual(self.triggers(), after, "CREATE TRIGGER IF NOT EXISTS must keep the v2 bodies")
        self.assertEqual(self.native.total_changes, changes)
        self.assertEqual(self.fixture.migrate()["messages_stripped"], 0, "a rerun is a no-op")

    def test_message_text_lives_once_and_new_writes_are_projected_then_stripped(self):
        counts = COMPACT.counts(self.native)
        self.fixture.migrate()
        self.assertEqual({key: value for key, value in COMPACT.counts(self.native).items() if key != "records/metadata"},
                         {key: value for key, value in counts.items() if key != "records/metadata"})
        self.assertNotIn("text", self.fixture.record("message:m1"))
        self.assertEqual(self.fixture.record("message:m1")["role"], "assistant")
        self.assertEqual(self.native.execute("SELECT text FROM messages WHERE id='m1'").fetchone()[0], TEXTS["m1"])
        checks = COMPACT.verify(self.native)
        self.assertEqual((checks["integrity_check"], checks["message_fts"], checks["records_with_text"]), ("ok", "ok", 0))
        self.fixture.write("message:m5", message("m5", "brand new turn"))
        self.assertNotIn("text", self.fixture.record("message:m5"))
        self.assertEqual(self.native.execute("SELECT m.id FROM message_fts JOIN messages m ON m.rowid=message_fts.rowid "
                                             "WHERE message_fts MATCH 'brand'").fetchall(), [("m5",)])
        self.fixture.write("message:m5", message("m5", "edited turn"))
        self.assertEqual(self.native.execute("SELECT text FROM messages WHERE id='m5'").fetchone()[0], "edited turn")
        self.assertEqual(self.native.execute("SELECT COUNT(*) FROM message_fts WHERE message_fts MATCH 'brand'").fetchone()[0], 0)
        self.fixture.write("message:m5", None)
        self.assertIsNone(self.native.execute("SELECT 1 FROM messages WHERE id='m5'").fetchone())
        self.assertEqual(COMPACT.verify(self.native)["message_fts"], "ok")

    def test_vectors_become_blobs_and_records_drop_vector_and_chunk(self):
        self.fixture.migrate(encoding="int8")
        rows = self.native.execute("SELECT typeof(vector),length(vector) FROM vectors").fetchall()
        self.assertEqual(set(rows), {("blob", DIMENSIONS + 4)})
        key = SEMANTICS.vector_prefix("transcript", "m1") + "0"
        record = self.fixture.record(key)
        self.assertFalse({"vector", "chunk", "vector_hex"} & set(record))
        self.assertEqual(record["content_hash"], SEMANTICS.content_hash(TEXTS["m1"]))
        self.assertEqual(self.native.execute("SELECT chunk FROM semantic_chunks WHERE key=?", (key,)).fetchone()[0], TEXTS["m1"])
        self.assertEqual(COMPACT.verify(self.native)["records_with_vector"], 0)
        # A compacted index receives hex blobs from the helper...
        fresh = vector_document("m4", "keychain token rotated", self.fixture.embedding)
        encoded = VECTORS.encode_vector(fresh.pop("vector"), "int8")
        self.fixture.write(fresh["key"], {**fresh, "vector_hex": encoded.hex()})
        stored = self.native.execute("SELECT vector FROM vectors WHERE key=?", (fresh["key"],)).fetchone()[0]
        self.assertEqual(stored, encoded)
        self.assertEqual(self.native.execute("SELECT chunk FROM semantic_chunks WHERE key=?", (fresh["key"],)).fetchone()[0],
                         "keychain token rotated")
        self.assertFalse({"vector_hex", "chunk"} & set(self.fixture.record(fresh["key"])))
        # ...and an older helper's JSON list is still stored, decodable, and stripped.
        legacy = vector_document("m3", "deploy the release", self.fixture.embedding)
        self.fixture.write(legacy["key"], legacy)
        raw = self.native.execute("SELECT vector FROM vectors WHERE key=?", (legacy["key"],)).fetchone()[0]
        self.assertEqual(SEMANTICS.SemanticIndex.decode_vector(raw, DIMENSIONS), legacy["vector"])
        self.assertNotIn("vector", self.fixture.record(legacy["key"]))
        self.fixture.write(legacy["key"], None)
        self.assertIsNone(self.native.execute("SELECT 1 FROM semantic_chunks WHERE key=?", (legacy["key"],)).fetchone())

    def test_float32_encoding_keeps_exact_vectors(self):
        self.fixture.migrate(encoding="float32")
        key = SEMANTICS.vector_prefix("transcript", "m2") + "0"
        raw = self.native.execute("SELECT vector FROM vectors WHERE key=?", (key,)).fetchone()[0]
        self.assertEqual(len(raw), 4 * DIMENSIONS)
        expected = self.fixture.embedding.embed(TEXTS["m2"])
        for value, wanted in zip(SEMANTICS.SemanticIndex.decode_vector(raw, DIMENSIONS), expected):
            self.assertAlmostEqual(value, wanted, places=6)

    def test_an_interrupted_migration_resumes_to_the_same_result(self):
        partial = self.fixture.migrate(batch=2, stop_after=1)
        self.assertNotIn("done", partial)
        self.assertEqual(COMPACT.schema_version(self.native), 1, "version 2 is recorded only at the end")
        self.assertEqual(COMPACT.compaction_state(self.native)["state"], "migrating")
        self.assertEqual(self.native.execute("SELECT text FROM messages WHERE id='m4'").fetchone()[0], TEXTS["m4"])
        resumed = self.fixture.migrate(encoding="float32", batch=2)
        self.assertTrue(resumed["done"])
        self.assertEqual(resumed["encoding"], "int8", "a resumed run keeps the encoding it started with")
        checks = COMPACT.verify(self.native)
        self.assertEqual((checks["records_with_text"], checks["records_with_vector"], checks["text_vectors"]), (0, 0, 0))

    def test_compact_switches_to_incremental_auto_vacuum(self):
        self.fixture.migrate()
        target = self.fixture.directory / "compacted.sqlite"
        COMPACT.compact(self.native, into=target)
        copy = sqlite3.connect(target)
        self.addCleanup(copy.close)
        self.assertEqual(copy.execute("PRAGMA auto_vacuum").fetchone()[0], 2)
        self.assertEqual(copy.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        COMPACT.compact(self.native)
        self.assertEqual(self.native.execute("PRAGMA auto_vacuum").fetchone()[0], 2)


class VersionThreeTests(unittest.TestCase):
    """Version 3: a message lives in `messages` only; removals travel as delete markers."""

    def setUp(self):
        self.fixture = Fixture(self)
        self.native = self.fixture.native
        self.fixture.migrate()
        self.report = COMPACT.migrate_v3(self.native, batch=2)

    def message_records(self):
        return self.native.execute("SELECT COUNT(*) FROM records WHERE key>='message:' AND key<'message;'").fetchone()[0]

    def hits(self, word):
        return [row[0] for row in self.native.execute(
            "SELECT m.id FROM message_fts JOIN messages m ON m.rowid=message_fts.rowid WHERE message_fts MATCH ?", (word,))]

    def test_record_rows_go_and_every_message_stays(self):
        self.assertTrue(self.report["done"])
        self.assertEqual((self.report["message_records_deleted"], self.report["messages"]), (4, 4))
        self.assertEqual(COMPACT.schema_version(self.native), 3)
        self.assertEqual(self.message_records(), 0)
        self.assertEqual(self.hits("bread"), ["m2"])
        checks = COMPACT.verify(self.native)
        self.assertEqual((checks["integrity_check"], checks["message_fts"], checks["message_records"]), ("ok", "ok", 0))
        self.assertEqual(COMPACT.migrate_v3(self.native)["message_records_deleted"], 0, "a rerun is a no-op")

    def test_writes_project_without_a_record_and_markers_delete(self):
        self.fixture.write("message:m5", message("m5", "brand new turn"))
        self.assertEqual((self.message_records(), self.hits("brand")), (0, ["m5"]))
        self.fixture.write("message:m5", message("m5", "edited turn"))
        self.assertEqual((self.hits("brand"), self.hits("edited")), ([], ["m5"]))
        self.fixture.write("message:m5", KNOWLEDGE.MESSAGE_DELETE_MARKER)
        self.assertIsNone(self.native.execute("SELECT 1 FROM messages WHERE id='m5'").fetchone())
        self.assertEqual((self.hits("edited"), self.message_records()), ([], 0))
        self.fixture.write("message:unknown", KNOWLEDGE.MESSAGE_DELETE_MARKER)  # no NOT NULL failure, no row
        self.assertEqual((self.message_records(), COMPACT.verify(self.native)["message_fts"]), (0, "ok"))
        self.assertEqual(self.native.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 4)

    def test_the_apps_startup_schema_and_a_v2_rerun_keep_version_three(self):
        triggers = dict(self.native.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'"))
        self.native.executescript(SCHEMA)  # the next app launch
        self.fixture.migrate()  # the owner script reruns the v2 step first
        self.assertEqual(dict(self.native.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'")), triggers)
        self.fixture.write("message:m6", message("m6", "after relaunch"))
        self.assertEqual(self.message_records(), 0)

    def test_the_worker_sends_markers_only_to_a_version_three_index(self):
        store = KNOWLEDGE.KnowledgeStore(roots=[], home=str(self.fixture.directory), semantics=False,
                                         read_index=self.fixture.path)
        self.addCleanup(store.close)
        store._pending["message:m1"] = (1, None)
        self.assertEqual(store._batch()["_writes"], [{"key": "message:m1", "value": KNOWLEDGE.MESSAGE_DELETE_MARKER}])
        older = Fixture(self)
        older.migrate()
        plain = KNOWLEDGE.KnowledgeStore(roots=[], home=str(older.directory), semantics=False, read_index=older.path)
        self.addCleanup(plain.close)
        plain._pending["message:m1"] = (1, None)
        self.assertEqual(plain._batch()["_writes"], [{"key": "message:m1", "value": None}])
        for write in store._batch()["_writes"]:
            self.fixture.write(write["key"], write["value"])
        self.assertEqual(self.hits("database"), [])


class CodecTests(unittest.TestCase):
    def test_int8_round_trip_error_is_within_half_a_step(self):
        generator = random.Random(7)
        vector = [generator.uniform(-1, 1) for _ in range(512)]
        encoded = VECTORS.encode_int8(vector)
        self.assertEqual(len(encoded), 516)
        decoded = SEMANTICS.SemanticIndex.decode_vector(encoded, 512)
        step = max(abs(value) for value in vector) / 127
        self.assertLessEqual(max(abs(a - b) for a, b in zip(vector, decoded)), step / 2 + 1e-6)

    def test_decode_reads_every_stored_form_and_rejects_bad_lengths(self):
        vector = [0.5, -0.25, 1.0]
        decode = SEMANTICS.SemanticIndex.decode_vector
        self.assertEqual(decode(json.dumps(vector), 3), vector)
        self.assertEqual(list(decode(VECTORS.encode_float32(vector), 3)), vector)
        self.assertEqual([round(value, 2) for value in decode(VECTORS.encode_int8(vector), 3)], [0.5, -0.25, 1.0])
        self.assertEqual([round(value, 2) for value in decode(VECTORS.encode_int8(vector).hex(), 3, hexadecimal=True)],
                         [0.5, -0.25, 1.0])
        self.assertIsNone(decode(b"\x00" * 5, 3))
        self.assertIsNone(decode("[1, 2]", 3))
        self.assertIsNone(decode("zz", 3, hexadecimal=True))
        self.assertEqual(decode(VECTORS.encode_int8([0.0, 0.0, 0.0]), 3), [0.0, 0.0, 0.0])

    def test_matrix_quantization_matches_the_per_vector_codec(self):
        generator = random.Random(3)
        rows = [[generator.gauss(0, 1) for _ in range(16)] for _ in range(5)]
        import array
        data = array.array("f", [value for row in rows for value in row])
        scales, quantized = VECTORS.quantize_rows(data, 16)
        back = VECTORS.dequantize_rows(scales, quantized, 16)
        for index, row in enumerate(rows):
            single = SEMANTICS.SemanticIndex.decode_vector(VECTORS.encode_int8(row), 16)
            for a, b in zip(back[index * 16:(index + 1) * 16], single):
                self.assertAlmostEqual(a, b, delta=scales[index] + 1e-6)  # rounding may differ by one step


class CompactedReadthroughTests(unittest.TestCase):
    """The helper over a migrated index: text and vectors come from the projections."""

    def setUp(self):
        self.fixture = Fixture(self)
        self.fixture.migrate()
        self.store = KNOWLEDGE.KnowledgeStore(roots=[], home=str(self.fixture.directory), semantics=False,
                                              read_index=self.fixture.path)
        self.addCleanup(lambda: self.store.close())
        self.index = SEMANTICS.SemanticIndex(self.store, embedding=self.fixture.embedding)
        self.index.load_matrices(None)

    def test_readers_still_return_text(self):
        lexical = self.store.search_transcripts({"query": "bread", "mode": "lexical"})
        self.assertEqual([(item["id"], item["text"]) for item in lexical["matches"]], [("m2", TEXTS["m2"])])
        semantic = self.index.search(TEXTS["m1"], mode="semantic", limit=2)
        self.assertEqual(semantic["matches"][0]["id"], "m1")
        self.assertEqual(semantic["matches"][0]["text"], TEXTS["m1"])
        self.assertEqual(semantic["matches"][0]["chunk"], TEXTS["m1"])
        self.assertGreater(semantic["matches"][0]["semantic_score"], 0.99)

    def test_storage_is_reported_and_new_vectors_travel_as_hex(self):
        self.assertEqual(self.index.vector_storage(), {"schema_version": 2, "blob_vectors": True, "encoding": "int8"})
        self.assertEqual(self.index.status()["vector_storage"]["encoding"], "int8")
        value = vector_document("m9", "a new chunk", self.fixture.embedding)
        wire = self.index._wire_vector(value)
        self.assertNotIn("vector", wire)
        self.fixture.write(wire["key"], wire)
        raw = self.fixture.native.execute("SELECT vector FROM vectors WHERE key=?", (wire["key"],)).fetchone()[0]
        self.assertEqual(len(raw), DIMENSIONS + 4)
        restored = SEMANTICS.SemanticIndex(self.store, embedding=self.fixture.embedding)
        restored.restore_vector(wire["key"], wire)  # the overlay accepts the hex form too
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM overlay_vectors WHERE key=?",
                                                       (wire["key"],)).fetchone()[0], 1)

    def test_an_uncompacted_index_keeps_json_vectors(self):
        fixture = Fixture(self)
        store = KNOWLEDGE.KnowledgeStore(roots=[], home=str(fixture.directory), semantics=False, read_index=fixture.path)
        self.addCleanup(store.close)
        index = SEMANTICS.SemanticIndex(store, embedding=fixture.embedding)
        self.assertEqual(index.vector_storage(), {"schema_version": 1, "blob_vectors": False, "encoding": None})
        value = vector_document("m9", "a new chunk", fixture.embedding)
        self.assertIs(index._wire_vector(value), value)

    def test_restoring_the_snapshot_pauses_for_a_waiting_request(self):
        self.index._matrix_loader["transcript"]["persisted_after"] = 0
        self.assertTrue(self.index.maybe_save_snapshot(now=0))
        fresh = SEMANTICS.SemanticIndex(self.store, embedding=self.fixture.embedding)
        self.store.request_waiting = lambda: True
        self.addCleanup(setattr, self.store, "request_waiting", lambda: False)
        calls = 0
        while fresh.load_matrices(budget_seconds=60):
            calls += 1
            self.assertLess(calls, 50)
        self.assertGreater(calls, 3, "Each stage of the restore is its own call")
        self.assertEqual(sorted(fresh._matrices["transcript"].key_row), sorted(self.index._matrices["transcript"].key_row))

    def test_int8_snapshot_round_trips_and_replaces_the_float_file(self):
        self.index._matrix_loader["transcript"]["persisted_after"] = 0
        self.assertTrue(self.index.maybe_save_snapshot(now=0))
        json_path, f32_path, i8_path = self.index._snapshot_paths("transcript")
        self.assertTrue(i8_path.exists())
        self.assertFalse(f32_path.exists())
        self.assertEqual(json.loads(json_path.read_text())["version"], SEMANTICS.SNAPSHOT_INT8_VERSION)
        fresh = SEMANTICS.SemanticIndex(self.store, embedding=self.fixture.embedding)
        self.assertTrue(fresh._restore_snapshot("transcript", self.fixture.embedding.model_id, DIMENSIONS))
        original, restored = self.index._matrices["transcript"], fresh._matrices["transcript"]
        self.assertEqual(sorted(original.key_row), sorted(restored.key_row))
        for key, row in original.key_row.items():
            other = restored.key_row[key]
            a = original.data[row * DIMENSIONS:(row + 1) * DIMENSIONS]
            b = restored.data[other * DIMENSIONS:(other + 1) * DIMENSIONS]
            self.assertLess(max(abs(x - y) for x, y in zip(a, b)), 0.01)


if __name__ == "__main__":
    unittest.main()
