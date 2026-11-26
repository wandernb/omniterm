import importlib.util
import json
import os
import tempfile
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock


_SPEC = importlib.util.spec_from_file_location(
    "omni_knowledge_tests", Path(__file__).parents[1] / "helpers/omni_knowledge.py")
KNOWLEDGE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(KNOWLEDGE)
# These fixtures exercise the stream parser with records a little over 64 KB;
# test_inline_records covers the production inline bound.
KNOWLEDGE.INLINE_RECORD_BYTES = KNOWLEDGE.STREAM_FEED_BYTES



def exact(matches):
    """Matches of every query word: any-word matches are added after them (partial_match)."""
    return [match for match in matches if not match.get("partial_match")]

class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        self.claude = self.home / ".claude/projects"
        self.codex = self.home / ".codex/sessions"
        self.archive = self.home / ".codex/archived_sessions"
        self.roots = [("claude", self.claude), ("codex", self.codex), ("codex", self.archive)]
        self.store = KNOWLEDGE.KnowledgeStore(roots=self.roots, home=self.home, semantics=False)
        self.addCleanup(self.store.close)

    def write(self, path, events):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(event) + "\n" for event in events))
        return path

    def claude_message(self, text, role="user", **extra):
        return {"type": role, "timestamp": "2026-11-06T09:00:00Z", "cwd": "/workspace",
                "message": {"role": role, "content": [{"type": "text", "text": text}]}, **extra}

    def codex_events(self, identifier="same-id", text="Codex output"):
        return [{"type": "session_meta", "payload": {"id": identifier, "cwd": "/workspace"}},
                {"type": "response_item", "timestamp": "2026-11-06T09:01:00Z", "payload": {
                    "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}}]

    def assertKeptOutput(self, parts, output):
        """Long tool output keeps each stored part's head and tail (TOOL_OUTPUT_HEAD/TAIL):
        the stored text starts and ends as the output did, and no part is longer than kept."""
        self.assertTrue(parts)
        limit = KNOWLEDGE.TOOL_OUTPUT_HEAD + KNOWLEDGE.TOOL_OUTPUT_TAIL + KNOWLEDGE.TOOL_OUTPUT_SLACK
        self.assertTrue(all(len(part) <= limit for part in parts), max(len(part) for part in parts))
        joined = "".join(parts)
        self.assertTrue(joined.startswith(output[:KNOWLEDGE.TOOL_OUTPUT_HEAD // 2]))
        self.assertTrue(joined.endswith(output[-(KNOWLEDGE.TOOL_OUTPUT_TAIL // 2):]))
        self.assertLess(len(joined), len(output))

    def complete_scan(self, **options):
        for _ in range(100):
            result = self.store.scan(**options)
            if result["scan_complete"] and not result["backlog_files"]:
                return
        self.fail("Index scan did not converge")

    def request(self, op, **arguments):
        value = self.store.request({"op": op, **arguments})
        self.assertTrue(value["ok"], value)
        return value

    def test_scan_stops_at_its_time_bound_but_always_makes_progress(self):
        for index in range(3):
            self.write(self.claude / ("workspace/s%d.jsonl" % index), [self.claude_message("Message %d" % index)])
        with mock.patch.object(KNOWLEDGE, "SCAN_JSONL_SECONDS", 0):
            first = self.store.scan()
            self.assertEqual(first["processed_files"], 1)
            for _ in range(20):
                if self.store.scan()["scan_complete"]:
                    break
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 3)

    def test_catalog_qualifies_colliding_ids_and_pages_without_losing_sessions(self):
        self.write(self.claude / "workspace/same-id.jsonl", [self.claude_message("Claude question")])
        self.write(self.codex / "2026/rollout-same-id.jsonl", self.codex_events())
        self.write(self.archive / "rollout-old.jsonl", self.codex_events("archived-id"))
        self.complete_scan(max_files=1)
        identifiers, cursor = [], None
        while True:
            arguments = {"limit": 1}
            if cursor:
                arguments["cursor"] = cursor
            result = self.request("list_sessions", **arguments)
            identifiers += [item["session_id"] for item in result["sessions"]]
            cursor = result["next_cursor"]
            if not cursor:
                break
        self.assertEqual(identifiers, ["claude:same-id", "codex:archived-id", "codex:same-id"])
        archived = self.request("list_sessions", state="archived")["sessions"]
        self.assertEqual([row["session_id"] for row in archived], ["codex:archived-id"])
        self.assertEqual(self.request("list_sessions", provider="claude")["sessions"][0]["state"], "unknown")
        recent, offset = [], 0
        while offset is not None:
            result = self.request("list_sessions", order="recent", limit=1, offset=offset)
            recent += result["sessions"]
            offset = result["next_offset"]
        self.assertEqual(sorted(row["session_id"] for row in recent), identifiers)
        updated = [row["updated"] or 0 for row in recent]
        self.assertEqual(updated, sorted(updated, reverse=True))

    def test_request_echoes_are_neither_indexed_nor_shown_and_tail_starts_at_the_end(self):
        tool = {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]}}
        echo = {"type": "api-request-blob", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]}}
        events = [self.claude_message("Question"), echo, {"type": "data-flow-records"}, tool,
                  self.claude_message("Answer", "assistant")]
        self.write(self.claude / "workspace/echoed.jsonl", events)
        self.complete_scan()
        result = self.request("get_session", session_id="claude:echoed")
        self.assertEqual([(m["role"], m["kind"]) for m in result["messages"]],
                         [("user", "message"), ("assistant", "tool"), ("assistant", "message")])
        tail = self.request("get_session", session_id="claude:echoed", tail=True, limit=2)
        self.assertEqual(tail["total"], 3)
        self.assertEqual(tail["offset"], 1)
        self.assertEqual([m["text"] for m in tail["messages"]][-1], "Answer")

    def test_echoes_indexed_by_older_builds_are_hidden_when_read(self):
        self.write(self.claude / "workspace/old.jsonl", [self.claude_message("Question")])
        self.complete_scan()
        source = self.store.connection.execute("SELECT key FROM sources WHERE session_id='claude:old'").fetchone()["key"]
        path = str((self.claude / "workspace/old.jsonl").resolve())
        for line, text in ((5, "Bash\nls"), (9, "Bash\nls\n"), (40, "Bash\nls")):
            self.store._put_message({"id": "m%d" % line, "source_key": source, "session_id": "claude:old", "provider": "claude",
                                     "workspace": "/workspace", "source_path": path, "source_line": line, "part": 0,
                                     "role": "assistant", "kind": "tool", "text": text, "timestamp": None})
        shown = self.request("get_session", session_id="claude:old")["messages"]
        self.assertEqual([m["message_id"] for m in shown if m["kind"] == "tool"], ["m9", "m40"],
                         "The echo a few lines before its record is hidden; a later repeat is kept")
        focused = self.request("get_session", session_id="claude:old", message_id="m5")
        self.assertEqual(focused["focus_message_id"], "m9", "A match on an echo opens at the real record")

    def test_rename_writes_each_providers_own_record_and_lists_the_new_title(self):
        path = self.write(self.claude / "workspace/named.jsonl", [self.claude_message("First prompt")])
        self.write(self.codex / "2026/rollout-named.jsonl", self.codex_events("thread-1"))
        self.complete_scan()
        result = self.request("rename_session", session_id="claude:named", title="  Release   prep ")
        self.assertEqual((result["title"], result["native"]), ("Release prep", "claude_custom_title"))
        last = json.loads(path.read_text().splitlines()[-1])
        self.assertEqual(last, {"type": "custom-title", "customTitle": "Release prep", "sessionId": "named"})
        listed = {row["session_id"]: row["title"] for row in self.request("list_sessions", order="recent")["sessions"]}
        self.assertEqual(listed["claude:named"], "Release prep")
        self.store.messenger.set_codex_thread_name = lambda thread, name: False
        codex = self.request("rename_session", session_id="codex:thread-1", title="Codex work")
        self.assertEqual(codex["native"], "codex_session_index")
        index = json.loads((self.home / ".codex/session_index.jsonl").read_text().splitlines()[-1])
        self.assertEqual((index["id"], index["thread_name"]), ("thread-1", "Codex work"))
        self.assertEqual(self.request("get_session", session_id="codex:thread-1")["session"]["title"], "Codex work")
        with self.assertRaises(AssertionError):
            self.request("rename_session", session_id="claude:named", title="bad\x07title")
        self.store.scan()
        self.assertIn("Release prep", path.read_text())

    def test_sessions_go_by_claudes_own_title_before_the_first_prompt(self):
        path = self.write(self.claude / "workspace/titled.jsonl", [self.claude_message("<command-name>/plan</command-name> long prompt")])
        self.complete_scan()
        with path.open("a") as handle:
            handle.write(json.dumps({"type": "ai-title", "aiTitle": "Sidebar modes plan", "sessionId": "titled"}) + "\n")
        listed = {row["session_id"]: row["title"] for row in self.request("list_sessions", order="recent")["sessions"]}
        self.assertEqual(listed["claude:titled"], "Sidebar modes plan")
        with path.open("a") as handle:
            handle.write(json.dumps({"type": "custom-title", "customTitle": "Modes", "sessionId": "titled"}) + "\n")
        self.assertEqual(self.request("get_session", session_id="claude:titled")["session"]["title"], "Modes",
                         "A /rename wins over the AI title")

    def test_system_prompt_comes_from_the_providers_own_record(self):
        self.write(self.claude / "workspace/prompted.jsonl", [
            {"type": "api-request-shape", "shape": {"system": [{"type": "text", "text": "You are Claude Code."},
                                                              {"type": "text", "text": "Be brief."}],
                                                   "tools": [{"name": "Bash"}, {"name": "Read"}]}},
            self.claude_message("Hello")])
        self.write(self.codex / "2026/rollout-prompted.jsonl", [
            {"type": "session_meta", "payload": {"id": "codex-prompted", "cwd": "/w", "base_instructions": {"text": "You are Codex."}}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Hi"}]}}])
        self.complete_scan()
        claude = self.request("get_system_prompt", session_id="claude:prompted")
        self.assertEqual((claude["system_prompt"], claude["tools"]), ("You are Claude Code.\n\nBe brief.", ["Bash", "Read"]))
        self.assertEqual(self.request("get_system_prompt", session_id="codex:codex-prompted")["system_prompt"], "You are Codex.")

    def test_transcript_has_ordered_source_references_and_kept_tool_output(self):
        output = "extraordinarily-specific-output " + "z" * 18000
        events = [self.claude_message("Question"), self.claude_message("Answer", "assistant"), {
            "type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": output}]}}]
        path = self.write(self.claude / "workspace/session.jsonl", events)
        self.complete_scan()
        result = self.request("get_session", session_id="claude:session", limit=2)
        self.assertEqual([message["text"] for message in result["messages"]], ["Question", "Answer"])
        later = self.request("get_session", session_id="claude:session", offset=result["next_offset"])
        self.assertKeptOutput([message["text"] for message in later["messages"]], output)
        self.assertTrue(all(message["source_path"] == str(path.resolve()) and message["source_line"] == 3
                            and message["role"] == "tool" for message in later["messages"]))
        matches = self.request("search_transcripts", query="extraordinarily-specific-output")["matches"]
        self.assertEqual(matches[0]["kind"], "output")
        around = self.request("get_session", session_id="claude:session", message_id=matches[0]["message_id"])
        self.assertEqual(around["offset"], 0)
        self.assertIn(matches[0]["message_id"], [message["message_id"] for message in around["messages"]])

    def test_incremental_append_partial_line_and_restart_preserve_exact_records(self):
        path = self.write(self.claude / "workspace/session.jsonl", [self.claude_message("First")])
        self.complete_scan()
        partial = json.dumps(self.claude_message("Second"))
        with path.open("a") as handle:
            handle.write(partial[:40])
        self.store.scan()
        self.assertEqual(len(self.request("get_session", session_id="claude:session")["messages"]), 1)
        restored = KNOWLEDGE.KnowledgeStore(roots=self.roots, home=self.home, semantics=False)
        self.addCleanup(restored.close)
        records = self.store.snapshot()
        midpoint = len(records) // 2
        restored.restore(records[:midpoint])
        restored.restore(records[midpoint:], final=True)
        self.assertEqual(restored.request({"op": "get_session", "session_id": "claude:session"})["messages"][0]["text"], "First")
        with path.open("a") as handle:
            handle.write(partial[40:] + "\n")
        for _ in range(3):
            restored.scan()
        messages = restored.request({"op": "get_session", "session_id": "claude:session"})["messages"]
        self.assertEqual([message["text"] for message in messages], ["First", "Second"])

    def test_replacement_truncation_and_deleted_sources_remove_stale_search_hits(self):
        path = self.write(self.codex / "rollout-id.jsonl", self.codex_events("id", "Original zebra"))
        self.complete_scan()
        replacement = path.with_suffix(".new")
        self.write(replacement, self.codex_events("id", "Replacement penguin"))
        replacement.replace(path)
        self.complete_scan()
        self.assertEqual(exact(self.request("search_transcripts", query="zebra")["matches"]), [])
        self.assertEqual(len(self.request("search_transcripts", query="penguin")["matches"]), 1)
        path.write_text("")
        self.complete_scan()
        self.assertEqual(exact(self.request("search_transcripts", query="penguin")["matches"]), [])
        path.unlink()
        self.complete_scan()
        self.assertEqual(self.request("list_sessions")["sessions"], [])

    def test_subagent_history_and_malformed_records_do_not_abort_catalog(self):
        self.write(self.claude / "workspace/parent/subagents/agent-child.jsonl", [self.claude_message("Child work")])
        path = self.write(self.claude / "workspace/parent.jsonl", [self.claude_message("Parent work")])
        with path.open("a") as handle:
            handle.write("not json\n[]\n")
            handle.write(json.dumps(self.claude_message("After malformed")) + "\n")
        self.complete_scan()
        sessions = self.request("list_sessions")["sessions"]
        self.assertEqual([row["session_id"] for row in sessions], ["claude:parent", "claude:parent/agent-child"])
        self.assertEqual(sessions[1]["parent_session_id"], "claude:parent")
        self.assertEqual(self.request("knowledge_status")["status"]["malformed_records"], 2)
        self.assertEqual(len(self.request("search_transcripts", query="After malformed")["matches"]), 1)

    def test_source_symlink_cannot_index_an_unrelated_file(self):
        secret = self.write(self.home / "unrelated.jsonl", [self.claude_message("outside content")])
        self.claude.mkdir(parents=True)
        (self.claude / "linked.jsonl").symlink_to(secret)
        self.complete_scan()
        self.assertEqual(self.request("list_sessions")["sessions"], [])
        rejected = self.store.request({"op": "get_session", "session_id": str(secret)})
        self.assertFalse(rejected["ok"])

    def test_large_single_record_is_chunked_instead_of_starving_bounded_scan(self):
        output = "q" * 300000
        self.write(self.codex / "rollout-large.jsonl", self.codex_events("large", output))
        self.complete_scan(byte_budget=64000)
        messages = self.request("get_session", session_id="codex:large", limit=200)["messages"]
        self.assertEqual("".join(message["text"] for message in messages), output)

    def test_native_write_acknowledgement_does_not_drop_later_batches(self):
        self.write(self.codex / "rollout-large.jsonl", self.codex_events("large", "q" * 900000))
        self.complete_scan()
        native = {}
        batches = 0
        while self.store._pending:
            response = self.request("drain_writes")
            self.assertGreater(len(response["_writes"]), 0)
            self.assertLess(len(KNOWLEDGE.json_text(response["_writes"]).encode()), KNOWLEDGE.MAX_BATCH + 1000)
            for write in response["_writes"]:
                if write["value"] is None:
                    native.pop(write["key"], None)
                else:
                    native[write["key"]] = write["value"]
            self.request("ack_writes", revision=response["_revision"])
            batches += 1
        self.assertGreater(batches, 1)
        restored = KNOWLEDGE.KnowledgeStore(roots=self.roots, home=self.home, semantics=False)
        self.addCleanup(restored.close)
        restored.restore([{"key": key, "value": value} for key, value in native.items()], final=True)
        self.assertEqual(restored.knowledge_status()["messages"], self.store.knowledge_status()["messages"])

    def test_pending_bytes_grow_with_indexing_and_return_to_zero_on_acknowledgement(self):
        self.write(self.codex / "rollout-pending.jsonl", self.codex_events("pending", "p" * 200000))
        self.complete_scan()
        status = self.request("knowledge_status")["status"]
        self.assertGreater(status["pending_bytes"], 200000)
        while self.store._pending:
            self.request("ack_writes", revision=self.request("drain_writes")["_revision"])
        self.assertEqual(self.request("knowledge_status")["status"]["pending_bytes"], 0)

    def test_delivery_only_scan_reads_no_transcript(self):
        self.write(self.codex / "rollout-later.jsonl", self.codex_events("later", "Not read under pressure"))
        delivered = self.request("scan", delivery_only=True)
        self.assertTrue(delivered["delivery_only"])
        self.assertEqual(delivered["processed_files"], 0)
        self.assertEqual(self.request("list_sessions")["sessions"], [])
        self.complete_scan()
        self.assertEqual(len(self.request("list_sessions")["sessions"]), 1)

    def test_relieving_pressure_keeps_pending_writes_and_rejects_unknown_levels(self):
        self.write(self.codex / "rollout-kept.jsonl", self.codex_events("kept", "Kept through pressure"))
        self.complete_scan()
        pending = dict(self.store._pending)
        self.assertTrue(pending)
        self.request("relieve_pressure", level=2)
        self.assertEqual(dict(self.store._pending), pending)
        self.assertFalse(self.store.request({"op": "relieve_pressure", "level": 3})["ok"])

    def test_memory_revisions_validate_evidence_and_survive_source_deletion(self):
        path = self.write(self.claude / "workspace/session.jsonl", [self.claude_message("The launch needs a native signature")])
        self.complete_scan()
        message = self.request("get_session", session_id="claude:session")["messages"][0]
        values = {"id": "launch", "title": "Signing", "body": "Sign native products.",
                  "sources": [{"session_id": message["session_id"], "message_id": message["id"]}], "tags": ["build"]}
        first = self.request("upsert_memory", **values)
        self.assertEqual(first["memory"]["revision"], 1)
        self.assertFalse(self.request("upsert_memory", **values)["changed"])
        values["body"] = "Sign and verify native products."
        second = self.request("upsert_memory", **values)
        self.assertEqual(second["memory"]["revision"], 2)
        invalid = self.store.request({"op": "upsert_memory", **values, "sources": [{"session_id": "claude:session", "message_id": "bad"}]})
        self.assertFalse(invalid["ok"])
        path.unlink()
        self.complete_scan()
        result = self.request("search_memories", query="verify")["memories"][0]
        self.assertEqual(result["revision"], 2)
        self.assertFalse(result["sources"][0]["available"])
        snapshot = self.store.snapshot()
        self.assertEqual(len([row for row in snapshot if row["key"].startswith("memory_revision:")]), 2)
        self.request("delete_memory", id="launch")
        self.assertEqual(self.request("search_memories")["memories"], [])

    def test_search_filters_and_fts_operators_are_literal_user_text(self):
        self.write(self.claude / "workspace/session.jsonl", [self.claude_message("literal OR query")])
        self.write(self.codex / "rollout-id.jsonl", self.codex_events("id", "other OR query"))
        self.complete_scan()
        matches = self.request("search_transcripts", query='OR "query"', provider="claude", workspace="/workspace")["matches"]
        self.assertEqual([row["session_id"] for row in matches], ["claude:session"])
        semantic = self.store.request({"op": "search_transcripts", "query": "literal", "mode": "semantic"})
        self.assertFalse(semantic["ok"])
        self.assertEqual(semantic["code"], "embedding_unavailable")

    def test_stale_registry_pid_is_not_running_and_verified_snapshot_is_explicit(self):
        self.write(self.claude / "workspace/session.jsonl", [self.claude_message("Question")])
        self.write(self.home / ".claude/sessions/123.json", [])
        registry = self.home / ".claude/sessions/123.json"
        registry.write_text(json.dumps({"pid": 123, "sessionId": "session", "procStart": "expected"}))
        self.complete_scan()
        with mock.patch.object(KNOWLEDGE.os, "kill"), mock.patch.object(KNOWLEDGE, "process_start", return_value="reused"):
            self.assertEqual(self.request("list_sessions")["sessions"][0]["state"], "unknown")
        self.request("update_live_sessions", sessions=[{"session_id": "claude:session", "state": "running", "run_id": "run", "live_evidence": "native_process_group"}])
        with mock.patch.object(KNOWLEDGE.os, "kill", side_effect=ProcessLookupError):
            self.assertEqual(self.request("list_sessions", state="running")["sessions"][0]["run_id"], "run")

    def test_store_never_creates_its_own_disk_database(self):
        database = self.home / "forbidden.sqlite"
        with self.assertRaises(KNOWLEDGE.KnowledgeError):
            KNOWLEDGE.KnowledgeStore(database)
        self.assertFalse(database.exists())
        self.assertFalse(self.store.request({"op": "get_session", "session_id": "claude:missing", "limit": True})["ok"])

    def test_live_transcript_progress_and_history_share_one_file_scan_budget(self):
        self.write(self.claude / "aaa/old.jsonl", [self.claude_message("old backlog " * 20000)])
        self.write(self.claude / "bbb/history.jsonl", [self.claude_message("Historical progress")])
        self.store.scan(max_files=1, byte_budget=2048)
        target = self.write(self.claude / "zzz/live.jsonl", [self.claude_message("Fresh live evidence")])
        self.request("update_live_sessions", sessions=[{
            "session_id": "claude:live", "state": "running", "transcript_path": str(target)}])
        page = self.request("get_session", session_id="claude:live")
        self.assertTrue(page["indexing_pending"])
        self.assertTrue(page["partial_index"])
        self.assertIsNone(page["next_offset"])
        for _ in range(8):
            result = self.store.scan(max_files=1, byte_budget=2048)
            self.assertLessEqual(result["processed_files"], 1)
            self.assertLessEqual(result["processed_bytes"], 2048)
            self.acknowledge_all()
        self.assertEqual([row["text"] for row in self.request("get_session", session_id="claude:live")["messages"]],
                         ["Fresh live evidence"])
        self.assertFalse(self.request("get_session", session_id="claude:live")["partial_index"])
        self.assertEqual([row["text"] for row in self.request("get_session", session_id="claude:history")["messages"]],
                         ["Historical progress"])
        self.assertTrue(self.request("get_session", session_id="claude:old")["indexing_pending"])

    def test_metadata_discovery_advances_past_large_transcripts_with_bounded_content_work(self):
        self.write(self.claude / "aaa/large.jsonl", [self.claude_message("Large old transcript " * 30000)])
        for index in range(20):
            self.write(self.claude / f"zzz/later-{index:02d}.jsonl", [self.claude_message(f"Later session {index}")])
        for _ in range(7):
            result = self.store.scan(max_files=3, byte_budget=512)
            self.assertLessEqual(result.get("discovered_files", 0), 3)
            self.assertLessEqual(result["processed_files"], 3)
            self.assertLessEqual(result["processed_bytes"], 512)
            self.acknowledge_all()
        sessions = self.request("list_sessions", limit=100)["sessions"]
        self.assertEqual({row["session_id"] for row in sessions},
                         {"claude:large"} | {f"claude:later-{index:02d}" for index in range(20)})
        result = self.store.scan(max_files=3, byte_budget=512)
        self.assertFalse(result["scan_complete"])
        self.assertGreater(result["backlog_files"], 0)
        self.assertTrue(self.request("list_sessions")["indexing"])
        self.assertTrue(self.request("get_session", session_id="claude:large")["indexing_pending"])

    def restart_acknowledged_store(self):
        self.acknowledge_all()
        if self.store.readthrough:
            restored = KNOWLEDGE.KnowledgeStore(roots=self.roots, home=self.home, semantics=False, read_index=self._native_path)
        else:
            restored = KNOWLEDGE.KnowledgeStore(roots=self.roots, home=self.home, semantics=False)
            restored.restore(self.store.snapshot(), final=True)
        self.addCleanup(restored.close)
        self.store = restored

    def test_cataloged_jsonl_work_advances_all_providers_across_every_scan_restart(self):
        paths = [self.write(self.claude / f"workspace/claude-{index}.jsonl", [self.claude_message("Large history " * 10000)])
                 for index in range(4)]
        paths += [self.write(self.codex / f"2026/rollout-codex-{index}.jsonl", self.codex_events(f"codex-{index}", "Later Codex history " * 10000))
                  for index in range(2)]
        before = [path.read_bytes() for path in paths]
        self.store._discover_sources(100)
        for _ in paths:
            self.restart_acknowledged_store()
            result = self.store.scan(max_files=1, byte_budget=256)
            self.assertLessEqual(result["processed_bytes"], 256)
        progressed = []
        for path in paths:
            source = self.store._document_value("source:" + KNOWLEDGE.digest(str(path.resolve())))
            job = self.store._document_value(source.get("stream_key", "")) or {}
            progressed.append(source["offset"] > 0 or job.get("cursor", 0) > 0)
        self.assertEqual(progressed, [True] * len(paths))
        writes = self.request("drain_writes")["_writes"]
        cursor_index = next(index for index, record in enumerate(writes) if record["key"] == "metadata:jsonl_ingestion_scan")
        source_indices = [index for index, record in enumerate(writes) if record["key"].startswith(("source:", "message:", "metadata:stream_"))]
        self.assertGreater(cursor_index, max(source_indices))
        self.assertEqual([path.read_bytes() for path in paths], before)

    def test_uncataloged_jsonl_discovery_resumes_nested_roots_across_restarts(self):
        expected = set()
        for index in range(4):
            self.write(self.claude / f"workspace/branch-{index}/claude-{index}.jsonl", [self.claude_message("Claude")])
            self.write(self.codex / f"2026/11/{index}/rollout-codex-{index}.jsonl", self.codex_events(f"codex-{index}"))
            self.write(self.archive / f"old-{index}/rollout-archive-{index}.jsonl", self.codex_events(f"archive-{index}"))
            expected.update((f"claude:claude-{index}", f"codex:codex-{index}", f"codex:archive-{index}"))
        for _ in range(20):
            self.store.scan(max_files=1, byte_budget=4096)
            self.restart_acknowledged_store()
        actual = {row[0] for row in self.store.connection.execute("SELECT session_id FROM sources")}
        self.assertEqual(actual, expected)
        # A newly added earlier path must appear on the next wrapped walk.
        self.write(self.claude / "aaa/new.jsonl", [self.claude_message("New earlier file")])
        for _ in range(20):
            self.store.scan(max_files=1, byte_budget=4096)
            self.restart_acknowledged_store()
        self.assertIsNotNone(self.store.connection.execute("SELECT 1 FROM sources WHERE session_id='claude:new'").fetchone())

    def test_uncataloged_codex_database_rows_resume_after_every_scan_restart(self):
        self.native_batch_fixture(8)
        for _ in range(8):
            self.store.scan(max_files=1, byte_budget=128)
            self.restart_acknowledged_store()
        actual = [row[0] for row in self.store.connection.execute("SELECT session_id FROM sources WHERE inode='provider_db' ORDER BY session_id")]
        self.assertEqual(actual, [f"codex:thread-{index:03d}" for index in range(8)])

    def test_jsonl_backlog_status_counts_persisted_work_beyond_hydrated_window(self):
        for index in range(6):
            self.write(self.claude / f"workspace/session-{index}.jsonl", [self.claude_message("Pending history")])
        self.store._discover_sources(100)
        self.restart_acknowledged_store()
        with mock.patch.object(self.store, "_discover", return_value=iter(())):
            result = self.store.scan(max_files=2, byte_budget=4096)
        self.assertEqual(result["backlog_files"], 4)
        self.assertFalse(result["scan_complete"])
        self.assertEqual(self.request("knowledge_status")["status"]["backlog_files"], 4)
        self.assertTrue(self.request("knowledge_status")["status"]["scan_in_progress"])
        self.assertTrue(self.request("list_sessions")["indexing"])

    def test_discovered_append_intent_survives_restart_without_rediscovery(self):
        path = self.write(self.claude / "workspace/session.jsonl", [self.claude_message("First")])
        self.complete_scan()
        with path.open("a") as handle:
            handle.write(json.dumps(self.claude_message("Appended after complete checkpoint")) + "\n")
        self.store._discover_sources(100)
        self.restart_acknowledged_store()
        with mock.patch.object(self.store, "_discover", return_value=iter(())):
            self.store.scan(max_files=1, byte_budget=4096)
        self.assertEqual([row["text"] for row in self.request("get_session", session_id="claude:session")["messages"]],
                         ["First", "Appended after complete checkpoint"])

    def test_discovery_restart_unwinds_replaced_directory_and_reordered_roots_safely(self):
        first = self.write(self.claude / "aaa/first.jsonl", [self.claude_message("First")])
        self.write(self.claude / "zzz/last.jsonl", [self.claude_message("Last")])
        outside = self.write(self.home / "outside/escape.jsonl", [self.claude_message("Outside must not be indexed")])
        self.store.scan(max_files=1, byte_budget=4096)
        self.restart_acknowledged_store()
        first.unlink()
        first.parent.rmdir()
        first.parent.symlink_to(outside.parent, target_is_directory=True)
        for _ in range(6):
            self.store.scan(max_files=1, byte_budget=4096)
            self.restart_acknowledged_store()
        self.assertIsNotNone(self.store.connection.execute("SELECT 1 FROM sources WHERE session_id='claude:last'").fetchone())
        self.assertIsNone(self.store.connection.execute("SELECT 1 FROM sources WHERE session_id='claude:escape'").fetchone())
        self.write(self.codex / "fresh/rollout-new.jsonl", self.codex_events("new"))
        self.store.roots.reverse()
        self.store._discovery = None
        self.store._discovery_state = None
        for _ in range(6):
            self.store.scan(max_files=1, byte_budget=4096)
        self.assertIsNotNone(self.store.connection.execute("SELECT 1 FROM sources WHERE session_id='codex:new'").fetchone())

    def test_a_scan_with_nothing_pending_does_not_search_for_pending_transcripts(self):
        # The search reads every source's document; on a large index it held the
        # worker for a second on every scan even when nothing was pending.
        path = self.write(self.claude / "workspace/quiet.jsonl", [self.claude_message("First")])
        self.store.scan(max_files=10, byte_budget=1 << 20)
        self.store.scan(max_files=10, byte_budget=1 << 20)
        self.assertEqual(self.store._pending_after_scan, 0)
        with mock.patch.object(self.store, "_hydrate_jsonl_backlog", side_effect=AssertionError("searched again")):
            self.store.scan(max_files=10, byte_budget=1 << 20)
        self.write(path, [self.claude_message("First"), self.claude_message("Second")])
        for _ in range(3):
            self.store.scan(max_files=10, byte_budget=1 << 20)
        texts = [row[0] for row in self.store.connection.execute("SELECT text FROM messages")]
        self.assertTrue(any("Second" in text for text in texts), "A later change is still ingested")

    def test_a_scan_stops_early_when_another_request_is_waiting(self):
        for index in range(3):
            self.write(self.claude / f"workspace/waiting-{index}.jsonl", [self.claude_message("Waiting")])
        self.store.request_waiting = lambda: True
        with mock.patch.object(self.store, "_scan_native_history", side_effect=AssertionError("ran on")), \
                mock.patch.object(self.store, "_pending_jsonl_count", side_effect=AssertionError("counted")):
            result = self.store.scan(max_files=10, byte_budget=1 << 20)
        self.assertTrue(result["interrupted"])
        self.assertEqual(result["processed_files"], 0)
        self.assertGreater(result["backlog_files"], 0, "Work remains for the next scan")
        self.store.request_waiting = lambda: False
        for _ in range(3):
            self.store.scan(max_files=10, byte_budget=1 << 20)
        texts = [row[0] for row in self.store.connection.execute("SELECT text FROM messages")]
        self.assertEqual(sum("Waiting" in text for text in texts), 3, "The next scans do the rest")

    def test_unacknowledged_discovery_checkpoint_does_not_mutate_when_later_scan_fails(self):
        for index in range(3):
            self.write(self.claude / f"workspace/session-{index}.jsonl", [self.claude_message("History")])
        self.store.scan(max_files=1, byte_budget=128)
        key = "metadata:jsonl_discovery_scan"
        revision, checkpoint = self.store._pending[key]
        encoded = KNOWLEDGE.json_text(checkpoint)
        # Any step after discovery: the backlog is refilled only when used up.
        with mock.patch.object(self.store, "_priority_sources", side_effect=ValueError("Stop after metadata discovery")):
            response = self.store.request({"op": "scan", "max_files": 1, "byte_budget": 128})
        self.assertFalse(response["ok"])
        self.assertNotEqual(KNOWLEDGE.json_text(self.store._discovery_state), encoded)
        self.assertEqual(self.store._pending[key][0], revision)
        self.assertEqual(KNOWLEDGE.json_text(self.store._pending[key][1]), encoded)
        self.store.scan(max_files=1, byte_budget=128)
        self.assertGreater(self.store._pending[key][0], revision)
        self.assertEqual(self.store._pending[key][1], self.store._discovery_state)

    def test_pending_receipt_keeps_native_source_hint_after_run_ends(self):
        self.write(self.claude / "aaa/old.jsonl", [self.claude_message("history " * 20000)])
        self.store.scan(max_files=1, byte_budget=1024)
        receipt = {"request_id": "00000000-0000-0000-0000-000000000009", "session_id": "claude:recipient",
                   "provider": "claude", "sender_name": "Coordinator", "message": "Receipt evidence",
                   "state": "sent", "delivered": False}
        target = self.write(self.claude / "zzz/recipient.jsonl",
                            [self.claude_message(self.store.messenger.provider_message_text(receipt))])
        self.request("update_live_sessions", sessions=[{
            "session_id": "claude:recipient", "state": "running", "transcript_path": str(target)}])
        self.store._document("receipt:" + receipt["request_id"], receipt)
        self.acknowledge_all()
        self.request("update_live_sessions", sessions=[])
        for _ in range(4):
            self.store.scan(max_files=2, byte_budget=4096)
        confirmed = self.request("get_session_message", request_id=receipt["request_id"])["receipt"]
        self.assertTrue(confirmed["delivered"])
        self.assertEqual(confirmed["delivery_evidence"]["kind"], "provider_user_input_record")

    def test_live_source_hint_rejects_foreign_or_mismatched_paths(self):
        foreign = self.write(self.home / "outside/recipient.jsonl", [self.claude_message("Do not index")])
        mismatch = self.write(self.claude / "workspace/other.jsonl", [self.claude_message("Wrong identity")])
        for path in (foreign, mismatch):
            self.request("update_live_sessions", sessions=[{
                "session_id": "claude:recipient", "state": "running", "transcript_path": str(path)}])
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 0)

    def test_pending_codex_receipt_finds_exact_rollout_without_walking_history(self):
        identifier = "00000000-0000-0000-0000-000000000008"
        target = self.write(self.codex / ("today/rollout-" + identifier + ".jsonl"), self.codex_events(identifier))
        state = self.home / ".codex/state_5.sqlite"
        with closing(sqlite3.connect(state)) as connection, connection:
            connection.execute("CREATE TABLE threads(id TEXT PRIMARY KEY,rollout_path TEXT)")
            connection.execute("INSERT INTO threads VALUES(?,?)", (identifier, str(target)))
        self.store._document("receipt:pending", {"session_id": "codex:" + identifier, "state": "sent"})
        with mock.patch.object(self.store, "_discover", return_value=iter(())), \
                mock.patch.object(self.store, "_scan_codex_catalog"), mock.patch.object(self.store, "_scan_native_history"):
            self.store.scan(max_files=2, byte_budget=4096)
        self.assertEqual([row["text"] for row in self.request("get_session", session_id="codex:" + identifier)["messages"]],
                         ["Codex output"])

    def acknowledge_all(self):
        while self.store._pending:
            batch = self.request("drain_writes")
            self.request("ack_writes", revision=batch["_revision"])

    def test_outbox_transport_waits_for_both_native_durable_acknowledgements(self):
        messenger = mock.Mock()
        self.store._messenger = messenger
        receipt = {"request_id": "request", "session_id": "codex:session", "state": "queued"}
        messenger.prepare.return_value = receipt
        messenger.claim.return_value = {**receipt, "state": "sending", "attempt_id": "attempt"}
        messenger.deliver.return_value = {**receipt, "state": "queued", "attempt_id": "attempt"}
        result = self.request("send_session_message", session_id="codex:session", request_id="request", message="Hi")
        self.assertEqual(result["receipt"]["state"], "queued")
        self.store.scan()
        messenger.claim.assert_not_called()
        messenger.deliver.assert_not_called()
        self.acknowledge_all()
        self.store.scan()
        messenger.claim.assert_called_once()
        messenger.deliver.assert_not_called()
        self.store.scan()
        messenger.deliver.assert_not_called()
        self.acknowledge_all()
        self.store.scan()
        messenger.deliver.assert_called_once()
        self.acknowledge_all()
        self.store.scan()
        messenger.deliver.assert_called_once()

    def test_restored_sending_receipt_is_uncertain_and_never_replayed(self):
        messenger = mock.Mock()
        receipt = {"request_id": "request", "session_id": "claude:session", "state": "sending", "attempt_id": "attempt"}
        messenger.recover.return_value = {**receipt, "state": "unknown"}
        self.store._messenger = messenger
        if self.store.readthrough:
            with self.writer:
                self.writer.execute("INSERT INTO records VALUES(?,?)", ("receipt:request", json.dumps(receipt)))
            self.store.restore([], final=True)
        else:
            self.store.restore([{"key": "receipt:request", "value": receipt}], final=True)
        self.assertEqual(self.request("get_session_message", request_id="request")["receipt"]["state"], "unknown")
        self.acknowledge_all()
        self.store.scan()
        messenger.deliver.assert_not_called()

    def test_native_codex_history_is_cataloged_and_incrementally_searchable(self):
        import sqlite3
        state = self.home / ".codex/state_5.sqlite"
        state.parent.mkdir(parents=True)
        with closing(sqlite3.connect(state)) as connection, connection:
            connection.execute("CREATE TABLE threads(id,cwd,title,name,archived,history_mode,created_at_ms,updated_at_ms)")
            connection.execute("INSERT INTO threads VALUES('native','/workspace','Native title',NULL,0,'native',1000,2000)")
        history = self.home / ".codex/thread_history_1.sqlite"
        with closing(sqlite3.connect(history)) as connection, connection:
            connection.execute("CREATE TABLE thread_items(thread_id,item_id,rollout_ordinal,created_at_ms,item_json,item_type,updated_at_ordinal)")
            connection.execute("CREATE INDEX thread_updates ON thread_items(thread_id,updated_at_ordinal)")
            connection.executemany("INSERT INTO thread_items VALUES(?,?,?,?,?,?,?)", [
                ("native", "user", 0, 1000, json.dumps({"type": "userMessage", "content": [{"type": "text", "text": "Native question"}]}), "userMessage", 0),
                ("native", "agent", 1, 2000, json.dumps({"type": "agentMessage", "text": "Native answer"}), "agentMessage", 1),
                ("native", "command", 2, 3000, json.dumps({"type": "commandExecution", "command": "demo", "aggregatedOutput": "native-tool-output"}), "commandExecution", 2),
            ])
        with mock.patch.object(self.store, "_scan_native_history"):
            pending = self.store.scan()
        self.assertFalse(pending["scan_complete"])
        self.assertTrue(self.request("list_sessions")["indexing"])
        self.assertTrue(self.request("get_session", session_id="codex:native")["indexing_pending"])
        for _ in range(3):
            self.store.scan()
        self.assertFalse(self.request("get_session", session_id="codex:native")["indexing_pending"])
        self.assertEqual(self.request("list_sessions")["sessions"][0]["session_id"], "codex:native")
        self.assertEqual([row["text"] for row in self.request("get_session", session_id="codex:native")["messages"]],
                         ["Native question", "Native answer", "demo", "native-tool-output"])
        matches = self.request("search_transcripts", query="native-tool-output")["matches"]
        self.assertEqual(len([match for match in matches if not match.get("partial_match")]), 1)
        with closing(sqlite3.connect(history)) as connection, connection:
            connection.execute("UPDATE thread_items SET item_json=?,updated_at_ordinal=3 WHERE item_id='agent'",
                               (json.dumps({"type": "agentMessage", "text": "Revised answer"}),))
        for _ in range(3):
            self.store.scan()
        self.assertEqual(len(self.request("search_transcripts", query="Revised")["matches"]), 1)
        self.assertEqual(exact(self.request("search_transcripts", query="Native answer")["matches"]), [])

    def test_paginated_codex_history_preserves_visible_outputs_and_repeated_turns(self):
        state = self.home / ".codex/state_5.sqlite"
        state.parent.mkdir(parents=True)
        with closing(sqlite3.connect(state)) as connection, connection:
            connection.execute("CREATE TABLE threads(id,cwd,title,name,archived,history_mode,created_at_ms,updated_at_ms)")
            connection.execute("INSERT INTO threads VALUES('paginated','/workspace','Current history',NULL,0,'paginated',1000,2000)")
        changes = [{"path": "file.py", "kind": {"type": "update"}, "diff": "-old\n+new\n"}]
        agents = {"child": {"status": "completed", "message": "Regression fixed"}}
        items = [
            {"type": "userMessage", "id": "user-one", "clientId": "client-one", "content": [{"type": "text", "text": "READY"}]},
            {"type": "userMessage", "id": "user-two", "clientId": "client-two", "content": [{"type": "text", "text": "READY"}]},
            {"type": "agentMessage", "id": "answer", "text": "Visible answer", "phase": "final_answer"},
            {"type": "commandExecution", "id": "command", "command": "printf hello", "aggregatedOutput": "hello"},
            {"type": "fileChange", "id": "change", "changes": changes, "status": "completed"},
            {"type": "mcpToolCall", "id": "mcp", "server": "omni", "tool": "get_session",
             "arguments": {"session_id": "codex:fixture"}, "result": {"content": [{"type": "text", "text": "MCP evidence"}]}, "error": "Tool warning"},
            {"type": "collabAgentToolCall", "id": "collab", "tool": "spawnAgent", "prompt": "Investigate regression", "agentsStates": agents},
            {"type": "functionCallOutput", "id": "output", "name": "exec", "output": "Standalone output"},
            {"type": "reasoning", "id": "private", "text": "Private reasoning must stay excluded"},
        ]
        history = self.home / ".codex/thread_history_1.sqlite"
        with closing(sqlite3.connect(history)) as connection, connection:
            connection.execute("CREATE TABLE thread_items(thread_id,item_id,rollout_ordinal,created_at_ms,item_json,item_type,updated_at_ordinal)")
            connection.executemany("INSERT INTO thread_items VALUES(?,?,?,?,?,?,?)", [
                ("paginated", item["id"], index, 2000, json.dumps(item), item["type"], index)
                for index, item in enumerate(items)])
        self.write(self.codex / "rollout-paginated.jsonl", self.codex_events("paginated", "Unique raw setup context"))
        self.complete_scan()
        page = self.request("get_session", session_id="codex:paginated", limit=200)
        text = [row["text"] for row in page["messages"]]
        self.assertEqual(text.count("READY"), 2)
        for expected in ("Visible answer", "printf hello", "hello", KNOWLEDGE.json_text(changes),
                         "omni", "get_session", "Tool warning", "spawnAgent", "Investigate regression",
                         KNOWLEDGE.json_text(agents), "exec", "Standalone output", "Unique raw setup context"):
            self.assertIn(expected, text)
        self.assertTrue(any("MCP evidence" in value for value in text))
        self.assertFalse(any("Private reasoning" in value for value in text))
        self.assertFalse(page["partial_index"])
        self.assertTrue(self.request("search_transcripts", query="Regression fixed")["matches"])

    def test_codex_rollout_order_interleaves_sources_without_splitting_records_or_losing_repeats(self):
        raw_text, native_text = "Raw record " * 1800, "Native record " * 1800
        def message(role, text):
            return {"type": "response_item", "payload": {"type": "message", "role": role,
                    "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}]}}
        events = [self.codex_events("ordered")[0], message("user", "Setup context"),
                  {"type": "turn_context"}, {"type": "turn_context"}, message("user", "READY"),
                  {"type": "turn_context"}, {"type": "turn_context"}, {"type": "turn_context"},
                  message("assistant", raw_text)]
        self.write(self.codex / "rollout-ordered.jsonl", events)
        state = self.home / ".codex/state_5.sqlite"
        with closing(sqlite3.connect(state)) as connection, connection:
            connection.execute("CREATE TABLE threads(id,cwd,title,name,archived,history_mode,created_at_ms,updated_at_ms)")
            connection.execute("INSERT INTO threads VALUES('ordered','/workspace','Ordered',NULL,0,'paginated',1000,2000)")
        history = self.home / ".codex/thread_history_1.sqlite"
        with closing(sqlite3.connect(history)) as connection, connection:
            connection.execute("CREATE TABLE thread_items(thread_id,item_id,rollout_ordinal,created_at_ms,item_json,item_type,updated_at_ordinal)")
            for ordinal, kind, text in ((2, "agentMessage", "Early native answer"),
                                        (4, "userMessage", "READY"), (8, "agentMessage", native_text)):
                item = {"type": kind, "text": text}
                connection.execute("INSERT INTO thread_items VALUES(?,?,?,?,?,?,?)",
                                   ("ordered", str(ordinal), ordinal, 1000, json.dumps(item), kind, ordinal))
        self.complete_scan()
        deduplicated = self.request("get_session", session_id="codex:ordered", limit=200)["messages"]
        self.assertEqual([row["text"] for row in deduplicated[:3]], ["Setup context", "Early native answer", "READY"])
        self.assertEqual(sum(row["text"] == "READY" for row in deduplicated), 1,
                         "The JSONL and database copies of one rollout position are one record")
        paged, offset = [], 0
        for _ in range(len(deduplicated) + 1):
            page = self.request("get_session", session_id="codex:ordered", limit=1, offset=offset)
            paged.extend(row["message_id"] for row in page["messages"])
            if page["next_offset"] is None:
                break
            offset = page["next_offset"]
        self.assertEqual(paged, [row["message_id"] for row in deduplicated])
        matches = self.request("search_transcripts", query="READY", session_id="codex:ordered")["matches"]
        self.assertEqual([row["text"] for row in matches], ["READY"], "Search shows a mirrored record once")
        expected = self.request("get_session", session_id="codex:ordered", limit=200, include_mirrors=True)["messages"]
        self.assertEqual([row["text"] for row in expected[:4]],
                         ["Setup context", "Early native answer", "READY", "READY"])
        tied = [row for row in expected if row["source_line"] == 9]
        self.assertGreater(len(tied), 2)
        self.assertEqual("".join(row["text"] for row in tied), raw_text + native_text)
        expected_ids = [row["message_id"] for row in expected]
        for limit in (1, 2):
            identifiers, offset = [], 0
            for _ in range(len(expected) + 1):
                page = self.request("get_session", session_id="codex:ordered", limit=limit, offset=offset,
                                    include_mirrors=True)
                identifiers.extend(row["message_id"] for row in page["messages"])
                if page["next_offset"] is None:
                    break
                offset = page["next_offset"]
            self.assertEqual(identifiers, expected_ids)
            self.assertEqual(len(identifiers), len(set(identifiers)))

    def test_claude_multiple_sources_retain_source_then_line_order(self):
        self.write(self.claude / "aaa/session.jsonl", [self.claude_message("A first"), self.claude_message("A second")])
        self.write(self.claude / "zzz/session.jsonl", [self.claude_message("B first")])
        self.complete_scan()
        page = self.request("get_session", session_id="claude:session")
        self.assertEqual([row["text"] for row in page["messages"]], ["A first", "A second", "B first"])

    @mock.patch.object(KNOWLEDGE, "NATIVE_SCAN_SOURCES", 1)
    def test_native_history_frontier_survives_acknowledged_restarts_and_wraps(self):
        state = self.home / ".codex/state_5.sqlite"
        state.parent.mkdir(parents=True)
        identifiers = [f"thread-{index}" for index in range(6)]
        with closing(sqlite3.connect(state)) as connection, connection:
            connection.execute("CREATE TABLE threads(id,cwd,title,name,archived,history_mode,created_at_ms,updated_at_ms)")
            connection.executemany("INSERT INTO threads VALUES(?,'/workspace','Fixture',NULL,0,'paginated',1000,2000)",
                                   [(identifier,) for identifier in identifiers])
        history = self.home / ".codex/thread_history_1.sqlite"
        with closing(sqlite3.connect(history)) as connection, connection:
            connection.execute("CREATE TABLE thread_items(thread_id,item_id,rollout_ordinal,created_at_ms,item_json,item_type,updated_at_ordinal)")
            connection.executemany("INSERT INTO thread_items VALUES(?,'answer',0,1000,?,'agentMessage',0)",
                [(identifier, json.dumps({"type": "agentMessage", "text": identifier})) for identifier in identifiers])

        def restart():
            if self.store.readthrough:
                restored = KNOWLEDGE.KnowledgeStore(roots=self.roots, home=self.home, semantics=False, read_index=self._native_path)
            else:
                restored = KNOWLEDGE.KnowledgeStore(roots=self.roots, home=self.home, semantics=False)
                restored.restore(self.store.snapshot(), final=True)
            self.addCleanup(restored.close)
            self.store = restored

        for _ in identifiers:
            self.store.scan()
            writes = self.request("drain_writes")["_writes"]
            frontier_index = next(index for index, record in enumerate(writes)
                                  if record["key"] == "metadata:native_history_scan")
            source_indices = [index for index, record in enumerate(writes)
                              if record["key"].startswith(("source:", "message:"))]
            self.assertGreater(frontier_index, max(source_indices))
            self.acknowledge_all()
            restart()
        rows = self.store.connection.execute("SELECT text FROM messages ORDER BY text").fetchall()
        self.assertEqual([row["text"] for row in rows], identifiers)
        with closing(sqlite3.connect(history)) as connection, connection:
            connection.execute("UPDATE thread_items SET item_json=?,updated_at_ordinal=1 WHERE thread_id=?",
                               (json.dumps({"type": "agentMessage", "text": "Updated first thread"}), identifiers[0]))
        for _ in range(2):
            self.store.scan()
            self.acknowledge_all()
            restart()
        self.assertEqual([row["text"] for row in self.request("get_session", session_id="codex:" + identifiers[0])["messages"]],
                         ["Updated first thread"])

    def native_batch_fixture(self, count, items_per_source=1, text="Native fixture", empty_prefix=0):
        state = self.home / ".codex/state_5.sqlite"
        state.parent.mkdir(parents=True)
        identifiers = [f"thread-{index:03d}" for index in range(count)]
        with closing(sqlite3.connect(state)) as connection, connection:
            connection.execute("CREATE TABLE threads(id,cwd,title,name,archived,history_mode,created_at_ms,updated_at_ms)")
            connection.executemany("INSERT INTO threads VALUES(?,'/workspace','Fixture',NULL,0,'paginated',1000,2000)",
                                   [(identifier,) for identifier in identifiers])
        history = self.home / ".codex/thread_history_1.sqlite"
        with closing(sqlite3.connect(history)) as connection, connection:
            connection.execute("CREATE TABLE thread_items(thread_id,item_id,rollout_ordinal,created_at_ms,item_json,item_type,updated_at_ordinal)")
            connection.execute("CREATE UNIQUE INDEX fixture_ordinals ON thread_items(thread_id,rollout_ordinal)")
            connection.execute("CREATE INDEX fixture_updates ON thread_items(thread_id,updated_at_ordinal)")
            connection.executemany("INSERT INTO thread_items VALUES(?,?,?,1000,?,'agentMessage',?)",
                [(identifier, str(ordinal), ordinal, json.dumps({"type": "agentMessage", "text": text}, ensure_ascii=False), ordinal)
                 for identifier in identifiers[empty_prefix:] for ordinal in range(items_per_source)])
        return identifiers

    def test_native_batch_advances_tiny_and_empty_sources_with_durable_restart_frontier(self):
        identifiers = self.native_batch_fixture(40, empty_prefix=8)
        self.store.scan()
        completed = [self.store._document_value("source:codex-db:" + identifier) for identifier in identifiers]
        self.assertEqual(sum(source.get("history_pending") is False for source in completed), 16)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 8)
        self.assertEqual(self.store._document_value("metadata:native_history_scan"), {"after": "source:codex-db:thread-015"})
        writes = self.request("drain_writes")["_writes"]
        frontier_index = next(index for index, record in enumerate(writes) if record["key"] == "metadata:native_history_scan")
        self.assertGreater(frontier_index, max(index for index, record in enumerate(writes)
            if record["key"].startswith(("source:", "message:"))))
        self.acknowledge_all()
        if self.store.readthrough:
            restored = KNOWLEDGE.KnowledgeStore(roots=self.roots, home=self.home, semantics=False, read_index=self._native_path)
        else:
            restored = KNOWLEDGE.KnowledgeStore(roots=self.roots, home=self.home, semantics=False)
            restored.restore(self.store.snapshot(), final=True)
        self.addCleanup(restored.close)
        self.store = restored
        self.store.scan()
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 24)
        self.assertEqual(self.store._document_value("metadata:native_history_scan"), {"after": "source:codex-db:thread-031"})

    def test_native_batch_caps_total_items_and_each_source_without_losing_later_progress(self):
        self.native_batch_fixture(10, items_per_source=30)
        self.store.scan()
        counts = self.store.connection.execute("SELECT session_id,COUNT(*) count FROM messages GROUP BY session_id ORDER BY session_id").fetchall()
        self.assertEqual(sum(row["count"] for row in counts), 100)
        self.assertEqual([row["count"] for row in counts], [25] * 4)
        self.assertEqual(self.store._document_value("metadata:native_history_scan"), {"after": "source:codex-db:thread-003"})
        self.store.scan()
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 200)
        self.assertEqual(self.store._document_value("metadata:native_history_scan"), {"after": "source:codex-db:thread-007"})
        self.complete_scan()
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 300)

    def test_native_batch_shares_real_utf8_read_budget_across_sources_and_stream_verification(self):
        self.native_batch_fixture(12, text="🌍" * 40000)
        reads = []
        original = self.store._readonly_provider_db

        class Cursor:
            def __init__(self, cursor):
                self.cursor = cursor

            def fetchone(self):
                row = self.cursor.fetchone()
                if row is not None and isinstance(row[0], str):
                    reads.append(len(row[0].encode("utf-8")))
                return row

        class Connection:
            def __init__(self, connection):
                self.connection = connection

            def execute(self, sql, arguments=()):
                cursor = self.connection.execute(sql, arguments)
                return Cursor(cursor) if sql.startswith("SELECT substr(item_json") else cursor

            def close(self):
                self.connection.close()

        def tracked(path):
            connection = original(path)
            return Connection(connection) if connection is not None else None

        with mock.patch.object(self.store, "_readonly_provider_db", side_effect=tracked):
            self.store.scan()
            visited = [self.store._document_value("source:codex-db:" + f"thread-{index:03d}") for index in range(12)]
            self.assertGreater(sum(source.get("projection_version") == KNOWLEDGE.NATIVE_PROJECTION_VERSION for source in visited), 1)
            self.assertLessEqual(sum(reads), 262144)
            self.assertGreater(sum(reads), 200000)
            self.assertTrue(any(source.get("stream_key") for source in visited))
            for _ in range(32):
                reads.clear()
                self.store.scan()
                self.assertLessEqual(sum(reads), 262144)
                sources = [self.store._document_value("source:codex-db:" + f"thread-{index:03d}") for index in range(12)]
                if all(source.get("history_pending") is False and not source.get("stream_key") for source in sources):
                    break
            else:
                self.fail("Shared byte budget stalled native stream verification")
        for index in range(12):
            rows = self.store.connection.execute(
                "SELECT text FROM messages WHERE session_id=? ORDER BY source_line,part,id",
                (f"codex:thread-{index:03d}",)).fetchall()
            self.assertEqual("".join(row["text"] for row in rows), "🌍" * 40000)

    def test_invalid_native_history_frontier_does_not_skip_sources(self):
        for checkpoint in ([], {"after": 7}, {"after": "zzzz"}, {"after": "source:codex-db:"}):
            with self.subTest(checkpoint=checkpoint):
                store = KNOWLEDGE.KnowledgeStore(roots=self.roots, home=self.home, semantics=False)
                self.addCleanup(store.close)
                store._document("metadata:native_history_scan", checkpoint)
                key = "source:codex-db:fixture"
                store._put_source({"key": key, "session_id": "codex:fixture", "provider": "codex",
                                   "path": "provider-db#thread=fixture", "native_history": False})
                store._scan_native_history()
                self.assertEqual(store._document_value("metadata:native_history_scan"), {"after": key})

    @mock.patch.object(KNOWLEDGE, "NATIVE_SCAN_SOURCES", 1)
    def test_requested_late_native_history_runs_next_without_moving_background_frontier(self):
        state = self.home / ".codex/state_5.sqlite"
        state.parent.mkdir(parents=True)
        identifiers = [f"thread-{index}" for index in range(6)]
        with closing(sqlite3.connect(state)) as connection, connection:
            connection.execute("CREATE TABLE threads(id,cwd,title,name,archived,history_mode,created_at_ms,updated_at_ms)")
            connection.executemany("INSERT INTO threads VALUES(?,'/workspace','Fixture',NULL,0,'paginated',1000,2000)",
                                   [(identifier,) for identifier in identifiers])
        history = self.home / ".codex/thread_history_1.sqlite"
        with closing(sqlite3.connect(history)) as connection, connection:
            connection.execute("CREATE TABLE thread_items(thread_id,item_id,rollout_ordinal,created_at_ms,item_json,item_type,updated_at_ordinal)")
            connection.executemany("INSERT INTO thread_items VALUES(?,'answer',0,1000,?,'agentMessage',0)",
                [(identifier, json.dumps({"type": "agentMessage", "text": identifier})) for identifier in identifiers])
        provider_bytes = [path.read_bytes() for path in (state, history)]
        self.store.scan()
        frontier = self.store._document_value("metadata:native_history_scan")
        blocked_path = self.write(self.codex / "blocked/rollout-thread-5.jsonl", self.codex_events("thread-5")).resolve()
        blocked = self.store._initial_source("codex", blocked_path, blocked_path.stat())
        blocked.update(session_id="codex:thread-5", block_version=1, blocked_record={
            "code": "fixture_parser_limit", "parser_version": KNOWLEDGE.INGEST_PARSER_VERSION,
            "size": blocked["size"], "mtime": blocked["mtime"]})
        self.store._put_source(blocked)
        pending = self.request("get_session", session_id="codex:thread-5")
        self.assertEqual(pending["messages"], [])
        self.assertTrue(pending["indexing_pending"])
        self.assertTrue(pending["indexing_blocked"])
        self.assertEqual(self.store._requested_sessions["codex:thread-5"], {"native"})
        self.store.scan()
        self.assertEqual(self.store._document_value("metadata:native_history_scan"), frontier)
        self.assertEqual([row["text"] for row in self.request("get_session", session_id="codex:thread-5")["messages"]], ["thread-5"])
        self.store.scan()
        self.assertEqual(self.store._document_value("metadata:native_history_scan"), {"after": "source:codex-db:thread-1"})
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM messages WHERE session_id='codex:thread-1'").fetchone()[0], 1)
        self.assertEqual([path.read_bytes() for path in (state, history)], provider_bytes)

    def test_requested_jsonl_uses_next_priority_slice_and_flood_keeps_history_progressing(self):
        for index in range(10):
            self.write(self.claude / f"aaa/old-{index}.jsonl", [self.claude_message("Large history " * 20000)])
        target = self.write(self.claude / "zzz/selected.jsonl", [self.claude_message("Requested late transcript")])
        before = target.read_bytes()
        self.store.scan(max_files=100, byte_budget=1024)
        self.assertEqual(self.request("get_session", session_id="claude:selected")["messages"], [])
        first = self.store.scan(max_files=1, byte_budget=4096)
        self.assertLessEqual(first["processed_files"], 1)
        self.assertLessEqual(first["processed_bytes"], 4096)
        self.assertEqual([row["text"] for row in self.request("get_session", session_id="claude:selected")["messages"]],
                         ["Requested late transcript"])
        second = self.store.scan(max_files=1, byte_budget=4096)
        self.assertGreater(second["processed_bytes"], 0)
        self.assertLessEqual(second["processed_bytes"], 4096)
        self.assertEqual(target.read_bytes(), before)

    def test_requested_session_queue_coalesces_bounds_and_rejects_unknown_ids(self):
        for index in range(40):
            self.write(self.claude / f"workspace/session-{index}.jsonl", [self.claude_message("Incomplete transcript " * 1000)])
        self.store.scan(byte_budget=1)
        for index in range(40):
            self.request("get_session", session_id=f"claude:session-{index}")
        self.assertEqual(len(self.store._requested_sessions), 32)
        for _ in range(5):
            self.request("get_session", session_id="claude:session-39")
        self.assertEqual(len(self.store._requested_sessions), 32)
        pending = list(self.store._requested_sessions)
        for identifier in ("unknown", "claude:absent"):
            self.assertFalse(self.store.request({"op": "get_session", "session_id": identifier})["ok"])
        self.assertEqual(list(self.store._requested_sessions), pending)
        self.store.scan(max_files=1, byte_budget=64)
        self.assertNotIn(pending[0], self.store._requested_sessions)
        self.assertEqual(list(self.store._requested_sessions), pending[1:])

    def test_repeated_requested_partial_jsonl_progresses_without_backlog_or_live_priority(self):
        self.write(self.claude / "workspace/selected.jsonl", [self.claude_message("Long requested history " * 20000)])
        self.store.scan(byte_budget=1024)
        self.request("get_session", session_id="claude:selected")
        self.store.scan(max_files=1, byte_budget=1024)
        self.assertNotIn("claude:selected", self.store._requested_sessions)
        self.request("get_session", session_id="claude:selected")
        # A saved partial source can outlive the transient ingestion backlog.
        self.store._backlog.clear()
        self.store._backlog_set.clear()
        self.store._discovery = iter(())
        result = self.store.scan(max_files=1, byte_budget=1024)
        self.assertGreater(result["processed_bytes"], 0)
        self.assertNotIn("claude:selected", self.store._requested_sessions)

    def test_requested_history_flood_does_not_starve_live_receipt_evidence(self):
        for index in range(8):
            self.write(self.claude / f"aaa/old-{index}.jsonl", [self.claude_message("Large requested history " * 20000)])
        receipt = {"request_id": "00000000-0000-0000-0000-000000000077", "session_id": "claude:live",
                   "provider": "claude", "sender_name": "Coordinator", "message": "Live evidence",
                   "state": "sent", "delivered": False}
        target = self.write(self.claude / "zzz/live.jsonl", [self.claude_message(self.store.messenger.provider_message_text(receipt))])
        self.store.scan(byte_budget=1)
        self.request("update_live_sessions", sessions=[{"session_id": "claude:live", "state": "running", "transcript_path": str(target)}])
        self.store._document("receipt:" + receipt["request_id"], receipt)
        for index in range(8):
            self.request("get_session", session_id=f"claude:old-{index}")
        for _ in range(2):
            result = self.store.scan(max_files=2, byte_budget=4096)
            self.assertLessEqual(result["processed_bytes"], 4096)
        self.assertTrue(self.request("get_session_message", request_id=receipt["request_id"])["receipt"]["delivered"])
        self.assertTrue(self.store._requested_sessions)

    def test_pending_native_receipt_prioritizes_late_database_only_input(self):
        state = self.home / ".codex/state_5.sqlite"
        state.parent.mkdir(parents=True)
        receipt = {"request_id": "00000000-0000-0000-0000-000000000078", "session_id": "codex:zz-recipient",
                   "provider": "codex", "sender_name": "Coordinator", "message": "Native receipt evidence",
                   "state": "sent", "delivered": False}
        identifiers = [f"aa-history-{index:02d}" for index in range(32)] + ["zz-recipient"]
        with closing(sqlite3.connect(state)) as connection, connection:
            connection.execute("CREATE TABLE threads(id,cwd,title,name,archived,history_mode,created_at_ms,updated_at_ms)")
            connection.executemany("INSERT INTO threads VALUES(?,'/workspace','Fixture',NULL,0,'paginated',1000,2000)",
                                   [(identifier,) for identifier in identifiers])
        history = self.home / ".codex/thread_history_1.sqlite"
        with closing(sqlite3.connect(history)) as connection, connection:
            connection.execute("CREATE TABLE thread_items(thread_id,item_id,rollout_ordinal,created_at_ms,item_json,item_type,updated_at_ordinal)")
            connection.execute("INSERT INTO thread_items VALUES('zz-recipient','user',0,1000,?,'userMessage',0)",
                (json.dumps({"type": "userMessage", "text": self.store.messenger.provider_message_text(receipt)}),))
        self.store.scan()
        frontier = self.store._document_value("metadata:native_history_scan")
        self.store._document("receipt:" + receipt["request_id"], receipt)
        self.store.scan()
        self.assertEqual(self.store._document_value("metadata:native_history_scan"), frontier)
        self.assertTrue(self.request("get_session_message", request_id=receipt["request_id"])["receipt"]["delivered"])

    def test_verified_running_native_session_requests_refresh_after_complete_snapshot(self):
        state = self.home / ".codex/state_5.sqlite"
        state.parent.mkdir(parents=True)
        with closing(sqlite3.connect(state)) as connection, connection:
            connection.execute("CREATE TABLE threads(id,cwd,title,name,archived,history_mode,created_at_ms,updated_at_ms)")
            connection.execute("INSERT INTO threads VALUES('running','/workspace','Fixture',NULL,0,'paginated',1000,2000)")
        history = self.home / ".codex/thread_history_1.sqlite"
        with closing(sqlite3.connect(history)) as connection, connection:
            connection.execute("CREATE TABLE thread_items(thread_id,item_id,rollout_ordinal,created_at_ms,item_json,item_type,updated_at_ordinal)")
            connection.execute("INSERT INTO thread_items VALUES('running','first',0,1000,?,'agentMessage',0)",
                               (json.dumps({"type": "agentMessage", "text": "First"}),))
        self.complete_scan()
        self.assertFalse(self.request("get_session", session_id="codex:running")["indexing_pending"])
        self.assertFalse(self.store._requested_sessions)
        with closing(sqlite3.connect(history)) as connection, connection:
            connection.execute("INSERT INTO thread_items VALUES('running','second',1,2000,?,'agentMessage',1)",
                               (json.dumps({"type": "agentMessage", "text": "New live answer"}),))
        provider_bytes = history.read_bytes()
        with mock.patch.object(self.store.messenger, "discover_codex_sessions", return_value={"available": True, "sessions": [
                {"session_id": "codex:running", "state": "running", "live_verified": True}]}):
            pending = self.request("get_session", session_id="codex:running")
        self.assertTrue(pending["indexing_pending"])
        self.assertEqual(self.store._requested_sessions["codex:running"], {"native"})
        frontier = self.store._document_value("metadata:native_history_scan")
        self.store.scan()
        self.assertEqual(self.store._document_value("metadata:native_history_scan"), frontier)
        self.assertEqual([row["text"] for row in self.request("get_session", session_id="codex:running")["messages"]],
                         ["First", "New live answer"])
        self.assertEqual(history.read_bytes(), provider_bytes)

    def test_memory_candidates_exclude_considered_and_own_consolidation_sessions(self):
        first = self.write(self.claude / "workspace/session.jsonl", [self.claude_message("Useful durable conclusion. " * 5, "assistant")])
        self.write(self.claude / "workspace/memory-agent.jsonl", [self.claude_message("Consolidator own output. " * 5, "assistant")])
        self.complete_scan()
        self.request("update_memory_sessions", session_ids=["claude:memory-agent"])
        candidates = self.request("memory_candidates")["candidates"]
        self.assertEqual([row["session_id"] for row in candidates], ["claude:session"])
        identifier = candidates[0]["message_id"]
        self.request("dismiss_memory_candidate", message_id=identifier, reason="An ephemeral progress report")
        self.assertEqual(self.request("memory_candidates")["candidates"], [])
        first.unlink()
        self.complete_scan()
        self.write(first, [self.claude_message("Useful durable conclusion. " * 5, "assistant")])
        self.complete_scan()
        self.assertEqual(self.request("memory_candidates")["candidates"], [])

    def test_a_candidate_batch_fits_what_agent_harnesses_show(self):
        self.write(self.claude / "workspace/session.jsonl",
                   [self.claude_message(("Durable conclusion %d. " % index) + "detail " * 900, "assistant")
                    for index in range(8)])
        self.complete_scan()
        first = self.request("memory_candidates")["candidates"]
        self.assertLess(len(KNOWLEDGE.json_text(first).encode()), KNOWLEDGE.CANDIDATE_RESPONSE_BYTES + 8000)
        self.assertLess(len(first), 8, "candidates beyond the budget wait for the next batch")
        self.request("dismiss_memory_candidates", items=[
            {"message_id": item["message_id"], "reason": "test"} for item in first])
        self.assertTrue(self.request("memory_candidates")["candidates"], "the rest are offered next")

    def test_upsert_memory_consumes_its_evidence_candidate(self):
        self.write(self.claude / "workspace/session.jsonl", [self.claude_message("A durable build conclusion. " * 5, "assistant")])
        self.complete_scan()
        candidate = self.request("memory_candidates")["candidates"][0]
        self.request("upsert_memory", title="Build", body="Keep the useful conclusion.",
                     sources=[{"session_id": candidate["session_id"], "message_id": candidate["message_id"]}])
        self.assertEqual(self.request("memory_candidates")["candidates"], [])

    def test_capacity_limit_pauses_ingestion_and_reports_the_gap(self):
        self.write(self.claude / "workspace/session.jsonl", [self.claude_message("Question")])
        with mock.patch.object(KNOWLEDGE, "CACHE_LIMIT_BYTES", 1):
            result = self.store.scan()
            self.assertEqual(result["processed_files"], 0)
            self.assertTrue(self.request("knowledge_status")["status"]["capacity_limited"])

    def test_same_length_in_place_rewrite_before_unchanged_tail_reindexes(self):
        path = self.write(self.claude / "workspace/session.jsonl", [
            self.claude_message("Original zebra"), self.claude_message("unchanged " * 1000, "assistant")])
        self.complete_scan()
        attributes = path.stat()
        content = path.read_text().replace("Original zebra", "Replaced otter")
        path.write_text(content)
        os.utime(path, ns=(attributes.st_atime_ns, attributes.st_mtime_ns + 1000000))
        self.complete_scan()
        self.assertEqual(exact(self.request("search_transcripts", query="zebra")["matches"]), [])
        self.assertEqual(len(self.request("search_transcripts", query="otter")["matches"]), 1)

    def test_deleting_memory_with_wildcard_id_preserves_other_revision_history(self):
        self.write(self.claude / "workspace/session.jsonl", [self.claude_message("Source evidence for independent durable memories")])
        self.complete_scan()
        evidence = self.request("get_session", session_id="claude:session")["messages"][0]
        references = [{"session_id": evidence["session_id"], "message_id": evidence["message_id"]}]
        for identifier in ("a%", "alpha", "a_", "ab"):
            for revision in (1, 2):
                self.request("upsert_memory", id=identifier, title="Independent memory", body=f"Revision {revision} for {identifier}",
                             sources=references)
        self.acknowledge_all()
        for identifier in ("a%", "a_"):
            self.request("delete_memory", id=identifier)
            self.acknowledge_all()
            for preserved in ("alpha", "ab"):
                for revision in (1, 2):
                    record = self.store._document_value(f"memory_revision:{preserved}:{revision}")
                    self.assertIsNotNone(record, (identifier, preserved, revision))
                    self.assertEqual(record["body"], f"Revision {revision} for {preserved}")
        self.assertEqual({row["id"] for row in self.request("search_memories")["memories"]}, {"alpha", "ab"})
        self.assertEqual(self.request("get_session", session_id="claude:session")["messages"][0]["text"],
                         "Source evidence for independent durable memories")

    def test_private_worker_socket_requires_its_native_parent_pid_and_uid(self):
        client = mock.Mock()
        uid = os.geteuid()
        for peer, accepted in (((123, uid), True), ((456, uid), False), ((123, uid + 1), False)):
            with self.subTest(peer=peer), mock.patch.object(KNOWLEDGE, "peer_identity", return_value=peer):
                self.assertEqual(KNOWLEDGE.same_user_peer(client, owner_pid=123), accepted)

    def test_old_worker_cleanup_preserves_a_successor_socket(self):
        path = mock.Mock()
        attributes = mock.Mock(st_mode=KNOWLEDGE.stat.S_IFSOCK, st_dev=1, st_ino=12)
        path.lstat.return_value = attributes
        KNOWLEDGE.unlink_owned_socket(path, (1, 11))
        path.unlink.assert_not_called()
        KNOWLEDGE.unlink_owned_socket(path, (1, 12))
        path.unlink.assert_called_once_with()

    def test_rewrite_and_growth_before_unchanged_tail_is_eventually_reindexed(self):
        path = self.write(self.claude / "workspace/session.jsonl", [self.claude_message("Original zebra"),
                          self.claude_message("unchanged tail " * 30000, "assistant")])
        self.complete_scan()
        self.acknowledge_all()
        attributes = path.stat()
        content = path.read_text().replace("Original zebra", "Replaced otter")
        path.write_text(content + json.dumps(self.claude_message("New appended turn")) + "\n")
        os.utime(path, ns=(attributes.st_atime_ns, attributes.st_mtime_ns + 1000000))
        audited = []
        max_cycles = 100 + 4 * ((path.stat().st_size + 16383) // 16384)
        for _ in range(max_cycles):
            result = self.store.scan(max_files=1, byte_budget=16384)
            audited.append(result.get("audited_bytes", 0))
            self.acknowledge_all()
            if result["scan_complete"] and not result["backlog_files"]:
                break
        self.assertLessEqual(max(audited), 4 * 65536)
        self.assertEqual(exact(self.request("search_transcripts", query="zebra")["matches"]), [])
        self.assertEqual(len(self.request("search_transcripts", query="otter")["matches"]), 1)
        self.assertEqual(len(self.request("search_transcripts", query="appended")["matches"]), 1)

    def test_native_provider_item_deletion_removes_search_and_marks_memory_evidence_missing(self):
        state = self.home / ".codex/state_5.sqlite"
        state.parent.mkdir(parents=True)
        with closing(sqlite3.connect(state)) as connection, connection:
            connection.execute("CREATE TABLE threads(id,cwd,title,name,archived,history_mode,created_at_ms,updated_at_ms)")
            connection.execute("INSERT INTO threads VALUES('native','/workspace','Native title',NULL,0,'native',1000,2000)")
        history = self.home / ".codex/thread_history_1.sqlite"
        with closing(sqlite3.connect(history)) as connection, connection:
            connection.execute("CREATE TABLE thread_items(thread_id,item_id,rollout_ordinal,created_at_ms,item_json,item_type,updated_at_ordinal)")
            connection.execute("CREATE UNIQUE INDEX native_ordinals ON thread_items(thread_id,rollout_ordinal)")
            connection.execute("CREATE INDEX native_updates ON thread_items(thread_id,updated_at_ordinal)")
            connection.execute("INSERT INTO thread_items VALUES(?,?,?,?,?,?,?)", (
                "native", "answer", 0, 2000, json.dumps({"type": "agentMessage", "text": "A durable deleted-provider conclusion"}), "agentMessage", 1))
        for _ in range(3):
            self.store.scan()
            self.acknowledge_all()
        message = self.request("get_session", session_id="codex:native")["messages"][0]
        self.request("upsert_memory", id="durable", title="Durable conclusion", body="Keep this authored memory.",
                     sources=[{"session_id": "codex:native", "message_id": message["message_id"]}])
        self.acknowledge_all()
        with closing(sqlite3.connect(history)) as connection, connection:
            connection.execute("DELETE FROM thread_items WHERE item_id='answer'")
            connection.execute("INSERT INTO thread_items VALUES(?,?,?,?,?,?,?)", (
                "native", "next", 1, 3000, json.dumps({"type": "agentMessage", "text": "Next checkpoint progress"}), "agentMessage", 2))
        for _ in range(6):
            self.store.scan()
            self.acknowledge_all()
        self.assertEqual(exact(self.request("search_transcripts", query="deleted-provider")["matches"]), [])
        self.assertEqual(len(self.request("search_transcripts", query="checkpoint progress")["matches"]), 1)
        memory = self.request("search_memories", query="authored")["memories"][0]
        self.assertFalse(memory["sources"][0]["available"])
        self.assertGreaterEqual(self.store._document_value("source:codex-db:native")["history_cursor"], 2)

    def test_rewrite_in_last_partial_block_is_detected_before_append_rehashes_it(self):
        text = "a" * 69900 + " original_zebra " + "b" * 30000
        path = self.write(self.claude / "workspace/session.jsonl", [self.claude_message(text)])
        self.complete_scan()
        self.acknowledge_all()
        attributes = path.stat()
        changed = path.read_text().replace("original_zebra", "replaced_otter")
        path.write_text(changed + json.dumps(self.claude_message("Next appended turn")) + "\n")
        os.utime(path, ns=(attributes.st_atime_ns, attributes.st_mtime_ns + 1000000))
        self.complete_scan()
        self.assertEqual(exact(self.request("search_transcripts", query="original_zebra")["matches"]), [])
        self.assertEqual(len(self.request("search_transcripts", query="replaced_otter")["matches"]), 1)

    def test_repeated_append_during_audit_revisits_previously_checked_blocks(self):
        text = "a" * 69900 + " original_zebra " + "b" * 530000
        path = self.write(self.claude / "workspace/session.jsonl", [self.claude_message(text)])
        self.complete_scan()
        self.acknowledge_all()
        with path.open("a") as handle:
            handle.write(json.dumps(self.claude_message("First appended turn")) + "\n")
        self.store.scan()
        self.acknowledge_all()
        self.assertGreater(self.store._pending_source_audits(), 0)
        attributes = path.stat()
        changed = path.read_text().replace("original_zebra", "replaced_otter")
        path.write_text(changed + json.dumps(self.claude_message("Second appended turn")) + "\n")
        os.utime(path, ns=(attributes.st_atime_ns, attributes.st_mtime_ns + 1000000))
        self.complete_scan()
        self.assertEqual(exact(self.request("search_transcripts", query="original_zebra")["matches"]), [])
        self.assertEqual(len(self.request("search_transcripts", query="replaced_otter")["matches"]), 1)

    def test_previous_parser_blocked_record_retries_without_touching_the_transcript(self):
        path = self.write(self.claude / "workspace/session.jsonl", [self.claude_message("Before"),
                          self.claude_message("Oversized pending output. " * 12000, "assistant")])
        first_line_size = len(path.read_bytes().splitlines(keepends=True)[0])
        self.store.scan(byte_budget=first_line_size)
        row = self.store.connection.execute("SELECT key FROM sources WHERE session_id='claude:session'").fetchone()
        source = self.store._document_value(row["key"])
        source["blocked_record"] = {"parser_version": 1, "size": path.stat().st_size, "mtime": path.stat().st_mtime_ns,
                                    "offset": first_line_size, "code": "streaming_parser_required"}
        self.store._put_source(source)
        self.acknowledge_all()
        for _ in range(100):
            result = self.store.scan()
            self.acknowledge_all()
            if result["scan_complete"] and not result["backlog_files"]:
                break
        self.assertFalse(self.request("get_session", session_id="claude:session")["indexing_blocked"])
        self.assertGreater(len(self.request("search_transcripts", query="Oversized pending")["matches"]), 0)

    def test_streaming_record_larger_than_eight_mib_publishes_full_output_after_validation(self):
        output = "stream-output-line\n" * 530000
        self.assertGreater(len(output.encode()), 8 * 1024 * 1024)
        event = {"message": {"content": [{"content": output, "type": "tool_result"}], "role": "user"},
                 "type": "user", "cwd": "/workspace", "timestamp": "2026-11-06T09:00:00Z"}
        self.write(self.claude / "workspace/large.jsonl", [event])
        first = self.store.scan()
        self.acknowledge_all()
        self.assertEqual(exact(self.request("search_transcripts", query="stream-output-line")["matches"]), [])
        for _ in range(400):
            result = self.store.scan()
            self.acknowledge_all()
            if result["scan_complete"] and not result["backlog_files"]:
                break
        else:
            self.fail("Streaming record did not finish within its bounded work cycles")
        chunks, offset = [], 0
        while True:
            page = self.request("get_session", session_id="claude:large", offset=offset, limit=200)
            chunks.extend(message["text"] for message in page["messages"])
            self.assertTrue(all(message["kind"] == "output" for message in page["messages"]))
            if page["next_offset"] is None:
                break
            offset = page["next_offset"]
        self.assertKeptOutput(chunks, output)
        self.assertEqual(self.request("knowledge_status")["status"]["blocked_sources"], 0)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM documents WHERE key LIKE 'metadata:stream_%'").fetchone()[0], 0)

    def test_stream_checkpoint_resumes_mid_escaped_unicode_string_after_native_ack(self):
        output = ("é🌍\\\"\n" * 20000) + "tail marker"
        self.write(self.claude / "workspace/large.jsonl", [self.claude_message(output, "assistant")])
        self.store.scan(byte_budget=65536)
        self.acknowledge_all()
        self.assertEqual(exact(self.request("search_transcripts", query="tail marker")["matches"]), [])
        if self.store.readthrough:
            restored = KNOWLEDGE.KnowledgeStore(roots=self.roots, home=self.home, semantics=False, read_index=self._native_path)
        else:
            restored = KNOWLEDGE.KnowledgeStore(roots=self.roots, home=self.home, semantics=False)
            restored.restore(self.store.snapshot(), final=True)
        self.addCleanup(restored.close)
        self.store = restored
        for _ in range(100):
            result = self.store.scan()
            self.acknowledge_all()
            if result["scan_complete"] and not result["backlog_files"]:
                break
        messages = self.request("get_session", session_id="claude:large", limit=200)["messages"]
        self.assertEqual("".join(message["text"] for message in messages), output)

    def test_malformed_streamed_record_never_exposes_provisional_text(self):
        path = self.claude / "workspace/large.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text('{"type":"assistant","message":{"role":"assistant","content":"' +
                        "provisional-secret-token " * 10000 + '"},"invalid":}\n')
        for _ in range(100):
            result = self.store.scan()
            self.acknowledge_all()
            if result["scan_complete"] and not result["backlog_files"]:
                break
        self.assertEqual(exact(self.request("search_transcripts", query="provisional-secret-token")["matches"]), [])
        self.assertEqual(self.request("knowledge_status")["status"]["malformed_records"], 1)

    def test_receipt_confirmation_requires_recorded_recipient_input_across_chunk_boundary(self):
        for provider, identifier in (("claude", "11111111-1111-4111-8111-111111111111"),
                                     ("codex", "22222222-2222-4222-8222-222222222222")):
            with self.subTest(provider=provider):
                receipt = {"request_id": identifier, "session_id": provider + ":session", "provider": provider,
                           "sender_name": "Coordinator", "sender_session_id": "codex:sender",
                           "message": "&" * 3000 + " important body", "state": "sent", "delivered": False}
                canonical = self.store.messenger.provider_message_text(receipt)
                # Split the UUID's first component across an 8,000-character
                # boundary; the bounded lookup must find its other component.
                prefix = "p" * (7997 - canonical.index(identifier[:8]))
                submitted = prefix + canonical
                self.store._document("receipt:" + identifier, receipt)
                if provider == "claude":
                    self.write(self.claude / "workspace/session.jsonl", [self.claude_message(submitted)])
                else:
                    events = self.codex_events("session")[:1]
                    events.append({"type": "response_item", "payload": {"type": "message", "role": "user",
                                   "content": [{"type": "input_text", "text": submitted}]}})
                    self.write(self.codex / "rollout-session.jsonl", events)
                self.complete_scan()
                self.acknowledge_all()
                result = self.request("get_session_message", request_id=identifier)["receipt"]
                self.assertEqual(result["state"], "delivered")
                self.assertTrue(result["delivered"])
                self.assertEqual(result["delivery_evidence"]["session_id"], provider + ":session")
                self.assertGreaterEqual(len(result["delivery_evidence"]["message_ids"]), 2)

    def test_receipt_confirmation_ignores_assistant_quotes_other_sessions_and_marker_only(self):
        identifier = "33333333-3333-4333-8333-333333333333"
        receipt = {"request_id": identifier, "session_id": "claude:session", "provider": "claude",
                   "sender_name": "Coordinator", "message": "The actual required body", "state": "sent", "delivered": False}
        canonical = self.store.messenger.provider_message_text(receipt)
        self.store._document("receipt:" + identifier, receipt)
        self.write(self.claude / "workspace/session.jsonl", [self.claude_message(canonical, "assistant"),
                   self.claude_message(self.store.messenger.message_marker(receipt) + " a different body")])
        self.write(self.claude / "workspace/other.jsonl", [self.claude_message(canonical)])
        self.complete_scan()
        self.acknowledge_all()
        result = self.request("get_session_message", request_id=identifier)["receipt"]
        self.assertEqual(result["state"], "sent")
        self.assertFalse(result["delivered"])


class NativeProjectionTests(KnowledgeTests):
    def setUp(self):
        super().setUp()
        self.store.close()
        self._native_path = self.home / "knowledge.sqlite"
        self.writer = sqlite3.connect(self._native_path)
        self.addCleanup(self.writer.close)
        self.writer.execute("PRAGMA journal_mode=WAL")
        self.writer.execute("CREATE TABLE records(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        schema = (Path(__file__).parents[1] / "helpers/omni_knowledge_schema.sql").read_text()
        self.writer.executescript(schema)
        self.store = KNOWLEDGE.KnowledgeStore(roots=self.roots, home=self.home, semantics=False,
                                              read_index=self._native_path)
        self.addCleanup(self.store.close)

    def acknowledge_all(self):
        while self.store._pending:
            batch = self.request("drain_writes")
            with self.writer:
                for record in batch["_writes"]:
                    if record["value"] is None:
                        self.writer.execute("DELETE FROM records WHERE key=?", (record["key"],))
                    else:
                        self.writer.execute("INSERT OR REPLACE INTO records VALUES(?,?)",
                                            (record["key"], json.dumps(record["value"])))
            self.request("ack_writes", revision=batch["_revision"])

    def native_item_fixture(self, encoded, item_type="commandExecution", history_mode="native"):
        state = self.home / ".codex/state_5.sqlite"
        state.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(state)) as connection, connection:
            connection.execute("CREATE TABLE threads(id,cwd,title,name,archived,history_mode,created_at_ms,updated_at_ms)")
            connection.execute("INSERT INTO threads VALUES('native','/workspace','Native title',NULL,0,?,1000,2000)", (history_mode,))
        history = self.home / ".codex/thread_history_1.sqlite"
        with closing(sqlite3.connect(history)) as connection, connection:
            connection.execute("CREATE TABLE thread_items(thread_id,item_id,rollout_ordinal,created_at_ms,item_json,item_type,updated_at_ordinal)")
            connection.execute("CREATE UNIQUE INDEX fixture_ordinals ON thread_items(thread_id,rollout_ordinal)")
            connection.execute("CREATE INDEX fixture_updates ON thread_items(thread_id,updated_at_ordinal)")
            connection.execute("INSERT INTO thread_items VALUES(?,?,?,?,?,?,?)",
                               ("native", "item", 0, 2000, encoded, item_type, 1))
        return history

    def ready_credit_streams(self, jsonl_text, native_text=None, cleanup=False):
        event = self.claude_message(jsonl_text)
        event["ignored_padding"] = "x" * 70000
        path = self.write(self.claude / "workspace/credits.jsonl", [event]).resolve()
        keys = ["source:" + KNOWLEDGE.digest(str(path))]
        if native_text is not None:
            self.native_item_fixture(json.dumps({"type": "agentMessage", "text": native_text}), item_type="agentMessage")
            keys.append("source:codex-db:native")
        with mock.patch.object(self.store, "_stream_publish"):
            for _ in range(80):
                self.store.scan()
                self.acknowledge_all()
                sources = [self.store._document_value(key) or {} for key in keys]
                jobs = [self.store._document_value(source.get("stream_key", "")) or {} for source in sources]
                if all(job.get("phase") == "publishing" for job in jobs):
                    break
            else:
                self.fail("Credit fixture did not reach publication")
        if cleanup:
            with mock.patch.object(self.store, "_stream_cleanup", return_value=False):
                for _ in range(80):
                    self.store.scan()
                    self.acknowledge_all()
                    jobs = [self.store._document_value(source["stream_key"]) or {} for source in sources]
                    if all(job.get("phase") == "cleanup" for job in jobs):
                        break
                else:
                    self.fail("Credit fixture did not reach cleanup")
        if native_text is not None:
            self.request("get_session", session_id="codex:native")
        return sources, jobs

    # The fixtures size their staging to exceed a 200-document cleanup cap.
    @mock.patch.object(KNOWLEDGE, "STREAM_CLEAN_DOCUMENTS", 200)
    def test_stream_publication_shares_scan_cap_with_ready_native_work(self):
        self.ready_credit_streams("JSONL text " * 60000, "Native text " * 60000)
        before_cursor = self.store._native_history_cursor
        before_requests = {identifier: set(kinds) for identifier, kinds in self.store._requested_sessions.items()}
        self.assertEqual(self.store._native_stream_credit_reserve(), (16, 100))
        self.assertEqual(self.store._native_history_cursor, before_cursor)
        self.assertEqual(self.store._requested_sessions, before_requests)
        self.store.scan()
        counts = dict(self.store.connection.execute("SELECT provider,COUNT(*) FROM messages GROUP BY provider"))
        self.assertGreater(counts.get("claude", 0), 0)
        self.assertGreater(counts.get("codex", 0), 0)
        self.assertLessEqual(sum(counts.values()), 32)

    # The fixtures size their staging to exceed a 200-document cleanup cap.
    @mock.patch.object(KNOWLEDGE, "STREAM_CLEAN_DOCUMENTS", 200)
    def test_stream_cleanup_shares_scan_cap_with_ready_native_work(self):
        sources, jobs = self.ready_credit_streams("JSONL text " * 8000, "Native text " * 8000, cleanup=True)
        prefixes = [self.store._stream_prefix(job) for job in jobs]
        for prefix in prefixes:
            for index in range(300):
                self.store._document(prefix + "fixture:" + str(index), {"fixture": index})
        self.acknowledge_all()
        count = lambda prefix: self.store.connection.execute("SELECT COUNT(*) FROM documents WHERE key>=? AND key<?",
            (prefix, prefix + "\U0010ffff")).fetchone()[0]
        before = [count(prefix) for prefix in prefixes]
        self.store.scan()
        removed = [previous - count(prefix) for previous, prefix in zip(before, prefixes)]
        self.assertGreater(removed[0], 0)
        self.assertGreater(removed[1], 0)
        self.assertLessEqual(sum(removed), 200)

    def test_native_stream_can_use_unspent_jsonl_publication_credits_after_verification(self):
        sources, jobs = self.ready_credit_streams("One small visible message", "Native text " * 60000)
        native = jobs[1]
        native.update(phase="verifying", verify_index=native["raw_count"] - 1)
        self.store._stream_put_job(native)
        self.acknowledge_all()
        self.store.scan()
        counts = dict(self.store.connection.execute("SELECT provider,COUNT(*) FROM messages GROUP BY provider"))
        self.assertEqual(counts.get("claude"), 1)
        self.assertEqual(counts.get("codex"), 31)

    # The fixtures size their staging to exceed a 200-document cleanup cap.
    @mock.patch.object(KNOWLEDGE, "STREAM_CLEAN_DOCUMENTS", 200)
    def test_jsonl_stream_keeps_full_credit_caps_without_native_work(self):
        sources, jobs = self.ready_credit_streams("JSONL text " * 60000)
        self.store.scan()
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 32)
        with mock.patch.object(self.store, "_stream_cleanup", return_value=False):
            for _ in range(10):
                self.store.scan()
                self.acknowledge_all()
                job = self.store._document_value(sources[0]["stream_key"])
                if job.get("phase") == "cleanup":
                    break
            else:
                self.fail("JSONL credit fixture did not reach cleanup")
        prefix = self.store._stream_prefix(job)
        for index in range(300):
            self.store._document(prefix + "fixture:" + str(index), {"fixture": index})
        self.acknowledge_all()
        count = lambda: self.store.connection.execute("SELECT COUNT(*) FROM documents WHERE key>=? AND key<?",
            (prefix, prefix + "\U0010ffff")).fetchone()[0]
        before = count()
        self.store.scan()
        self.assertEqual(before - count(), 200)

    def test_paginated_file_change_stream_keeps_the_large_diff_head_and_tail(self):
        diff = "+generated change\n" * 500000
        self.assertGreater(len(diff.encode()), 8 * 1024 * 1024)
        self.native_item_fixture(json.dumps({"type": "fileChange", "id": "change", "changes": [
            {"path": "large.patch", "kind": {"type": "update"}, "diff": diff}], "status": "completed"}),
            item_type="fileChange", history_mode="paginated")
        for _ in range(800):
            self.store.scan()
            self.acknowledge_all()
            source = self.store._document_value("source:codex-db:native")
            if source and source.get("history_cursor") == 1 and not source.get("stream_key"):
                break
        else:
            self.fail("Paginated file change did not finish bounded streaming")
        fragments = self.store.connection.execute(
            "SELECT text FROM messages WHERE session_id='codex:native' AND role='tool' ORDER BY source_line,part,id").fetchall()
        combined = "".join(row["text"] for row in fragments)
        self.assertIn(diff[:600], combined)
        self.assertTrue(combined.rstrip().endswith(diff.rstrip()[-200:]))
        self.assertIn("large.patch", combined)

    def test_current_native_visible_shapes_preserve_text_without_encoded_images(self):
        receipt = {"request_id": "00000000-0000-0000-0000-000000000079", "session_id": "codex:native",
                   "provider": "codex", "sender_name": "Coordinator", "message": "Hook echo is not delivery",
                   "state": "sent", "delivered": False}
        hook_echo = self.store.messenger.provider_message_text(receipt)
        items = [
            {"type": "webSearch", "query": "Visible search query", "action": {"type": "search", "queries": ["Related query"], "url": "https://example.test/input"},
             "results": [{"title": "Result title", "snippet": "Useful search excerpt", "url": "https://example.test/result"}]},
            {"type": "hookPrompt", "fragments": [{"hookRunId": "hook", "text": "Visible hook instruction"},
                                                    {"hookRunId": "echo", "text": hook_echo}]},
            {"type": "imageGeneration", "result": "QUJDREVGRw==", "revisedPrompt": "Draw a blue square",
             "savedPath": "/workspace/square.png", "failure": "Image warning"},
            {"type": "imageView", "path": "/workspace/reference.png"},
            {"type": "plan", "text": "Visible plan"},
            {"type": "functionCallOutput", "name": "exec", "namespace": "tools", "output": "Visible function output"},
        ]
        history = self.native_item_fixture(json.dumps(items[0]), item_type="webSearch", history_mode="paginated")
        with closing(sqlite3.connect(history)) as connection, connection:
            connection.executemany("INSERT INTO thread_items VALUES('native',?,?,2000,?,?,?)",
                [(str(index), index, json.dumps(item), item["type"], index + 1) for index, item in enumerate(items[1:], 1)])
        self.store._document("receipt:" + receipt["request_id"], receipt)
        self.complete_scan()
        messages = self.request("get_session", session_id="codex:native", limit=200)["messages"]
        text = "\n".join(row["text"] for row in messages)
        for expected in ("Visible search query", "Related query", "https://example.test/input", "Result title",
                         "Useful search excerpt", "https://example.test/result", "Visible hook instruction",
                         "Draw a blue square", "/workspace/square.png", "Image warning", "/workspace/reference.png",
                         "Visible plan", "exec", "Visible function output"):
            self.assertIn(expected, text)
        self.assertNotIn("QUJDREVGRw==", text)
        hooks = [row for row in messages if row["source_line"] == 2]
        self.assertTrue(hooks)
        self.assertTrue(all(row["role"] == "tool" and row["kind"] == "output" for row in hooks))
        self.assertIn(hook_echo, text)
        self.assertFalse(self.request("get_session_message", request_id=receipt["request_id"])["receipt"]["delivered"])

    def test_native_visible_shapes_stream_without_staging_encoded_image_payload(self):
        snippet = "Search excerpt with evidence\n" * 3000
        hook = "Visible hook instruction\n" * 3000
        encoded_image = "QUJD" * 2200000
        self.assertGreater(len(encoded_image), 8 * 1024 * 1024)
        items = [
            {"type": "webSearch", "query": "Search query", "action": {"queries": ["Related query"]},
             "results": [{"title": "Title", "snippet": snippet, "url": "https://example.test/result"}]},
            {"type": "hookPrompt", "fragments": [{"hookRunId": "hook", "text": hook}]},
            {"type": "imageGeneration", "result": encoded_image, "revisedPrompt": "Visible revised prompt",
             "failure": "Visible failure", "savedPath": "/workspace/result.png"},
        ]
        history = self.native_item_fixture(json.dumps(items[0]), item_type="webSearch", history_mode="paginated")
        with closing(sqlite3.connect(history)) as connection, connection:
            connection.executemany("INSERT INTO thread_items VALUES('native',?,?,2000,?,?,?)",
                [(str(index), index, json.dumps(item), item["type"], index + 1) for index, item in enumerate(items[1:], 1)])
        for _ in range(400):
            self.store.scan()
            staged = self.store.connection.execute("SELECT 1 FROM documents WHERE key LIKE 'metadata:stream_data:%' "
                "AND instr(value,?)>0 LIMIT 1", ("QUJD" * 100,)).fetchone()
            self.assertIsNone(staged, "Encoded image was staged as searchable transcript content")
            self.acknowledge_all()
            source = self.store._document_value("source:codex-db:native")
            if source and source.get("history_cursor") == 3 and not source.get("stream_key"):
                break
        else:
            self.fail("Native visible shape projection did not finish bounded streaming")
        rows = self.store.connection.execute(
            "SELECT source_line,role,kind,text FROM messages WHERE session_id='codex:native' ORDER BY source_line,part,id").fetchall()
        text = "".join(row["text"] for row in rows)
        # Long tool output keeps its head and tail.
        for expected in (snippet[:600], hook[:600], "Search query", "Related query", "Title", "https://example.test/result",
                         "Visible revised prompt", "Visible failure", "/workspace/result.png"):
            self.assertIn(expected, text)
        self.assertNotIn("QUJD" * 100, text)
        self.assertTrue(all(row["role"] == "tool" and row["kind"] == "output" for row in rows if row["source_line"] == 2))

    def test_native_projection_refresh_replaces_old_image_text_preserves_other_state_and_restarts(self):
        item = {"type": "imageGeneration", "result": "LegacyEncodedImagePayload", "revisedPrompt": "Recovered revised prompt"}
        self.native_item_fixture(json.dumps(item), item_type="imageGeneration", history_mode="paginated")
        self.write(self.codex / "rollout-native.jsonl", self.codex_events("native", "Preserved raw context"))
        with mock.patch.object(self.store, "_native_items", return_value=[("tool", "output", item["result"])]):
            self.complete_scan()
        key = "source:codex-db:native"
        source = self.store._document_value(key)
        source.pop("projection_version", None)
        self.store._put_source(source)
        memory = {"id": "keep", "title": "Durable", "body": "Preserved memory", "sources": [], "tags": [], "revision": 1, "created": 1, "updated": 1}
        receipt = {"request_id": "keep", "session_id": "codex:native", "state": "delivered", "delivered": True}
        self.store._put_memory(memory)
        self.store._document("receipt:keep", receipt)
        self.acknowledge_all()
        restored = KNOWLEDGE.KnowledgeStore(roots=self.roots, home=self.home, semantics=False, read_index=self._native_path)
        self.addCleanup(restored.close)
        self.store = restored
        self.assertTrue(self.request("get_session", session_id="codex:native")["indexing_pending"])
        self.complete_scan()
        self.acknowledge_all()
        text = [row["text"] for row in self.request("get_session", session_id="codex:native")["messages"]]
        self.assertNotIn(item["result"], text)
        self.assertIn(item["revisedPrompt"], text)
        self.assertIn("Preserved raw context", text)
        self.assertEqual(self.store._document_value("memory:keep"), memory)
        self.assertEqual(self.store._document_value("receipt:keep"), receipt)
        with mock.patch.object(self.store, "_native_items", side_effect=AssertionError("Projection refreshed twice")):
            self.complete_scan()

    def test_native_projection_refresh_discards_old_stream_staging_before_resuming(self):
        self.native_item_fixture(json.dumps({"type": "imageGeneration", "result": "QUJD" * 100000,
            "revisedPrompt": "Recovered stream prompt"}), item_type="imageGeneration", history_mode="paginated")
        self.store.scan()
        source = self.store._document_value("source:codex-db:native")
        self.assertTrue(source.get("stream_key"))
        job = self.store._document_value(source["stream_key"])
        old_prefix = self.store._stream_prefix(job)
        self.store._document(old_prefix + "chunk:legacy", {"text": "Legacy staged encoded image"})
        for index in range(KNOWLEDGE.STREAM_CLEAN_DOCUMENTS + 5):
            self.store._document(old_prefix + "chunk:legacy:" + str(index), {"text": "Old staged fragment"})
        source.pop("projection_version", None)
        self.store._put_source(source)
        self.acknowledge_all()
        restored = KNOWLEDGE.KnowledgeStore(roots=self.roots, home=self.home, semantics=False, read_index=self._native_path)
        self.addCleanup(restored.close)
        self.store = restored
        for _ in range(4):
            self.store.scan()
            current = self.store._document_value(source["key"])
            if current.get("projection_version") == KNOWLEDGE.NATIVE_PROJECTION_VERSION:
                break
        self.assertEqual(current["history_cursor"], -1)
        current_job = self.store._document_value(current["stream_key"])
        self.assertTrue(current_job["restart"])
        self.assertTrue(current_job["invalid"])
        self.assertEqual(current_job["phase"], "cleanup")
        self.assertLess(self.store._pending[current_job["key"]][0], self.store._pending[source["key"]][0])
        self.acknowledge_all()
        restored = KNOWLEDGE.KnowledgeStore(roots=self.roots, home=self.home, semantics=False, read_index=self._native_path)
        self.addCleanup(restored.close)
        self.store = restored
        for _ in range(100):
            self.store.scan()
            self.acknowledge_all()
            current = self.store._document_value(source["key"])
            if current.get("history_cursor") == 1 and not current.get("stream_key"):
                break
        else:
            self.fail("Projection refresh did not finish old stream cleanup")
        text = "".join(row["text"] for row in self.request("get_session", session_id="codex:native", limit=200)["messages"])
        self.assertEqual(text, "Recovered stream prompt")
        self.assertIsNone(self.store._document_value(old_prefix + "chunk:legacy"))

    def test_paginated_collaboration_stream_keeps_prompt_and_result(self):
        prompt = "Investigate the regression\n" * 3000
        result = "Complete collaboration result\n" * 3000
        self.native_item_fixture(json.dumps({"type": "collabAgentToolCall", "id": "collab", "tool": "spawnAgent",
            "prompt": prompt, "agentsStates": {"child": {"status": "completed", "message": result}}}),
            item_type="collabAgentToolCall", history_mode="paginated")
        for _ in range(100):
            self.store.scan()
            self.acknowledge_all()
            source = self.store._document_value("source:codex-db:native")
            if source and source.get("history_cursor") == 1 and not source.get("stream_key"):
                break
        else:
            self.fail("Paginated collaboration did not finish bounded streaming")
        rows = self.store.connection.execute(
            "SELECT role,text FROM messages WHERE session_id='codex:native' ORDER BY source_line,part,id").fetchall()
        self.assertIn(prompt, "".join(row["text"] for row in rows if row["role"] == "assistant"))
        self.assertIn(result[:600], "".join(row["text"] for row in rows if row["role"] == "tool"), "Tool output keeps its head")

    def test_native_item_larger_than_eight_mib_streams_kept_output(self):
        output = "native-stream-output\n" * 500000
        self.assertGreater(len(output.encode()), 8 * 1024 * 1024)
        self.native_item_fixture(json.dumps({"type": "commandExecution", "command": "demo", "aggregatedOutput": output}))
        for _ in range(800):
            self.store.scan()
            self.acknowledge_all()
            source = self.store._document_value("source:codex-db:native")
            if source and source.get("history_cursor") == 1 and not source.get("stream_key"):
                break
        else:
            self.fail("Native streamed item did not finish its bounded work cycles")
        fragments, offset = [], 0
        while True:
            page = self.request("get_session", session_id="codex:native", offset=offset, limit=200)
            fragments.extend(row["text"] for row in page["messages"] if row["kind"] == "output")
            if page["next_offset"] is None:
                break
            offset = page["next_offset"]
        self.assertKeptOutput(fragments, output)
        self.assertEqual(self.request("knowledge_status")["status"]["blocked_sources"], 0)

    def test_native_stream_restart_preserves_snapshot_and_discards_changed_item_version(self):
        old_output = "discarded native phrase " * 20000
        history = self.native_item_fixture(json.dumps({"type": "commandExecution", "command": "old", "aggregatedOutput": old_output}))
        self.store.scan()
        self.acknowledge_all()
        source = self.store._document_value("source:codex-db:native")
        self.assertTrue(source.get("stream_key"))
        self.assertEqual(exact(self.request("search_transcripts", query="discarded native phrase")["matches"]), [])
        restored = KNOWLEDGE.KnowledgeStore(roots=self.roots, home=self.home, semantics=False, read_index=self._native_path)
        self.addCleanup(restored.close)
        self.store = restored
        with closing(sqlite3.connect(history)) as connection, connection:
            connection.execute("UPDATE thread_items SET item_json=?,updated_at_ordinal=2 WHERE item_id='item'",
                               (json.dumps({"type": "commandExecution", "command": "fresh", "aggregatedOutput": "New version output"}),))
        for _ in range(30):
            self.store.scan()
            self.acknowledge_all()
            source = self.store._document_value("source:codex-db:native")
            if source and source.get("history_cursor") == 2 and not source.get("stream_key"):
                break
        self.assertEqual(exact(self.request("search_transcripts", query="discarded native phrase")["matches"]), [])
        self.assertEqual(len(self.request("search_transcripts", query="New version output")["matches"]), 1)

    def test_native_stream_unicode_checkpoint_resumes_without_restarting_the_item(self):
        output = "é🌍\\\"\n" * 20000
        self.native_item_fixture(json.dumps({"type": "agentMessage", "text": output}), item_type="agentMessage")
        self.store.scan()
        self.acknowledge_all()
        source = self.store._document_value("source:codex-db:native")
        job = self.store._document_value(source["stream_key"])
        self.assertGreater(job["cursor"], 0)
        restored = KNOWLEDGE.KnowledgeStore(roots=self.roots, home=self.home, semantics=False, read_index=self._native_path)
        self.addCleanup(restored.close)
        self.store = restored
        for _ in range(100):
            self.store.scan()
            self.acknowledge_all()
            current = self.store._document_value("source:codex-db:native")
            if current.get("history_cursor") == 1 and not current.get("stream_key"):
                break
        page = self.request("get_session", session_id="codex:native", limit=200)
        self.assertEqual("".join(message["text"] for message in page["messages"]), output)

    def test_native_commit_evicts_overlay_without_losing_search_or_restart_history(self):
        self.write(self.claude / "workspace/session.jsonl", [self.claude_message("Persistent zebra evidence")])
        self.complete_scan()
        self.acknowledge_all()
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM overlay_messages").fetchone()[0], 0)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM overlay_documents").fetchone()[0], 0)
        self.assertEqual(len(self.request("search_transcripts", query="zebra")["matches"]), 1)
        reader = KNOWLEDGE.KnowledgeStore(roots=self.roots, home=self.home, semantics=False, read_index=self._native_path)
        self.addCleanup(reader.close)
        self.assertEqual(reader.request({"op": "get_session", "session_id": "claude:session"})["messages"][0]["text"],
                         "Persistent zebra evidence")
        self.assertEqual(reader.connection.execute("SELECT COUNT(*) FROM overlay_documents").fetchone()[0], 0)
        with self.assertRaises(sqlite3.OperationalError):
            reader.connection.execute("INSERT INTO persisted.records VALUES('forbidden','{}')")

    def test_pending_overlay_hides_stale_persisted_message_before_native_commit(self):
        path = self.write(self.claude / "workspace/session.jsonl", [self.claude_message("Old zebra")])
        self.complete_scan()
        self.acknowledge_all()
        self.write(path, [self.claude_message("New otter")])
        self.complete_scan()
        self.assertEqual(exact(self.request("search_transcripts", query="zebra")["matches"]), [])
        self.assertEqual(len(self.request("search_transcripts", query="otter")["matches"]), 1)
        self.acknowledge_all()
        self.assertEqual(exact(self.request("search_transcripts", query="zebra")["matches"]), [])
        self.assertEqual(len(self.request("search_transcripts", query="otter")["matches"]), 1)

    def test_authored_memory_evidence_and_revision_survive_eviction_and_source_removal(self):
        path = self.write(self.claude / "workspace/session.jsonl", [self.claude_message("A source-linked native conclusion")])
        self.complete_scan()
        candidate = self.request("get_session", session_id="claude:session")["messages"][0]
        self.acknowledge_all()
        self.request("upsert_memory", id="durable", title="Native decision", body="Retain durable memory.",
                     sources=[{"session_id": candidate["session_id"], "message_id": candidate["message_id"]}])
        self.acknowledge_all()
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM overlay_memories").fetchone()[0], 0)
        self.assertEqual(self.request("search_memories", query="durable")["memories"][0]["revision"], 1)
        path.unlink()
        self.complete_scan()
        self.acknowledge_all()
        result = self.request("search_memories", query="durable")["memories"][0]
        self.assertFalse(result["sources"][0]["available"])
        self.assertEqual(self.writer.execute("SELECT COUNT(*) FROM records WHERE key LIKE 'memory_revision:%'").fetchone()[0], 1)

    def test_persisted_corpus_size_does_not_trigger_worker_ram_capacity_gate(self):
        self.write(self.claude / "workspace/session.jsonl", [self.claude_message("First zebra")])
        self.complete_scan()
        self.acknowledge_all()
        self.write(self.claude / "workspace/another.jsonl", [self.claude_message("Second otter")])
        with mock.patch.object(KNOWLEDGE, "CACHE_LIMIT_BYTES", 1):
            self.complete_scan()
            self.acknowledge_all()
            self.assertFalse(self.store.capacity_limited)
        self.assertEqual(len(self.request("list_sessions")["sessions"]), 2)

    def test_pending_writes_past_the_high_mark_pause_indexing_until_saved(self):
        self.write(self.claude / "workspace/session.jsonl", [self.claude_message("First zebra")])
        self.write(self.claude / "workspace/another.jsonl", [self.claude_message("Second otter")])
        with mock.patch.object(KNOWLEDGE, "PENDING_HIGH_BYTES", 1), mock.patch.object(KNOWLEDGE, "PENDING_LOW_BYTES", 1):
            self.store.scan()
            self.assertTrue(self.store._pending)
            self.assertTrue(self.store.capacity_limited)
            self.assertEqual(self.store.scan()["processed_files"], 0, "nothing new is read while writes wait")
            # Each save lets indexing take one more step: it always catches up.
            for _ in range(50):
                self.acknowledge_all()
                self.assertFalse(self.store.capacity_limited)
                if self.store.scan()["scan_complete"] and not self.store._pending:
                    break
            self.acknowledge_all()
        self.assertEqual(len(self.request("list_sessions")["sessions"]), 2)

    def test_native_projection_schema_migrates_old_records_and_keeps_fts_current(self):
        self.write(self.claude / "workspace/session.jsonl", [self.claude_message("Original projection")])
        self.complete_scan()
        self.acknowledge_all()
        key, payload = self.writer.execute("SELECT key,value FROM records WHERE key LIKE 'message:%'").fetchone()
        value = json.loads(payload)
        value["text"] = "Updated projection"
        with self.writer:
            self.writer.execute("INSERT OR REPLACE INTO records VALUES(?,?)", (key, json.dumps(value)))
        self.assertEqual(exact(self.request("search_transcripts", query="Original")["matches"]), [])
        self.assertEqual(len(self.request("search_transcripts", query="Updated")["matches"]), 1)

    def test_semantic_vectors_are_read_from_native_projection_after_eviction_and_restart(self):
        class FixtureEmbedding:
            available = True
            model_id = "fixture-two-dimensional-v1"
            dimensions = 2

            def embed(self, text):
                return [1.0, 0.0] if any(word in text.lower() for word in ("repair", "fix", "broken")) else [0.0, 1.0]

            def status(self):
                return {"available": True, "model_id": self.model_id, "dimensions": self.dimensions}

        self.write(self.claude / "workspace/repair.jsonl", [self.claude_message("Repair the broken build")])
        self.write(self.claude / "workspace/fruit.jsonl", [self.claude_message("A basket of bananas")])
        self.complete_scan()
        self.acknowledge_all()
        self.store._semantics = None
        self.store.semantics.embedding = FixtureEmbedding()
        for _ in range(3):
            self.store.semantics.ensure_vectors(16)
            self.acknowledge_all()
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM overlay_vectors").fetchone()[0], 0)
        self.assertGreater(self.writer.execute("SELECT COUNT(*) FROM vectors").fetchone()[0], 0)
        matches = self.request("search_transcripts", query="fix", mode="semantic")["matches"]
        self.assertEqual(matches[0]["session_id"], "claude:repair")
        reader = KNOWLEDGE.KnowledgeStore(roots=self.roots, home=self.home, read_index=self._native_path)
        self.addCleanup(reader.close)
        reader.semantics.embedding = FixtureEmbedding()
        result = reader.request({"op": "search_transcripts", "query": "fix", "mode": "semantic"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["matches"][0]["session_id"], "claude:repair")
        self.assertEqual(reader.connection.execute("SELECT COUNT(*) FROM overlay_messages").fetchone()[0], 0)

    # Standalone cache tests intentionally exercise the in-memory fixture mode.
    def test_capacity_limit_pauses_ingestion_and_reports_the_gap(self):
        self.test_persisted_corpus_size_does_not_trigger_worker_ram_capacity_gate()

    def test_native_write_acknowledgement_does_not_drop_later_batches(self):
        self.write(self.codex / "rollout-large.jsonl", self.codex_events("large", "q" * 900000))
        self.complete_scan()
        self.acknowledge_all()
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM overlay_messages").fetchone()[0], 0)
        self.assertEqual(self.writer.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                         self.store.knowledge_status()["messages"])


if __name__ == "__main__":
    unittest.main()
