import importlib.util
import sqlite3
import unittest
from pathlib import Path


SPEC = importlib.util.spec_from_file_location("omni_query_tests", Path(__file__).parents[1] / "helpers/omni_query.py")
QUERY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(QUERY)

NOW = 1_800_000_000.0
DAY = 86400


class ParseTests(unittest.TestCase):
    def test_the_plan_example_splits_into_filters_phrase_and_negation(self):
        parsed = QUERY.parse('type:decision ws:omniterm since:7d "sqlite vacuum" -codex', now=NOW)
        self.assertEqual(parsed.text, "")
        self.assertEqual(parsed.filters, {"type": "decision", "workspace": "omniterm", "after": NOW - 7 * DAY})
        self.assertEqual(parsed.phrases, ["sqlite vacuum"])
        self.assertEqual(parsed.negations, ["codex"])

    def test_every_key_and_alias(self):
        parsed = QUERY.parse("workspace:/Users/me/x provider:Codex role:User concept:sqlite session:claude:abc "
                             "after:2026-11-01 before:2026-11-10T12:00:00Z archived:true word", now=NOW)
        self.assertEqual(parsed.filters["workspace"], "/Users/me/x")
        self.assertEqual(parsed.filters["provider"], "codex")
        self.assertEqual(parsed.filters["role"], "user")
        self.assertEqual(parsed.filters["concept"], "sqlite")
        self.assertEqual(parsed.filters["session_id"], "claude:abc")
        self.assertEqual(parsed.filters["after"], QUERY.parse_time("2026-11-01"))
        self.assertEqual(parsed.filters["before"], QUERY.parse_time("2026-11-10T12:00:00+00:00"))
        self.assertIs(parsed.filters["archived"], True)
        self.assertEqual(parsed.text, "word")

    def test_since_units(self):
        self.assertEqual(QUERY.parse("since:3h", now=NOW).filters["after"], NOW - 3 * 3600)
        self.assertEqual(QUERY.parse("since:2w", now=NOW).filters["after"], NOW - 14 * DAY)

    def test_unknown_keys_and_unreadable_values_stay_text(self):
        parsed = QUERY.parse("color:red provider:gemini after:yesterday since:soon https://x.y/z", now=NOW)
        self.assertEqual(parsed.filters, {})
        self.assertEqual(parsed.text, "color:red provider:gemini after:yesterday since:soon https://x.y/z")

    def test_negation_of_words_and_phrases(self):
        parsed = QUERY.parse('vacuum -"full scan" -wal', now=NOW)
        self.assertEqual(parsed.text, "vacuum")
        self.assertEqual(parsed.negations, ["full scan", "wal"])

    def test_quoted_key_value_and_unterminated_phrase(self):
        parsed = QUERY.parse('ws:"my project" "open phrase', now=NOW)
        self.assertEqual(parsed.filters["workspace"], "my project")
        self.assertEqual(parsed.phrases, ["open phrase"])

    def test_explicit_arguments_win(self):
        merged = QUERY.merge({"provider": "claude", "workspace": None, "limit": 5},
                             QUERY.parse("provider:codex ws:omni hello", now=NOW))
        self.assertEqual(merged["provider"], "claude")
        self.assertEqual(merged["workspace"], "omni")
        self.assertEqual(merged["query"], "hello")
        self.assertEqual(merged["limit"], 5)

    def test_empty_and_none(self):
        self.assertEqual(QUERY.parse(None), QUERY.Query("", {}, [], []))


class TimeTests(unittest.TestCase):
    def test_stored_timestamps_in_both_shapes(self):
        self.assertEqual(QUERY.timestamp_epoch("1793485994.484"), 1793485994.484)
        self.assertAlmostEqual(QUERY.timestamp_epoch("2026-11-11T14:20:35.072Z"),
                               QUERY.parse_time("2026-11-11T14:20:35") + 0.072, places=3)
        self.assertIsNone(QUERY.timestamp_epoch(None))
        self.assertIsNone(QUERY.timestamp_epoch("garbage"))


class FtsTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("CREATE VIRTUAL TABLE t USING fts5(body)")
        for body in ("sqlite vacuum ran on the full database", "sqlite full vacuum codex",
                     "vacuum of sqlite pages", 'quote " inside sqlite vacuum'):
            self.db.execute("INSERT INTO t VALUES(?)", (body,))

    def match(self, expression):
        return sorted(row[0] for row in self.db.execute("SELECT body FROM t WHERE t MATCH ?", (expression,)))

    def test_plain_words_are_anded_as_before(self):
        self.assertEqual(QUERY.fts_expression("sqlite vacuum"), '"sqlite" AND "vacuum"')

    def test_long_queries_drop_stopwords_and_short_ones_keep_them(self):
        self.assertEqual(QUERY.fts_expression("how did we fix the stalled vacuum run"),
                         '"fix" AND "stalled" AND "vacuum" AND "run"')
        self.assertEqual(QUERY.fts_expression("where are vacuum notes written"),
                         '"where" AND "are" AND "vacuum" AND "notes" AND "written"')
        self.assertEqual(QUERY.fts_expression("the"), '"the"', "a stopword alone is still searched")
        self.assertEqual(QUERY.fts_expression("sqlite of the vacuum", any_term=True), '"sqlite" OR "vacuum"')
        self.assertIsNone(QUERY.fts_expression("of the", any_term=True))
        self.assertEqual(QUERY.fts_expression("", phrases=["on the full"]), '"on the full"', "phrases keep every word")

    def test_expression_words_keep_phrases_and_drop_negations(self):
        expression = QUERY.fts_expression("vacuum", phrases=["sqlite pages"], negations=["codex"])
        self.assertEqual(QUERY.expression_words(expression), ["vacuum", "sqlite pages"])
        self.assertEqual(QUERY.mark("we ran sqlite  pages vacuum", QUERY.expression_words(expression)),
                         "we ran «sqlite pages» «vacuum»")

    def test_phrase_and_not(self):
        expression = QUERY.fts_expression("", ["sqlite vacuum"], ["codex"])
        self.assertEqual(self.match(expression), ['quote " inside sqlite vacuum', "sqlite vacuum ran on the full database"])
        self.assertEqual(self.match(QUERY.fts_expression("vacuum", [], ["full database", "codex"])),
                         ['quote " inside sqlite vacuum', "vacuum of sqlite pages"])

    def test_nothing_positive_is_none(self):
        self.assertIsNone(QUERY.fts_expression("", [], ["codex"]))
        self.assertIsNone(QUERY.fts_expression("!!", []))

    def test_quotes_are_escaped_and_terms_bounded(self):
        expression = QUERY.fts_expression(" ".join("w%d" % index for index in range(40)))
        self.assertEqual(expression.count(" AND "), QUERY.MAX_TERMS - 1)
        self.db.execute("SELECT 1 FROM t WHERE t MATCH ?", (QUERY.fts_expression('a"b'),)).fetchall()


class SnippetTests(unittest.TestCase):
    def test_marks_words_case_insensitively(self):
        self.assertEqual(QUERY.mark("Run SQLite vacuum now", ["sqlite", "vacuum"]), "Run «SQLite» «vacuum» now")

    def test_window_centres_on_the_first_hit(self):
        text = "filler " * 100 + "needle here" + " tail" * 100
        snippet = QUERY.mark(text, ["needle"], width=80)
        self.assertTrue(snippet.startswith("…"))
        self.assertTrue(snippet.endswith("…"))
        self.assertIn("«needle»", snippet)

    def test_excluded(self):
        self.assertTrue(QUERY.excluded("Ran a FULL scan today", ["full scan"]))
        self.assertFalse(QUERY.excluded("fullscan", ["full scan"]))


if __name__ == "__main__":
    unittest.main()
