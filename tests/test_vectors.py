import importlib.util
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
