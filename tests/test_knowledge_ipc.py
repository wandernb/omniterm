"""Opt-in worker IPC gate; this binds only a disposable, private Unix socket.

Run outside a sandbox that denies Unix socket binding:
OMNI_IPC_TESTS=1 /opt/homebrew/bin/python3.13 -m unittest discover -s tests -p test_knowledge_ipc.py -v
"""

import json
import os
import selectors
import socket
import sqlite3
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "helpers/omni-knowledge"
SCHEMA = ROOT / "helpers/omni_knowledge_schema.sql"
FRAME_LIMIT = 4 * 1024 * 1024


@unittest.skipUnless(os.environ.get("OMNI_IPC_TESTS") == "1",
                     "real Unix IPC requires the explicit OMNI_IPC_TESTS=1 gate")
class KnowledgeWorkerIPCTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="omni-ipc-", dir="/tmp")
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name).resolve()
        self.endpoint = self.home / "knowledge.sock"
        self.database = self.home / "knowledge.sqlite"
        self.native = sqlite3.connect(self.database)
        self.addCleanup(self.native.close)
        self.native.execute("PRAGMA journal_mode=WAL")
        self.native.execute("PRAGMA synchronous=FULL")
        self.native.execute("CREATE TABLE records(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        self.native.executescript(SCHEMA.read_text())
        self.database.chmod(0o600)
        self.worker = None
        self.addCleanup(self.stop_worker)
        self.start_worker()

    def launch_worker(self, owner_pid):
        return subprocess.Popen(
            ["/usr/bin/python3", str(WORKER), "--serve", "--socket", str(self.endpoint),
             "--owner-pid", str(owner_pid), "--read-index", str(self.database)],
            cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env={"HOME": str(self.home), "PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8",
                 "PYTHONDONTWRITEBYTECODE": "1"})

    def start_worker(self):
        self.worker = self.launch_worker(os.getpid())
        deadline = time.monotonic() + 15
        last_error = None
        with selectors.DefaultSelector() as events:
            events.register(self.worker.stderr, selectors.EVENT_READ)
            while time.monotonic() < deadline:
                if self.worker.poll() is not None:
                    self.fail("Worker exited during startup: " +
                              self.worker.stderr.read(4096).decode("utf-8", "replace"))
                try:
                    response = self.raw_request({"op": "knowledge_status"})
                except (OSError, ValueError) as error:
                    last_error = error
                    # Wait for an error/exit event while polling IPC readiness,
                    # rather than assume a fixed startup duration.
                    events.select(min(0.05, max(0, deadline - time.monotonic())))
                    continue
                self.assertTrue(response.get("ok"), response)
                self.assertTrue(response["status"]["readthrough"])
                self.assertEqual(stat.S_IMODE(self.endpoint.stat().st_mode), 0o600)
                self.request("restore", records=[], final=True)
                return
        self.fail(f"Worker did not become ready within its deadline: {last_error}")

    def stop_worker(self, crash=False):
        if self.worker is not None:
            if self.worker.poll() is None:
                self.worker.kill() if crash else self.worker.terminate()
                try:
                    self.worker.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.worker.kill()
                    self.worker.wait(timeout=5)
            self.worker.stderr.close()
            self.worker = None
        # SIGTERM/SIGKILL do not promise Python finally-block execution. This
        # path belongs exclusively to the child in this private fixture.
        self.endpoint.unlink(missing_ok=True)

    def exchange(self, payload, fragmented=False):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(15)
            client.connect(str(self.endpoint))
            if fragmented:
                split = max(1, len(payload) // 2)
                client.sendall(payload[:split])
                client.sendall(payload[split:])
            else:
                client.sendall(payload)
            client.shutdown(socket.SHUT_WR)
            response = bytearray()
            while len(response) <= FRAME_LIMIT:
                block = client.recv(min(65536, FRAME_LIMIT + 1 - len(response)))
                if not block:
                    break
                response.extend(block)
                if b"\n" in block:
                    break
        self.assertLessEqual(len(response), FRAME_LIMIT, "Worker response exceeded the test's bound")
        self.assertTrue(response.endswith(b"\n"), "Worker returned an incomplete response frame")
        return json.loads(response.split(b"\n", 1)[0])

    def raw_request(self, arguments, fragmented=False):
        encoded = json.dumps(arguments, ensure_ascii=False).encode("utf-8") + b"\n"
        return self.exchange(encoded, fragmented=fragmented)

    def commit_and_acknowledge(self, response):
        for _ in range(64):
            with self.native:
                for record in response.get("_writes", []):
                    if record["value"] is None:
                        self.native.execute("DELETE FROM records WHERE key=?", (record["key"],))
                    else:
                        self.native.execute("INSERT OR REPLACE INTO records VALUES(?,?)",
                                            (record["key"], json.dumps(record["value"], ensure_ascii=False)))
            if "_revision" in response:
                acknowledged = self.raw_request({"op": "ack_writes", "revision": response["_revision"]})
                self.assertTrue(acknowledged.get("ok"), acknowledged)
            if not response.get("_more_writes"):
                return
            response = self.raw_request({"op": "drain_writes"})
            self.assertTrue(response.get("ok"), response)
        self.fail("Worker did not drain its bounded write batches")

    def request(self, operation, **arguments):
        response = self.raw_request({"op": operation, **arguments})
        self.assertTrue(response.get("ok"), response)
        self.commit_and_acknowledge(response)
        return {key: value for key, value in response.items() if not key.startswith("_")}

    def write_events(self, relative_path, events, append=False):
        path = self.home / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a" if append else "w") as handle:
            for event in events:
                handle.write(json.dumps(event) + "\n")
        return path

    @staticmethod
    def claude_message(text):
        return {"type": "assistant", "timestamp": "2026-11-06T09:00:00Z", "cwd": "/fixture",
                "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}

    def scan_fixture(self):
        for _ in range(50):
            response = self.request("scan", max_files=10, byte_budget=65536)
            if response["scan_complete"] and not response["backlog_files"]:
                return
        self.fail("The bounded fixture scan did not converge")

    def test_transcript_append_and_revisioned_memory_survive_a_worker_crash(self):
        self.write_events(".claude/projects/fixture/session.jsonl", [
            self.claude_message("Restarting the connection restored access to stored records.")])
        self.write_events(".codex/sessions/rollout-output.jsonl", [
            {"type": "session_meta", "payload": {"id": "output", "cwd": "/fixture"}},
            {"type": "response_item", "payload": {"type": "function_call_output", "output": "fixture-tool-output"}}])
        self.scan_fixture()
        listed = self.request("list_sessions")["sessions"]
        self.assertEqual({item["session_id"] for item in listed}, {"claude:session", "codex:output"})
        transcript = self.request("get_session", session_id="claude:session")["messages"]
        reference = {"session_id": "claude:session", "message_id": transcript[0]["message_id"]}
        self.assertEqual(self.request("search_transcripts", query="fixture-tool-output")["matches"][0]["kind"], "output")
        first = self.request("upsert_memory", id="connection", title="Connection recovery",
                             body="Restarting the connection restored access.", sources=[reference])
        revised = self.request("upsert_memory", id="connection", title="Connection recovery",
                               body="Restart and verify access to stored records.", sources=[reference])
        self.assertEqual((first["memory"]["revision"], revised["memory"]["revision"]), (1, 2))
        self.write_events(".claude/projects/fixture/session.jsonl",
                          [self.claude_message("Appended fixture evidence remains searchable.")], append=True)
        self.scan_fixture()
        self.stop_worker(crash=True)
        self.start_worker()
        self.assertEqual(len(self.request("get_session", session_id="claude:session")["messages"]), 2)
        self.assertEqual(self.request("search_memories", query="verify")["memories"][0]["revision"], 2)
        self.assertEqual(self.native.execute("SELECT COUNT(*) FROM records WHERE key LIKE 'memory_revision:%'").fetchone()[0], 2)

    def test_malformed_and_incomplete_requests_do_not_abort_the_worker(self):
        for payload in (b"not-json\n", b'{"op":', b"[]\n"):
            with self.subTest(payload=payload):
                response = self.exchange(payload)
                self.assertFalse(response["ok"])
                self.assertEqual(response["code"], "invalid_request")
        response = self.raw_request({"op": "knowledge_status"}, fragmented=True)
        self.assertTrue(response["ok"])
        self.assertIsNone(self.worker.poll())

    def test_restarted_sending_reservation_is_uncertain_without_replay(self):
        receipt_id = "00000000-0000-0000-0000-000000000001"
        self.stop_worker(crash=True)
        receipt = {"request_id": receipt_id, "session_id": "codex:fixture", "provider": "codex",
                   "message": "This fixture must never reach a provider.", "sender_name": "IPC fixture",
                   "state": "sending", "attempt_id": "reserved-before-crash", "delivered": False}
        with self.native:
            self.native.execute("INSERT INTO records VALUES(?,?)", ("receipt:" + receipt_id, json.dumps(receipt)))
        self.start_worker()
        restored = self.request("get_session_message", request_id=receipt_id)["receipt"]
        self.assertEqual(restored["state"], "unknown")
        self.assertFalse(restored["delivered"])
        self.request("scan", max_files=1, byte_budget=4096)
        self.assertEqual(self.request("get_session_message", request_id=receipt_id)["receipt"]["state"], "unknown")

    def test_worker_rejects_an_owner_other_than_its_parent_before_binding(self):
        self.stop_worker()
        self.worker = self.launch_worker(os.getppid())
        self.worker.wait(timeout=15)
        self.assertNotEqual(self.worker.returncode, 0)
        self.assertIn("must be this worker's parent", self.worker.stderr.read(4096).decode("utf-8", "replace"))
        self.assertFalse(self.endpoint.exists())

    def test_real_local_semantic_retrieval_returns_the_related_source(self):
        status = self.request("knowledge_status")["status"]["embedding"]
        if not status["available"]:
            self.skipTest("Real local inference is unavailable: " + status.get("error", "no model"))
        self.write_events(".claude/projects/fixture/related.jsonl", [
            self.claude_message("A lost link prevented access to stored records")])
        self.write_events(".claude/projects/fixture/unrelated.jsonl", [
            self.claude_message("Fresh bread tastes delicious")])
        self.scan_fixture()
        response = self.request("search_transcripts", query="The database connection was interrupted",
                                mode="semantic", limit=2)
        self.assertFalse(response["partial_index"], response)
        self.assertEqual(response["matches"][0]["session_id"], "claude:related")
        self.assertGreater(response["matches"][0]["score"], response["matches"][1]["score"] + 0.1)
        self.assertEqual(response["matches"][0]["source_line"], 1)


if __name__ == "__main__":
    unittest.main()
