import json
import re
import sqlite3
import unittest
from pathlib import Path


SCHEMA = (Path(__file__).parents[1] / "helpers/omni_knowledge_schema.sql").read_text()


class SchemaStartupTests(unittest.TestCase):
    """The native app runs the projection schema each time it opens knowledge.sqlite."""

    def backfill_statement(self):
        statements = [line for line in SCHEMA.splitlines() if line.startswith("UPDATE records SET value=value")]
        self.assertEqual(len(statements), 1)
        return statements[0].rstrip(";")

    def open_document_only_database(self):
        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE TABLE records (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        source = {"key": "source:a", "session_id": "claude:a", "provider": "claude", "path": "/a.jsonl"}
        connection.execute("INSERT INTO records VALUES(?,?)", ("source:a", json.dumps(source)))
        return connection

    def test_first_open_backfills_earlier_document_only_records(self):
        connection = self.open_document_only_database()
        connection.executescript(SCHEMA)
        self.assertEqual(connection.execute("SELECT session_id FROM sources").fetchall(), [("claude:a",)])

    def test_later_opens_seek_instead_of_scanning_every_record(self):
        connection = self.open_document_only_database()
        connection.executescript(SCHEMA)
        plan = " ".join(row[-1] for row in connection.execute("EXPLAIN QUERY PLAN " + self.backfill_statement()))
        self.assertNotRegex(plan, r"\bSCAN records\b")
        self.assertRegex(plan, r"SEARCH records")
        changes = connection.total_changes
        connection.executescript(SCHEMA)
        self.assertEqual(connection.total_changes, changes, "A migrated database must not rewrite any record")


if __name__ == "__main__":
    unittest.main()
