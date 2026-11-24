import importlib.util
import math
import sys
import unittest
from pathlib import Path
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    "omni_embeddings", Path(__file__).parents[1] / "helpers/omni_embeddings.py")
EMBEDDINGS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EMBEDDINGS)


class WordBackend:
    algorithm = "word_mean_l2_v1"
    language = "en"
    revision = 7
    dimensions = 3

    def __init__(self):
        self.calls = []

    def vector(self, word):
        self.calls.append(word)
        return {"database": [1, 0, 0], "records": [0.8, 0.2, 0],
                "bread": [0, 0, 1], "fresh": [0, 0.2, 1]}.get(word)


class EmbeddingTests(unittest.TestCase):
    def test_initialization_is_lazy_and_model_identity_is_explicit(self):
        backend = WordBackend()
        factory = mock.Mock(return_value=backend)
        embedding = EMBEDDINGS.LocalEmbedding(backend_factory=factory)
        factory.assert_not_called()
        self.assertEqual(embedding.dimensions, 3)
        self.assertEqual(embedding.model_id, "apple_naturallanguage:word_mean_l2_v1:en:r7:d3")
        self.assertEqual(embedding.status()["algorithm"], "word_mean_l2_v1")
        factory.assert_called_once_with()

    def test_word_vectors_are_combined_and_normalized(self):
        embedding = EMBEDDINGS.LocalEmbedding(backend_factory=WordBackend)
        database = embedding.embed("Database records")
        bread = embedding.embed("fresh bread")
        self.assertAlmostEqual(sum(value * value for value in database), 1)
        self.assertGreater(database[0], 0.95)
        self.assertGreater(bread[2], 0.95)
        self.assertTrue(all(math.isfinite(value) for value in database))

    def test_unknown_words_are_not_fake_vectors(self):
        embedding = EMBEDDINGS.LocalEmbedding(backend_factory=WordBackend)
        for text in ("zzzzzzzzz", "123456", "", "\ud800"):
            with self.subTest(text=repr(text)), self.assertRaises(EMBEDDINGS.EmbeddingUnavailable):
                embedding.embed(text)

    def test_unavailable_model_has_explicit_error_and_never_uses_hash_vectors(self):
        def unavailable():
            raise EMBEDDINGS.EmbeddingUnavailable("Model missing")
        embedding = EMBEDDINGS.LocalEmbedding(backend_factory=unavailable)
        self.assertFalse(embedding.available)
        self.assertIsNone(embedding.model_id)
        self.assertEqual(embedding.status()["error"], "Model missing")
        with self.assertRaises(EMBEDDINGS.EmbeddingUnavailable):
            embedding.embed("database records")

    def test_word_cache_is_bounded_and_returned_vectors_are_independent(self):
        backend = WordBackend()
        embedding = EMBEDDINGS.LocalEmbedding(backend_factory=lambda: backend, cache_size=2)
        first = embedding.embed("database")
        first[0] = -100
        self.assertEqual(embedding.embed("database"), [1, 0, 0])
        embedding.embed("database records")
        self.assertEqual(backend.calls.count("database"), 1)
        embedding.embed("fresh bread")
        self.assertLessEqual(len(embedding._word_cache), 2)
        self.assertLessEqual(len(embedding._document_cache), 2)

    def test_invalid_backend_vector_fails_instead_of_poisoning_cosine_search(self):
        backend = WordBackend()
        for vector in ([float("nan"), 0, 0], [0, 0, 0], [1, 0]):
            with self.subTest(vector=vector):
                backend.vector = lambda word: vector
                embedding = EMBEDDINGS.LocalEmbedding(backend_factory=lambda: backend)
                with self.assertRaises(EMBEDDINGS.EmbeddingUnavailable):
                    embedding.embed("database")

    def test_sentence_model_encodes_the_sentence_as_a_single_input(self):
        backend = WordBackend()
        backend.algorithm = "sentence_l2_v1"
        backend.vector = mock.Mock(return_value=[2, 1, 0])
        embedding = EMBEDDINGS.LocalEmbedding(backend_factory=lambda: backend)
        embedding.embed("database records")
        backend.vector.assert_called_once_with("database records")
        self.assertEqual(embedding.status()["algorithm"], "sentence_l2_v1")

    def test_word_inference_budget_is_bounded(self):
        backend = WordBackend()
        embedding = EMBEDDINGS.LocalEmbedding(backend_factory=lambda: backend)
        embedding.embed("database " * 1000 + "bread")
        self.assertNotIn("bread", backend.calls)
        self.assertEqual(backend.calls, ["database"])

    def test_revision_change_produces_a_different_model_identity(self):
        first = WordBackend()
        second = WordBackend()
        second.revision += 1
        self.assertNotEqual(EMBEDDINGS.LocalEmbedding(lambda: first).model_id,
                            EMBEDDINGS.LocalEmbedding(lambda: second).model_id)


class ChunkTests(unittest.TestCase):
    def test_chunks_preserve_overlap_and_end_of_input(self):
        text = " ".join(f"token{number}" for number in range(30))
        chunks = EMBEDDINGS.chunk_text(text, max_tokens=10, overlap=2, max_characters=1600)
        self.assertEqual(chunks[0].split()[-2:], chunks[1].split()[:2])
        self.assertEqual(chunks[-1].split()[-1], "token29")
        self.assertTrue(all(len(chunk.split()) <= 10 for chunk in chunks))

    def test_long_unbroken_output_and_empty_text_are_bounded(self):
        text = "x" * 7000
        chunks = EMBEDDINGS.chunk_text(text)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(len(chunk) <= 1600 for chunk in chunks))
        self.assertEqual(EMBEDDINGS.chunk_text(" \n\t"), [])

    def test_invalid_bounds_cannot_make_a_nonadvancing_loop(self):
        for arguments in ({"max_tokens": 0}, {"max_tokens": 5, "overlap": 5},
                          {"overlap": -1}, {"max_characters": 0}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                EMBEDDINGS.chunk_text("database records", **arguments)


@unittest.skipUnless(sys.platform == "darwin", "macOS local NaturalLanguage inference")
class RealLocalEmbeddingTests(unittest.TestCase):
    def test_local_model_ranks_database_paraphrase_above_unrelated_bread(self):
        embedding = EMBEDDINGS.LocalEmbedding()
        if not embedding.available:
            self.skipTest(embedding.status()["error"])
        query, related, unrelated = [embedding.embed(text) for text in (
            "The database connection was interrupted",
            "A lost link prevented access to stored records",
            "Fresh bread tastes delicious")]
        similarity = lambda vector: sum(a * b for a, b in zip(query, vector))
        self.assertGreater(similarity(related), similarity(unrelated) + 0.1)
        self.assertEqual(len(query), embedding.dimensions)


if __name__ == "__main__":
    unittest.main()
