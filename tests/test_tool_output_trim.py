import importlib.machinery
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("omni_knowledge_trim_tests", ROOT / "helpers/omni_knowledge.py")
KNOWLEDGE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(KNOWLEDGE)

LIMIT = KNOWLEDGE.TOOL_OUTPUT_HEAD + KNOWLEDGE.TOOL_OUTPUT_TAIL + KNOWLEDGE.TOOL_OUTPUT_SLACK


def message(identifier, text, kind="output", role="tool"):
    return {"id": identifier, "source_key": "source:fixture", "session_id": "claude:fixture",
            "provider": "claude", "workspace": "/w", "source_path": "/fixture.jsonl", "source_line": 1,
            "part": 0, "role": role, "kind": kind, "text": text, "timestamp": "2026-11-01T00:00:00Z"}


def long_output(label):
    return "HEAD-" + label + " " + "x" * 9000 + " TAIL-" + label


class KeptToolOutputTests(unittest.TestCase):
    def test_long_output_keeps_its_head_and_tail_and_says_what_was_dropped(self):
        text = long_output("a")
        kept = KNOWLEDGE.kept_tool_output(text)
        self.assertTrue(kept.startswith("HEAD-a"))
        self.assertTrue(kept.endswith("TAIL-a"))
        self.assertIn("characters of tool output not kept", kept)
        self.assertLessEqual(len(kept), LIMIT)
        self.assertEqual(KNOWLEDGE.kept_tool_output(kept), kept, "A kept output is never trimmed again")

    def test_short_output_and_other_values_are_unchanged(self):
        self.assertEqual(KNOWLEDGE.kept_tool_output("short"), "short")
        self.assertIsNone(KNOWLEDGE.kept_tool_output(None))


class ToolOutputTrimTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "knowledge.sqlite"
        writer = sqlite3.connect(path)
        self.addCleanup(writer.close)
        writer.execute("CREATE TABLE records(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        writer.executescript((ROOT / "helpers/omni_knowledge_schema.sql").read_text())
        with writer:
            for value in (message("long-output", long_output("p")), message("short-output", "ok"),
                          message("long-answer", long_output("m"), kind="message", role="assistant")):
                writer.execute("INSERT INTO records VALUES(?,?)", ("message:" + value["id"], json.dumps(value)))
        self.store = KNOWLEDGE.KnowledgeStore(roots=[], home=directory.name, read_index=path, semantics=False)
        self.addCleanup(self.store.close)

    def text(self, identifier):
        return self.store.connection.execute("SELECT text FROM messages WHERE id=?", (identifier,)).fetchone()[0]

    def test_new_tool_output_is_stored_short_and_other_messages_whole(self):
        self.store._put_message(message("new-output", long_output("n")))
        self.store._put_message(message("new-answer", long_output("q"), kind="message", role="assistant"))
        self.assertLessEqual(len(self.text("new-output")), LIMIT)
        self.assertEqual(self.text("new-answer"), long_output("q"))
        pending = self.store._pending["message:new-output"][1]
        self.assertLessEqual(len(pending["text"]), LIMIT, "The saved record is short too")

    def test_the_trim_pass_rewrites_long_stored_outputs_once(self):
        self.assertTrue(self.store.step_trim_tool_outputs())
        kept = self.text("long-output")
        self.assertTrue(kept.startswith("HEAD-p") and kept.endswith("TAIL-p"))
        self.assertLessEqual(len(kept), LIMIT)
        self.assertEqual(self.text("short-output"), "ok")
        self.assertEqual(self.text("long-answer"), long_output("m"), "Only tool output is trimmed")
        self.assertEqual(self.store._pending["message:long-output"][1]["text"], kept)
        self.assertNotIn("message:short-output", self.store._pending)
        state = self.store._document_value(KNOWLEDGE.TRIM_DOCUMENT)
        self.assertEqual((state["done"], state["trimmed"]), (True, 1))
        revision = self.store._pending["message:long-output"][0]
        self.assertTrue(self.store.step_trim_tool_outputs(), "A finished pass does nothing more")
        self.assertEqual(self.store._pending["message:long-output"][0], revision)


if __name__ == "__main__":
    unittest.main()
