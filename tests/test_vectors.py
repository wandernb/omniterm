import importlib.util
import json
import math
import random
import unittest
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "omni_vectors_for_tests", Path(__file__).resolve().parents[1] / "helpers/omni_vectors.py")
VECTORS = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(VECTORS)


def unit(vector):
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector]


def brute_force(rows, query, limit, accept=lambda record, meta: True):
    """The reference ranking: best chunk per accepted record, by exact cosine."""
    query = unit(query)
    best = {}
    for key, record, chunk, vector, meta in rows:
        if not accept(record, meta):
            continue
        score = sum(a * b for a, b in zip(unit(vector), query))
        if record not in best or score > best[record][0]:
            best[record] = (score, record, chunk, key)
    return sorted(best.values(), reverse=True)[:limit]


class VectorMatrixTests(unittest.TestCase):
    def setUp(self):
        generator = random.Random(7)
        self.dimensions = 16
        self.rows = []
        for record in range(300):
            for chunk in range(1 + record % 3):
                vector = [generator.uniform(-1, 1) for _ in range(self.dimensions)]
                meta = {"workspace": "/w%d" % (record % 5)}
                self.rows.append(("vector:transcript:%03d:%d" % (record, chunk), "r%03d" % record, chunk, vector, meta))
        self.queries = [[generator.uniform(-1, 1) for _ in range(self.dimensions)] for _ in range(10)]

    def matrix(self, accelerate):
        matrix = VECTORS.VectorMatrix(self.dimensions, accelerate=accelerate)
        for key, record, chunk, vector, meta in self.rows:
            self.assertTrue(matrix.upsert(key, record, chunk, "hash-" + record, vector, meta))
        return matrix

    def assertSameRanking(self, actual, expected):
        self.assertEqual([item[1:] for item in actual], [item[1:] for item in expected])
        for (score, *_), (reference, *_) in zip(actual, expected):
            self.assertAlmostEqual(score, reference, places=5)

    def test_accelerate_and_fallback_rank_exactly_like_brute_force(self):
        self.assertIsNotNone(VECTORS.blas(), "Accelerate loads from the system Python")
        for accelerate in (True, False):
            matrix = self.matrix(accelerate)
            for query in self.queries:
                self.assertSameRanking(matrix.top(query, 10), brute_force(self.rows, query, 10))

    def test_a_narrow_filter_still_fills_the_result_like_brute_force(self):
        matrix = self.matrix(True)
        rare = lambda record, meta: meta["workspace"] == "/w3" and record.endswith("8")  # noqa: E731
        for query in self.queries:
            expected = brute_force(self.rows, query, 6, rare)
            self.assertEqual(len(expected), 6)
            self.assertSameRanking(matrix.top(query, 6, rare), expected)

    def test_snapshot_rows_stream_in_bounded_steps_and_decode_like_the_whole_matrix(self):
        import io
        from unittest import mock
        matrix = self.matrix(True)
        _, data = matrix.to_snapshot()
        rows = len(data) // self.dimensions
        whole = VECTORS.dequantize_rows(*VECTORS.quantize_rows(data, self.dimensions), self.dimensions)
        with mock.patch.object(VECTORS, "CODEC_ROWS", 7):
            handle = io.BytesIO()
            VECTORS.write_int8_rows(handle, data, self.dimensions)
            self.assertEqual(len(handle.getvalue()), rows * (4 + self.dimensions))
            handle.seek(0)
            streamed = VECTORS.read_int8_rows(handle, rows, self.dimensions)
            self.assertEqual(streamed.tobytes(), whole.tobytes())
            handle.seek(0)
            with self.assertRaises(ValueError):
                VECTORS.read_int8_rows(handle, rows + 1, self.dimensions)
            floats = io.BytesIO(data.tobytes())
            self.assertEqual(VECTORS.read_float32_rows(floats).tobytes(), data.tobytes())
            metadata, _ = matrix.to_snapshot()
            metadata["model"] = "fixture"
            written = io.BytesIO()
            VECTORS.write_json(written, metadata)
            self.assertEqual(written.getvalue(), json.dumps(metadata).encode("utf-8"))

    def test_ranking_widens_past_the_first_candidates_like_brute_force(self):
        matrix = self.matrix(True)
        # Accepts few records, all far down most rankings: the first 64 candidates hold none.
        sparse = lambda record, meta: record.endswith("7") and meta["workspace"] == "/w2"  # noqa: E731
        for query in self.queries:
            expected = brute_force(self.rows, query, 5, sparse)
            self.assertSameRanking(matrix.top(query, 5, sparse), expected)
        self.assertEqual(matrix.top(self.queries[0], 3, lambda record, meta: False), [])

    def test_int8_resident_matrix_ranks_like_the_float_matrix_and_saves_exactly(self):
        import io
        # Vectors as an int8 index stores them: a quantized matrix holds them exactly.
        stored = [(key, record, chunk, VECTORS.decode_blob(VECTORS.encode_int8(vector), self.dimensions), meta)
                  for key, record, chunk, vector, meta in self.rows]
        exact = VECTORS.VectorMatrix(self.dimensions)
        compact = VECTORS.VectorMatrix(self.dimensions, quantized=True)
        self.assertTrue(compact.quantized)
        for key, record, chunk, vector, meta in stored:
            exact.upsert(key, record, chunk, "h", vector, meta)
            compact.upsert(key, record, chunk, "h", vector, meta)
        self.assertEqual(len(compact.qdata), len(exact.data))  # a quarter of the bytes
        rare = lambda record, meta: meta["workspace"] == "/w3"  # noqa: E731
        for query in self.queries:
            for accept in (None, rare):
                expected, actual = exact.top(query, 8, accept), compact.top(query, 8, accept)
                self.assertEqual([item[1] for item in actual], [item[1] for item in expected])
                for (score, *_), (reference, *_) in zip(actual, expected):
                    self.assertAlmostEqual(score, reference, places=5)
        for key, *_ in stored[:40]:
            compact.delete(key)
            exact.delete(key)
        compact.compact()
        self.assertSameRanking(compact.top(self.queries[0], 8), exact.top(self.queries[0], 8))
        metadata, _ = compact.to_snapshot()
        handle = io.BytesIO()
        compact.write_rows(handle, int8=True)
        handle.seek(0)
        restored = VECTORS.VectorMatrix.from_int8_snapshot(metadata, handle)
        self.assertTrue(restored.quantized)
        self.assertEqual((bytes(restored.qdata), restored.scales.tobytes()), (bytes(compact.qdata), compact.scales.tobytes()))
        self.assertEqual(list(restored.row(3)), list(compact.row(3)))

    def test_replaced_and_deleted_rows_are_honoured_and_compaction_keeps_rankings(self):
        matrix = self.matrix(True)
        query = self.queries[0]
        winner = matrix.top(query, 1)[0]
        self.assertTrue(matrix.delete(winner[3]))
        self.assertFalse(matrix.delete(winner[3]), "a second delete is a no-op")
        remaining = [row for row in self.rows if row[0] != winner[3]]
        self.assertSameRanking(matrix.top(query, 10), brute_force(remaining, query, 10))
        # Point another row straight at the query: it must now rank first.
        key, record, chunk, _, meta = remaining[-1]
        matrix.upsert(key, record, chunk, "hash-new", query, meta)
        self.assertEqual(matrix.top(query, 1)[0][1:], (record, chunk, key))
        self.assertEqual(matrix.content_hash(record), "hash-new")
        for row in remaining[:len(remaining) // 2]:
            matrix.delete(row[0])
        self.assertLess(len(matrix.row_key), len(self.rows), "deleted rows were compacted away")
        survivors = [row for row in remaining[len(remaining) // 2:] if row[0] != key] + [(key, record, chunk, query, meta)]
        self.assertSameRanking(matrix.top(self.queries[1], 10), brute_force(survivors, self.queries[1], 10))

    def test_pairwise_similarities_match_with_and_without_accelerate(self):
        vectors = [unit(row[3]) for row in self.rows[:6]] + [None]
        fast = VECTORS.pairwise(vectors, self.dimensions)
        slow = VECTORS.pairwise(vectors, self.dimensions, accelerate=False)
        for i in range(6):
            self.assertAlmostEqual(fast[i][i], 1.0, places=5)
            for j in range(6):
                self.assertAlmostEqual(fast[i][j], slow[i][j], places=5)
        self.assertEqual(fast[6], [0.0] * 7, "a result without a vector is never redundant")

    def test_malformed_vectors_are_rejected(self):
        matrix = VECTORS.VectorMatrix(3)
        self.assertFalse(matrix.upsert("k", "r", 0, "h", [1.0, 2.0], {}))
        self.assertFalse(matrix.upsert("k", "r", 0, "h", [0.0, 0.0, 0.0], {}))
        self.assertFalse(matrix.upsert("k", "r", 0, "h", [float("nan"), 1.0, 0.0], {}))
        self.assertEqual((len(matrix), matrix.top([1.0, 0.0, 0.0], 5)), (0, []))


if __name__ == "__main__":
    unittest.main()
