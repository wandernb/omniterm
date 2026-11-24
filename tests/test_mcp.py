import importlib.machinery
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


_LOADER = importlib.machinery.SourceFileLoader(
    "omni_mcp", str(Path(__file__).parents[1] / "helpers/omni-mcp")
)
MCP = importlib.util.module_from_spec(importlib.util.spec_from_loader("omni_mcp", _LOADER))
_LOADER.exec_module(MCP)


class MCPProtocolTests(unittest.TestCase):
    def test_schema_and_tool_helpers_preserve_constraints(self):
        value = MCP.schema({"name": MCP.STRING}, ["name"])
        self.assertEqual(value["required"], ["name"])
        self.assertFalse(value["additionalProperties"])
        definition = MCP.tool("inspect", "Inspect", "Read data", read_only=True,
                              destructive=False, idempotent=True)
        self.assertTrue(definition["annotations"]["readOnlyHint"])
        self.assertTrue(definition["annotations"]["idempotentHint"])
        self.assertFalse(definition["annotations"]["openWorldHint"])

    def test_initialize_negotiates_supported_version(self):
        response = MCP.response_for({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        })
        self.assertEqual(response["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("tools", response["result"]["capabilities"])

    def test_initialize_falls_back_to_newest_supported_version(self):
        response = MCP.response_for({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "unsupported"},
        })
        self.assertEqual(response["result"]["protocolVersion"], MCP.SUPPORTED_PROTOCOLS[0])

    def test_instructions_teach_every_session_when_to_notify(self):
        response = MCP.response_for({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        result = response["result"]
        self.assertIn("resources", result["capabilities"])
        instructions = result["instructions"]
        for phrase in ("notify_user", "Never for routine progress", "urgent", "dedupe_key",
                       "preview_schedule", MCP.GUIDE_URI):
            self.assertIn(phrase, instructions)

    def test_guide_resource_is_listed_and_readable(self):
        listed = MCP.response_for({"jsonrpc": "2.0", "id": 2, "method": "resources/list"})
        self.assertEqual([item["uri"] for item in listed["result"]["resources"]], [MCP.GUIDE_URI])
        read = MCP.response_for({"jsonrpc": "2.0", "id": 3, "method": "resources/read",
                                 "params": {"uri": MCP.GUIDE_URI}})
        text = read["result"]["contents"][0]["text"]
        for phrase in ("notify_user", "`dedupe_key`", "omni notify", "Inbox", "widget", "dashboard",
                       "at most 20"):
            self.assertIn(phrase, text)
        missing = MCP.response_for({"jsonrpc": "2.0", "id": 4, "method": "resources/read",
                                    "params": {"uri": "omni://nope"}})
        self.assertEqual(missing["error"]["code"], -32002)

    def test_ping_has_empty_result(self):
        response = MCP.response_for({"jsonrpc": "2.0", "id": 2, "method": "ping"})
        self.assertEqual(response["result"], {})

    def test_tools_have_object_schemas_and_annotations(self):
        response = MCP.response_for({"jsonrpc": "2.0", "id": 2,
                                     "method": "tools/list", "params": {}})
        tools = response["result"]["tools"]
        self.assertGreaterEqual(len(tools), 12)
        self.assertEqual(len({tool["name"] for tool in tools}), len(tools))
        for tool in tools:
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertEqual(tool["outputSchema"]["type"], "object")
            self.assertIn("annotations", tool)

    def test_notification_has_no_response(self):
        self.assertIsNone(MCP.response_for({"jsonrpc": "2.0",
                                            "method": "notifications/initialized"}))

    def test_unknown_method_is_json_rpc_error(self):
        response = MCP.response_for({"jsonrpc": "2.0", "id": 9, "method": "unknown"})
        self.assertEqual(response["error"]["code"], -32601)

    def test_list_agents_returns_structured_and_text_content(self):
        scheduler = {"ok": True, "agents": [{
            "name": "A", "provider": "claude", "enabled": True,
            "workspace": "/tmp", "schedule": {"phrase": "daily at 09:00"},
            "next_run": 1, "overlap": "replace", "timeout_seconds": 7200,
        }]}
        with mock.patch.object(MCP, "scheduler_request", return_value=scheduler):
            response = MCP.response_for({
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "list_agents", "arguments": {}},
            })
        result = response["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"]["agents"][0]["name"], "A")
        self.assertEqual(result["content"][0]["type"], "text")

    def test_tool_failure_is_an_mcp_tool_error(self):
        with mock.patch.object(MCP, "call_tool", side_effect=ValueError("bad schedule")):
            response = MCP.response_for({
                "jsonrpc": "2.0", "id": 4, "method": "tools/call",
                "params": {"name": "preview_schedule", "arguments": {}},
            })
        self.assertTrue(response["result"]["isError"])
        self.assertIn("bad schedule", response["result"]["content"][0]["text"])

    def test_tool_result_text_matches_structured_content(self):
        value = {"message": "café", "items": [1, 2]}
        result = MCP.tool_result(value)
        self.assertEqual(MCP.json.loads(result["content"][0]["text"]), value)
        self.assertEqual(result["structuredContent"], value)


class SchedulerTransportTests(unittest.TestCase):
    def test_successful_request_uses_compact_frame_and_closes(self):
        client = mock.Mock()
        client.recv.side_effect = [b'{"ok":true,', b'"value":3}', b""]
        with mock.patch.object(MCP.socket, "socket", return_value=client):
            self.assertEqual(MCP.scheduler_request({"op": "ping"}), {"ok": True, "value": 3})
        self.assertEqual(MCP.json.loads(client.sendall.call_args.args[0]), {"op": "ping"})
        client.close.assert_called_once_with()

    def test_request_launches_app_once_then_retries(self):
        first = mock.Mock()
        first.connect.side_effect = OSError("offline")
        second = mock.Mock()
        second.recv.side_effect = [b'{"ok":true}', b""]
        with mock.patch.object(MCP.socket, "socket", side_effect=[first, second]), \
                mock.patch.object(MCP.subprocess, "run") as launch, \
                mock.patch.object(MCP, "launch_command", return_value=["open", "app"]), \
                mock.patch.object(MCP.time, "sleep") as sleep:
            result = MCP.scheduler_request({"op": "ping"})
        self.assertTrue(result["ok"])
        launch.assert_called_once_with(["open", "app"],
                                       env={"HOME": str(Path.home()), "PATH": "/usr/bin:/bin"},
                                       stdout=MCP.subprocess.DEVNULL,
                                       stderr=MCP.subprocess.DEVNULL)
        sleep.assert_called_once_with(0.4)
        first.close.assert_called_once_with()
        second.close.assert_called_once_with()

    def test_launch_opens_the_bundle_that_holds_the_helpers(self):
        bundle = Path("/Applications/OmniTerm.app")
        with mock.patch.object(MCP, "APP_BUNDLE", bundle):
            self.assertEqual(MCP.launch_command(), ["open", "-g", str(bundle)])

    def test_launch_uses_the_bundle_id_when_run_from_a_checkout(self):
        with mock.patch.object(MCP, "APP_BUNDLE", Path("/src/OmniTerm")):
            self.assertEqual(MCP.launch_command(), ["open", "-g", "-b", "com.omniterm.app"])

    def test_request_without_launch_fails_once_with_context(self):
        client = mock.Mock()
        client.connect.side_effect = OSError("offline")
        with mock.patch.object(MCP.socket, "socket", return_value=client), \
                mock.patch.object(MCP.subprocess, "run") as launch, \
                mock.patch.object(MCP.time, "sleep") as sleep, \
                self.assertRaisesRegex(RuntimeError, "offline"):
            MCP.scheduler_request({"op": "ping"}, launch=False)
        launch.assert_not_called()
        sleep.assert_not_called()
        client.close.assert_called_once_with()

    def test_scheduler_validation_error_is_not_replayed_or_treated_as_app_failure(self):
        client = mock.Mock()
        client.recv.side_effect = [b'{"ok":false,"error":"rejected"}', b""]
        with mock.patch.object(MCP.socket, "socket", return_value=client) as make_socket, \
                mock.patch.object(MCP.subprocess, "run") as launch, \
                mock.patch.object(MCP.time, "sleep") as sleep, \
                self.assertRaisesRegex(ValueError, "rejected"):
            MCP.scheduler_request({"op": "bad"})
        launch.assert_not_called()
        sleep.assert_not_called()
        make_socket.assert_called_once()
        client.close.assert_called_once_with()

    def test_ambiguous_mutation_is_not_replayed_without_idempotency_key(self):
        client = mock.Mock()
        client.recv.side_effect = OSError("reply lost")
        with mock.patch.object(MCP.socket, "socket", return_value=client) as make_socket, \
                mock.patch.object(MCP.subprocess, "run") as launch, \
                self.assertRaisesRegex(RuntimeError, "not replayed"):
            MCP.scheduler_request({"op": "upsert_memory", "title": "A"})
        make_socket.assert_called_once()
        launch.assert_not_called()

    def test_stable_message_retry_preserves_request_id(self):
        first, second = mock.Mock(), mock.Mock()
        first.recv.side_effect = OSError("reply lost")
        second.recv.side_effect = [b'{"ok":true,"receipt":{"state":"queued"}}', b""]
        payload = {"op": "send_session_message", "request_id": "aa8c98c4-6d30-4be6-871a-39bb7d5aad88",
                   "session_id": "codex:thread", "message": "Check the report"}
        with mock.patch.object(MCP.socket, "socket", side_effect=[first, second]), \
                mock.patch.object(MCP.subprocess, "run"), mock.patch.object(MCP.time, "sleep"):
            result = MCP.scheduler_request(payload)
        self.assertEqual(result["receipt"]["state"], "queued")
        self.assertEqual(first.sendall.call_args, second.sendall.call_args)
        first.settimeout.assert_called_once_with(90)

    def test_lost_memory_reads_and_idempotent_updates_are_retried(self):
        for payload in ({"op": "list_memories"}, {"op": "memory_digest", "regenerate": True},
                        {"op": "update_memory", "id": "m", "weight": 3}):
            first, second = mock.Mock(), mock.Mock()
            first.recv.side_effect = OSError("reply lost")
            second.recv.side_effect = [b'{"ok":true}', b""]
            with self.subTest(op=payload["op"]), mock.patch.object(MCP.socket, "socket", side_effect=[first, second]), \
                    mock.patch.object(MCP.subprocess, "run"), mock.patch.object(MCP.time, "sleep"):
                self.assertEqual(MCP.scheduler_request(payload), {"ok": True})

    def test_lost_reinforcement_is_not_replayed(self):
        client = mock.Mock()
        client.recv.side_effect = OSError("reply lost")
        with mock.patch.object(MCP.socket, "socket", return_value=client) as make_socket, \
                mock.patch.object(MCP.subprocess, "run"), self.assertRaisesRegex(RuntimeError, "not replayed"):
            MCP.scheduler_request({"op": "update_memory", "id": "m", "reinforce": True})
        make_socket.assert_called_once()


class SessionKnowledgeToolTests(unittest.TestCase):
    def setUp(self):
        # Tests may themselves run inside an OmniTerm run; attribution has its own tests.
        patcher = mock.patch.object(MCP, "run_actor", return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_memory_intake_tools_forward_only_bounded_public_operations(self):
        cases = [("memory_candidates", {"limit": 20}),
                 ("dismiss_memory_candidate", {"message_id": "m1", "reason": "Ordinary progress"})]
        for name, arguments in cases:
            with self.subTest(name=name), mock.patch.object(MCP, "scheduler_request", return_value={"ok": True, "considered": True}) as native:
                self.assertEqual(MCP.call_tool(name, arguments), {"considered": True})
                native.assert_called_once_with({"op": name, **arguments})
        for name, arguments in (("memory_candidates", {"limit": 21}),
                                ("dismiss_memory_candidate", {"message_id": "m1", "reason": "x" * 501}),
                                ("configure_memory", {"enabled": "yes"}),
                                ("configure_memory", {"model": "--yolo"}),
                                ("configure_memory", {"model": "gpt 6"}),
                                ("configure_memory", {"model": "x" * 101})):
            with self.subTest(name=name), mock.patch.object(MCP, "scheduler_request") as native, self.assertRaises(ValueError):
                MCP.call_tool(name, arguments)
                native.assert_not_called()

    def test_memory_configuration_uses_conflict_safe_agent_builder(self):
        result = {"agent": {"name": "Omni Memory", "enabled": False}, "changed": True}
        with mock.patch.object(MCP.OMNI, "configure_memory_agent", return_value=result) as configure:
            self.assertEqual(MCP.call_tool("configure_memory", {"provider": "codex", "enabled": False}), result)
            self.assertEqual(MCP.call_tool("configure_memory", {"model": "gpt-6-luna"}), result)
        self.assertEqual(configure.call_args_list, [
            mock.call(provider="codex", workspace=None, enabled=False, model=None,
                      native_request=MCP.scheduler_request),
            mock.call(provider=None, workspace=None, enabled=True, model="gpt-6-luna",
                      native_request=MCP.scheduler_request)])

    def test_every_knowledge_tool_forwards_a_validated_native_request(self):
        reference = {"session_id": "claude:conversation", "message_id": "message-1"}
        request_id = "aa8c98c4-6d30-4be6-871a-39bb7d5aad88"
        cases = [
            ("list_sessions", {"provider": "codex", "state": "running", "limit": 10, "cursor": "page"}),
            ("get_session", {"session_id": "claude:conversation", "offset": 20, "limit": 100}),
            ("search_transcripts", {"query": "deployment retry", "mode": "hybrid", "workspace": "/tmp/project"}),
            ("knowledge_status", {}),
            ("upsert_memory", {"title": "Retry policy", "body": "Use stable IDs", "sources": [reference], "tags": ["operations"]}),
            ("search_memories", {"query": "retry", "mode": "semantic", "limit": 3}),
            ("search_memories", {"type": "declarative", "workspace": "/tmp/project", "after": "2026-11-01",
                                 "before": "2026-11-10T12:00:00Z", "archived": True}),
            ("search_memories", {"like": "memory:memory-1"}),
            ("search_transcripts", {"query": "type:output since:7d -codex", "role": "tool", "after": "2026-11-01"}),
            ("search_transcripts", {"like": "message:message-1", "limit": 5}),
            ("delete_memory", {"id": "memory-1"}),
            ("send_session_message", {"session_id": "codex:thread", "request_id": request_id,
                                      "message": "Please inspect the retry policy", "sender_session_id": "claude:conversation"}),
            ("get_session_message", {"request_id": request_id}),
        ]
        for name, arguments in cases:
            with self.subTest(name=name), mock.patch.object(MCP, "scheduler_request", return_value={"ok": True, "value": {"state": "queued"}}) as native:
                response = MCP.response_for({"jsonrpc": "2.0", "id": 10, "method": "tools/call",
                                             "params": {"name": name, "arguments": arguments}})
                native.assert_called_once_with({"op": name, **arguments})
                result = response["result"]
                self.assertFalse(result["isError"])
                self.assertEqual(result["structuredContent"], {"value": {"state": "queued"}})
                self.assertEqual(json.loads(result["content"][0]["text"]), result["structuredContent"])

    def test_invalid_knowledge_arguments_never_reach_socket(self):
        cases = [
            ("list_sessions", {"limit": True}), ("list_sessions", {"limit": 101}),
            ("list_sessions", {"state": "recent"}), ("list_sessions", {"provider": "other"}),
            ("list_sessions", {"op": "restore"}), ("list_sessions", []),
            ("get_session", {"session_id": "unqualified"}),
            ("get_session", {"session_id": "claude:s", "offset": -1}),
            ("search_transcripts", {"query": "x", "mode": "pretend"}),
            ("search_transcripts", {"query": "x" * 2001}),
            ("search_transcripts", {"query": "x", "role": "robot"}),
            ("search_transcripts", {"query": "x", "after": "soon"}),
            ("search_memories", {"type": "decision"}), ("search_memories", {"archived": "yes"}),
            ("search_memories", {"like": ""}),
            ("upsert_memory", {"title": "A", "body": "B", "sources": []}),
            ("upsert_memory", {"title": "A", "body": "B", "sources": [{"session_id": "claude:s", "message_id": "m", "path": "/tmp/file"}]}),
            ("upsert_memory", {"title": "A", "body": "B", "sources": [{"session_id": "claude:s", "message_id": "m"}], "tags": [1]}),
            ("send_session_message", {"session_id": "codex:s", "message": "A", "request_id": "not-a-uuid"}),
            ("send_session_message", {"session_id": "codex:s", "message": "\x1b[201~\r", "request_id": "aa8c98c4-6d30-4be6-871a-39bb7d5aad88"}),
            ("get_session_message", {}),
        ]
        for name, arguments in cases:
            with self.subTest(name=name, arguments=arguments), mock.patch.object(MCP, "scheduler_request") as native:
                result = MCP.response_for({"jsonrpc": "2.0", "id": 11, "method": "tools/call",
                                           "params": {"name": name, "arguments": arguments}})["result"]
                self.assertTrue(result["isError"])
                native.assert_not_called()

    def test_memory_model_tools_are_declared_with_strict_schemas(self):
        tools = {item["name"]: item for item in MCP.TOOLS}
        for name in ("list_memories", "get_memory", "memory_events", "memory_overview", "memory_graph"):
            self.assertTrue(tools[name]["annotations"]["readOnlyHint"], name)
        for name in ("memory_digest", "update_memory"):
            self.assertFalse(tools[name]["annotations"]["readOnlyHint"], name)
        self.assertTrue(tools["memory_digest"]["annotations"]["idempotentHint"])
        self.assertFalse(tools["update_memory"]["annotations"]["idempotentHint"])
        for name in ("list_memories", "get_memory", "memory_events", "memory_overview", "memory_graph",
                     "memory_digest", "update_memory", "upsert_memory"):
            self.assertFalse(tools[name]["inputSchema"]["additionalProperties"], name)
        self.assertEqual(tools["get_memory"]["inputSchema"]["required"], ["id"])
        self.assertEqual(tools["update_memory"]["inputSchema"]["required"], ["id"])
        upsert = tools["upsert_memory"]["inputSchema"]["properties"]
        self.assertEqual(upsert["memory_type"]["enum"],
                         ["declarative", "episodic", "procedural", "prospective", "autobiographical",
                          "spatial", "conditioning"])
        self.assertEqual((upsert["weight"]["minimum"], upsert["weight"]["maximum"]), (0, 10))
        self.assertEqual(upsert["lineage"]["maxItems"], 20)

    def test_memory_model_tools_forward_validated_native_requests(self):
        reference = {"session_id": "claude:conversation", "message_id": "message-1"}
        cases = [
            ("list_memories", {"memory_type": "procedural", "tag": "ops", "workspace": "/tmp/project",
                               "sort": "evidence", "query": "retry", "include_expired": False,
                               "limit": 500, "offset": 10}),
            ("get_memory", {"id": "memory-1"}),
            ("memory_events", {"limit": 200, "before": "event-9", "kind": "memory_revised", "memory_id": "memory-1"}),
            ("memory_overview", {}),
            ("memory_graph", {"limit": 50, "edges": ["lineage", "similarity"], "min_similarity": 0.65,
                              "memory_type": "episodic", "focus": "memory-1", "mindmap": True}),
            ("memory_graph", {"min_similarity": 1}),
            ("memory_graph", {"focus": "memory-1", "hops": 2, "workspace": "/tmp/project", "after": 1700000000,
                              "min_weight": 2.5}),
            ("memory_digest", {"workspace": "/tmp/project", "regenerate": True}),
            ("update_memory", {"id": "memory-1", "memory_type": "prospective", "weight": 9.5,
                               "pinned": True, "reinforce": False}),
            ("update_memory", {"id": "memory-1", "weight": 0}),
            ("upsert_memory", {"title": "Deploy", "body": "Use the staging gate", "sources": [reference],
                               "memory_type": "procedural", "weight": 7, "lineage": ["memory-0"],
                               "pinned": False, "reason": "Confirmed twice"}),
        ]
        for name, arguments in cases:
            with self.subTest(name=name, arguments=arguments), \
                    mock.patch.object(MCP, "scheduler_request", return_value={"ok": True, "value": 1}) as native:
                result = MCP.response_for({"jsonrpc": "2.0", "id": 12, "method": "tools/call",
                                           "params": {"name": name, "arguments": arguments}})["result"]
                self.assertFalse(result["isError"], result)
                native.assert_called_once_with({"op": name, **arguments})
                self.assertEqual(result["structuredContent"], {"value": 1})

    def test_invalid_memory_model_arguments_never_reach_socket(self):
        reference = {"session_id": "claude:s", "message_id": "m"}
        cases = [
            ("list_memories", {"memory_type": "semantic"}), ("list_memories", {"sort": "random"}),
            ("list_memories", {"limit": 501}), ("list_memories", {"offset": -1}),
            ("list_memories", {"include_expired": "no"}), ("list_memories", {"op": "restore"}),
            ("get_memory", {}), ("get_memory", {"id": ""}), ("get_memory", {"id": "m", "actor": "x"}),
            ("memory_events", {"limit": 201}), ("memory_events", {"kind": "anything"}),
            ("memory_events", {"before": "e" * 65}),
            ("memory_overview", {"verbose": True}),
            ("memory_graph", {"min_similarity": 1.5}), ("memory_graph", {"min_similarity": -0.1}),
            ("memory_graph", {"min_similarity": True}), ("memory_graph", {"min_similarity": float("nan")}),
            ("memory_graph", {"edges": ["friendship"]}), ("memory_graph", {"edges": []}),
            ("memory_graph", {"limit": 501}), ("memory_graph", {"mindmap": 1}),
            ("memory_graph", {"focus": "m", "hops": 3}), ("memory_graph", {"hops": 0}),
            ("memory_graph", {"after": -1}), ("memory_graph", {"min_weight": 11}),
            ("memory_graph", {"workspace": ""}),
            ("memory_digest", {"regenerate": "yes"}), ("memory_digest", {"workspace": ""}),
            ("update_memory", {"weight": 5}), ("update_memory", {"id": "m", "weight": 10.01}),
            ("update_memory", {"id": "m", "weight": -1}), ("update_memory", {"id": "m", "weight": "5"}),
            ("update_memory", {"id": "m", "memory_type": "semantic"}),
            ("update_memory", {"id": "m", "run_id": "spoofed"}),
            ("upsert_memory", {"title": "A", "body": "B", "sources": [reference], "memory_type": "fact"}),
            ("upsert_memory", {"title": "A", "body": "B", "sources": [reference], "weight": 11}),
            ("upsert_memory", {"title": "A", "body": "B", "sources": [reference], "lineage": ["m"] * 21}),
            ("upsert_memory", {"title": "A", "body": "B", "sources": [reference], "reason": "x" * 501}),
            ("upsert_memory", {"title": "A", "body": "B", "sources": [reference], "pinned": "true"}),
            ("upsert_memory", {"title": "A", "body": "B", "sources": [reference], "agent": "spoofed"}),
        ]
        for name, arguments in cases:
            with self.subTest(name=name, arguments=arguments), mock.patch.object(MCP, "scheduler_request") as native:
                result = MCP.response_for({"jsonrpc": "2.0", "id": 13, "method": "tools/call",
                                           "params": {"name": name, "arguments": arguments}})["result"]
                self.assertTrue(result["isError"])
                native.assert_not_called()

    def test_discovery_and_mutation_annotations_match_side_effects(self):
        tools = {item["name"]: item for item in MCP.TOOLS}
        for name in ("list_sessions", "get_session", "search_transcripts", "knowledge_status", "search_memories", "get_session_message"):
            self.assertTrue(tools[name]["annotations"]["readOnlyHint"])
        self.assertFalse(tools["send_session_message"]["annotations"]["readOnlyHint"])
        self.assertTrue(tools["send_session_message"]["annotations"]["idempotentHint"])
        self.assertTrue(tools["delete_memory"]["annotations"]["destructiveHint"])
        self.assertNotIn("restore", tools)
        self.assertNotIn("scan", tools)


class MemoryAttributionTests(unittest.TestCase):
    def setUp(self):
        MCP._RUN_ACTOR.clear()
        self.addCleanup(MCP._RUN_ACTOR.clear)

    def test_memory_mutations_inside_a_run_carry_its_run_and_agent(self):
        reference = {"session_id": "claude:s", "message_id": "m"}
        cases = [("upsert_memory", {"title": "A", "body": "B", "sources": [reference]}),
                 ("delete_memory", {"id": "memory-1"}),
                 ("dismiss_memory_candidate", {"message_id": "m1", "reason": "Progress"}),
                 ("memory_candidates", {"limit": 5}),
                 ("update_memory", {"id": "memory-1", "reinforce": True})]
        with mock.patch.dict(MCP.os.environ, {"OMNI_RUN_ID": "r1", "OMNI_AGENT": "Omni Memory"}):
            for name, arguments in cases:
                with self.subTest(name=name), mock.patch.object(MCP, "scheduler_request", return_value={"ok": True}) as native:
                    MCP.call_tool(name, arguments)
                    native.assert_called_once_with({"op": name, **arguments, "run_id": "r1", "agent": "Omni Memory"})
            with mock.patch.object(MCP, "scheduler_request", return_value={"ok": True}) as native:
                MCP.call_tool("list_memories", {"limit": 5})
                native.assert_called_once_with({"op": "list_memories", "limit": 5})

    def test_a_run_found_by_process_ancestry_supplies_its_agent_once(self):
        def scheduler(payload):
            if payload["op"] == "list_runs":
                return {"ok": True, "runs": [{"id": "r2", "agent": "Nightly", "state": "running", "pid": 4242}]}
            return {"ok": True}

        environment = {key: value for key, value in MCP.os.environ.items()
                       if key not in ("OMNI_RUN_ID", "OMNI_AGENT")}
        with mock.patch.dict(MCP.os.environ, environment, clear=True), \
                mock.patch.object(MCP.OMNI, "process_ancestors", return_value=[4242, 1]), \
                mock.patch.object(MCP, "scheduler_request", side_effect=scheduler) as native:
            MCP.call_tool("delete_memory", {"id": "memory-1"})
            MCP.call_tool("delete_memory", {"id": "memory-2"})
        self.assertEqual([call.args[0] for call in native.call_args_list],
                         [{"op": "list_runs"},
                          {"op": "delete_memory", "id": "memory-1", "run_id": "r2", "agent": "Nightly"},
                          {"op": "delete_memory", "id": "memory-2", "run_id": "r2", "agent": "Nightly"}])

    def test_attribution_failure_never_blocks_the_mutation(self):
        def scheduler(payload):
            if payload["op"] == "list_runs":
                raise RuntimeError("OmniTerm scheduler is unavailable")
            return {"ok": True, "deleted": True}

        environment = {key: value for key, value in MCP.os.environ.items()
                       if key not in ("OMNI_RUN_ID", "OMNI_AGENT")}
        with mock.patch.dict(MCP.os.environ, environment, clear=True), \
                mock.patch.object(MCP.OMNI, "process_ancestors", return_value=[4242]), \
                mock.patch.object(MCP, "scheduler_request", side_effect=scheduler) as native:
            self.assertEqual(MCP.call_tool("delete_memory", {"id": "memory-1"}), {"deleted": True})
        native.assert_called_with({"op": "delete_memory", "id": "memory-1"})
        self.assertEqual(MCP._RUN_ACTOR, [])


class AgentValidationTests(unittest.TestCase):
    def valid_agent(self, workspace):
        return {"name": " Agent ", "provider": "codex", "workspace": str(workspace),
                "schedule": {"kind": "daily"}, "overlap": "replace",
                "advanced_args": ["--color", "always"]}

    def test_validate_agent_normalizes_safe_definition(self):
        with tempfile.TemporaryDirectory() as root:
            validated = MCP.validate_agent(self.valid_agent(Path(root)))
        self.assertEqual(validated["name"], "Agent")
        self.assertEqual(validated["provider"], "codex")
        self.assertEqual(validated["workspace"], str(Path(root).resolve()))
        self.assertEqual(validated["advanced_args"], ["--color", "always"])

    def test_validate_agent_rejects_invalid_fields(self):
        with tempfile.TemporaryDirectory() as root:
            base = self.valid_agent(Path(root))
            cases = {
                "empty name": {"name": "  "},
                "provider": {"provider": "other"},
                "workspace": {"workspace": str(Path(root) / "missing")},
                "overlap": {"overlap": "queue"},
                "argument type": {"advanced_args": "--flag"},
                "argument item": {"advanced_args": ["--flag", 3]},
                "reserved argument": {"advanced_args": ["--model=value"]},
            }
            for label, changes in cases.items():
                with self.subTest(label=label), self.assertRaises(ValueError):
                    MCP.validate_agent({**base, **changes})

    def test_clean_agent_removes_only_computed_schedule_metadata(self):
        agent = {"name": "A", "next_run": 10, "last_run": 5, "schedule": {"kind": "daily"}}
        self.assertEqual(MCP.clean_agent(agent),
                         {"name": "A", "schedule": {"kind": "daily"}})
        self.assertEqual(agent["next_run"], 10)

    def test_build_new_agent_applies_defaults_and_parses_schedule(self):
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(MCP, "scheduler_request", return_value={
                    "settings": {"timezone": "UTC", "default_timeout_seconds": 1800}}):
            agent = MCP.build_agent({"name": "A", "provider": "claude",
                                     "workspace": root, "schedule": "daily at 09:00",
                                     "startup_prompt": "go"})
        self.assertEqual(agent["workspace"], str(Path(root).resolve()))
        self.assertEqual(agent["timeout_seconds"], 1800)
        self.assertEqual(agent["overlap"], "replace")
        self.assertTrue(agent["enabled"])
        self.assertEqual(agent["schedule"]["kind"], "daily")
        self.assertEqual(agent["startup_prompt"], "go")
        self.assertNotIn("timezone", agent)

    def test_loop_creation_and_partial_update_preserve_lifecycle(self):
        with tempfile.TemporaryDirectory() as root:
            agent = self.build({"name": "Loop", "provider": "codex", "workspace": root,
                                "schedule": "every 10 minutes", "kind": "loop", "overlap": "replace",
                                "compact_on_end": True})
            self.assertEqual((agent["overlap"], agent["session_policy"], agent["timeout_seconds"]),
                             ("parallel", "resume", 86400))
            self.assertEqual(agent["run_budget_seconds"], 600)
            updated = self.build({"name": "Loop", "model": "new-model"}, agent)
            self.assertTrue(updated["compact_on_end"])
            self.assertEqual(updated["session_policy"], "resume")
            self.assertEqual(updated["kind"], "loop")

    def test_lifecycle_rejects_invalid_types_and_warning_window(self):
        with tempfile.TemporaryDirectory() as root:
            base = {"name": "A", "provider": "codex", "workspace": root, "schedule": "every 10 minutes"}
            for fields in ({"kind": "forever"}, {"session_policy": "latest"},
                           {"compact_on_end": "false"}, {"run_budget_seconds": True},
                           {"run_budget_seconds": 60, "wrap_up_seconds": 120}):
                with self.subTest(fields=fields), self.assertRaises(ValueError):
                    self.build({**base, **fields})

    def test_build_update_is_partial_and_preserves_next_run(self):
        with tempfile.TemporaryDirectory() as root:
            existing = {"name": "A", "provider": "codex", "workspace": root,
                        "system_prompt": "old", "startup_prompt": "start", "model": "",
                        "permission_mode": "", "advanced_args": [], "overlap": "replace",
                        "timeout_seconds": 7200, "enabled": True,
                        "schedule": {"kind": "daily", "hour": 9, "minute": 0},
                        "next_run": 123, "last_run": 100}
            with mock.patch.object(MCP, "scheduler_request", return_value={
                    "settings": {"timezone": "UTC"}}):
                agent = MCP.build_agent({"name": "A", "system_prompt": "new",
                                         "timeout": None}, existing)
        self.assertEqual(agent["name"], "A")
        self.assertEqual(agent["provider"], "codex")
        self.assertEqual(agent["system_prompt"], "new")
        self.assertIsNone(agent["timeout_seconds"])
        self.assertEqual(agent["next_run"], 123)
        self.assertNotIn("last_run", agent)

    def dangerous_agent(self, root, provider="claude", permission_mode="bypassPermissions"):
        return {"name": "A", "provider": provider, "workspace": root, "system_prompt": "",
                "startup_prompt": "", "model": "", "permission_mode": permission_mode,
                "advanced_args": [], "overlap": "replace", "timeout_seconds": 7200,
                "enabled": True, "schedule": {"kind": "daily", "hour": 9, "minute": 0}}

    def build(self, arguments, existing=None):
        with mock.patch.object(MCP, "scheduler_request", return_value={"settings": {"timezone": "UTC"}, "loop_mode": "persistent"}):
            return MCP.build_agent(arguments, existing)

    def test_loop_requires_native_persistent_capability_before_save(self):
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(MCP, "scheduler_request", return_value={"settings": {"timezone": "UTC"}}) as request:
            with self.assertRaisesRegex(ValueError, "does not support persistent loops"):
                MCP.build_agent({"name": "Loop", "provider": "codex", "workspace": root,
                                 "schedule": "every 10 minutes", "kind": "loop"})
            request.assert_called_once_with({"op": "get_settings"})

    def test_mcp_cannot_grant_dangerous_permissions(self):
        with tempfile.TemporaryDirectory() as root:
            for provider, mode in (("claude", "bypassPermissions"), ("codex", "dangerous")):
                safe = self.dangerous_agent(root, provider, "")
                with self.subTest(provider=provider), \
                        self.assertRaisesRegex(ValueError, "Dangerous permissions"):
                    self.build({"name": "A", "permission_mode": mode}, safe)
                with self.subTest(provider=provider, create=True), \
                        self.assertRaisesRegex(ValueError, "Dangerous permissions"):
                    self.build({"name": "New", "provider": provider, "workspace": root,
                                "schedule": "daily at 09:00", "permission_mode": mode})

    def test_mcp_can_still_edit_an_agent_a_person_made_dangerous(self):
        with tempfile.TemporaryDirectory() as root:
            agent = self.build({"name": "A", "startup_prompt": "new"}, self.dangerous_agent(root))
            self.assertEqual(agent["permission_mode"], "bypassPermissions")
            self.assertEqual(agent["startup_prompt"], "new")

    def test_mcp_can_lower_a_dangerous_agent(self):
        with tempfile.TemporaryDirectory() as root:
            agent = self.build({"name": "A", "permission_mode": "auto"}, self.dangerous_agent(root))
            self.assertEqual(agent["permission_mode"], "auto")

    def test_raw_dangerous_flags_cannot_go_around_the_permission_setting(self):
        with tempfile.TemporaryDirectory() as root:
            for flag in ("--dangerously-skip-permissions", "--allow-dangerously-skip-permissions",
                         "--dangerously-bypass-approvals-and-sandbox", "--yolo", "-s"):
                with self.subTest(flag=flag), self.assertRaisesRegex(ValueError, "owned flag"):
                    self.build({"name": "A", "advanced_args": [flag]}, self.dangerous_agent(root, "codex", ""))

    def test_permission_mode_must_match_the_provider(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(ValueError, "Invalid permission mode"):
                MCP.validate_agent(self.dangerous_agent(root, "codex", "auto"))

    def test_build_agent_uses_detected_timezone_and_workspace_defaults(self):
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(MCP.os, "getcwd", return_value=root), \
                mock.patch.object(MCP.OMNI, "system_timezone", return_value="Europe/Paris") as timezone, \
                mock.patch.object(MCP.OMNI, "parse_schedule",
                                  return_value=({"kind": "daily"}, 123)) as parse, \
                mock.patch.object(MCP, "scheduler_request",
                                  return_value={"settings": {}}):
            agent = MCP.build_agent({"name": "A", "provider": "codex",
                                     "schedule": "daily at 09:00"})
        timezone.assert_called_once_with()
        parse.assert_called_once_with("daily at 09:00", "Europe/Paris")
        self.assertEqual(agent["workspace"], str(Path(root).resolve()))
        self.assertEqual(agent["timeout_seconds"], 7200)

    def test_serializable_run_requires_a_nonempty_string_session(self):
        self.assertTrue(MCP.serializable_run({"session_id": "abc"})["resumable"])
        for session_id in (None, "", 123):
            with self.subTest(session_id=session_id):
                self.assertFalse(MCP.serializable_run({"session_id": session_id})["resumable"])


class ToolRoutingTests(unittest.TestCase):
    def test_settings_exposes_persistent_loop_capability(self):
        with mock.patch.object(MCP, "scheduler_request", return_value={
                "settings": {"timezone": "UTC"}, "loop_mode": "persistent"}):
            self.assertEqual(MCP.call_tool("get_settings", {})["loop_mode"], "persistent")

    def test_list_agents_filters_and_omits_prompt_bodies(self):
        agents = [
            {"name": "on", "provider": "codex", "enabled": True, "workspace": "/tmp",
             "schedule": {}, "next_run": 1, "overlap": "replace", "timeout_seconds": 2,
             "system_prompt": "secret"},
            {"name": "off", "provider": "claude", "enabled": False, "workspace": "/tmp",
             "schedule": {}, "next_run": 2, "overlap": "parallel", "timeout_seconds": None},
        ]
        with mock.patch.object(MCP, "scheduler_request", return_value={"agents": agents}):
            result = MCP.call_tool("list_agents", {"enabled": False})
        self.assertEqual([agent["name"] for agent in result["agents"]], ["off"])
        self.assertNotIn("system_prompt", result["agents"][0])

    def test_list_runs_filters_clamps_limit_and_marks_resumability(self):
        runs = [{"id": str(index), "state": "failed" if index % 2 else "completed",
                 "session_id": "session" if index == 1 else None} for index in range(6)]
        with mock.patch.object(MCP, "scheduler_request", return_value={"runs": runs}) as request:
            result = MCP.call_tool("list_runs", {"agent": "A", "state": "failed", "limit": 999})
        request.assert_called_once_with({"op": "list_runs", "name": "A", "agent": "A"})
        self.assertEqual([run["id"] for run in result["runs"]], ["1", "3", "5"])
        self.assertTrue(result["runs"][0]["resumable"])

    def test_list_runs_forwards_its_filters_to_the_scheduler(self):
        with mock.patch.object(MCP, "scheduler_request", return_value={"runs": []}) as request:
            MCP.call_tool("list_runs", {"agent": "A", "origin": "manual", "before": 1700000000, "limit": 5})
            MCP.call_tool("list_runs", {})
        self.assertEqual([call.args[0] for call in request.call_args_list], [
            {"op": "list_runs", "name": "A", "agent": "A", "origin": "manual", "before": 1700000000.0,
             "limit": 5},
            {"op": "list_runs", "name": None, "limit": 20}])
        properties = next(tool for tool in MCP.TOOLS if tool["name"] == "list_runs")["inputSchema"]["properties"]
        self.assertEqual(properties["origin"]["enum"], ["scheduled", "manual"])
        self.assertEqual(properties["before"]["type"], "number")
        with mock.patch.object(MCP, "scheduler_request") as request, \
                self.assertRaisesRegex(ValueError, "origin"):
            MCP.call_tool("list_runs", {"origin": "cron"})
        request.assert_not_called()

    def test_preview_schedule_clamps_count_and_stops_after_once(self):
        with mock.patch.object(MCP, "scheduler_request", return_value={
                "settings": {"timezone": "UTC"}}), \
                mock.patch.object(MCP.OMNI, "parse_schedule",
                                  return_value=({"kind": "once"}, 123)), \
                mock.patch.object(MCP.OMNI, "next_calendar", return_value=None) as next_calendar:
            result = MCP.call_tool("preview_schedule", {"schedule": "once", "count": 99})
        self.assertEqual(result["occurrences"], [123])
        next_calendar.assert_called_once_with({"kind": "once"}, "UTC", 123)

    def test_preview_schedule_uses_detected_timezone_and_minimum_count(self):
        with mock.patch.object(MCP, "scheduler_request",
                               return_value={"settings": {}}), \
                mock.patch.object(MCP.OMNI, "system_timezone", return_value="Europe/Paris"), \
                mock.patch.object(MCP.OMNI, "parse_schedule",
                                  return_value=({"kind": "interval"}, 100)) as parse, \
                mock.patch.object(MCP.OMNI, "next_calendar", return_value=200):
            result = MCP.call_tool("preview_schedule", {"schedule": "every hour",
                                                         "count": 0})
        parse.assert_called_once_with("every hour", "Europe/Paris")
        self.assertEqual(result["timezone"], "Europe/Paris")
        self.assertEqual(result["occurrences"], [100])

    def test_list_runs_enforces_minimum_limit_without_filtering(self):
        runs = [{"id": "1", "session_id": "session"},
                {"id": "2", "session_id": None}]
        with mock.patch.object(MCP, "scheduler_request", return_value={"runs": runs}):
            result = MCP.call_tool("list_runs", {"limit": 0})
        self.assertEqual([run["id"] for run in result["runs"]], ["1"])
        self.assertTrue(result["runs"][0]["resumable"])

    def test_simple_tools_map_to_constrained_scheduler_operations(self):
        cases = [
            ("get_agent", {"name": "A"}, {"agent": {"name": "A"}},
             {"op": "show_agent", "name": "A"}, {"agent": {"name": "A"}}),
            ("get_settings", {}, {"settings": {"timezone": "UTC"}},
             {"op": "get_settings"}, {"settings": {"timezone": "UTC"}}),
            ("set_agent_enabled", {"name": "A", "enabled": False}, {"ok": True},
             {"op": "set_enabled", "name": "A", "enabled": False},
             {"name": "A", "enabled": False}),
            ("delete_agent", {"name": "A"}, {"ok": True},
             {"op": "delete_agent", "name": "A"}, {"name": "A", "deleted": True}),
            ("run_agent", {"name": "A"}, {"run_id": "new"},
             {"op": "run_agent", "name": "A"}, {"name": "A", "run_id": "new"}),
            ("stop_run", {"run_id": "old"}, {"ok": True},
             {"op": "stop_run", "run_id": "old"}, {"run_id": "old", "stopped": True}),
            ("resume_run", {"run_id": "old"}, {"run_id": "new"},
             {"op": "resume_run", "run_id": "old"},
             {"source_run_id": "old", "run_id": "new"}),
        ]
        for name, arguments, response, payload, expected in cases:
            with self.subTest(name=name), \
                    mock.patch.object(MCP, "scheduler_request", return_value=response) as request:
                self.assertEqual(MCP.call_tool(name, arguments), expected)
                request.assert_called_once_with(payload)

    def test_notify_user_inside_a_run_attaches_it_and_leaves_workspace_to_the_scheduler(self):
        calls = []

        def scheduler(payload):
            calls.append(payload)
            return {"ok": True, "id": "n1", "severity": "urgent", "run_id": "r1", "agent": "Nightly"}

        with mock.patch.object(MCP, "scheduler_request", side_effect=scheduler), \
                mock.patch.dict(MCP.os.environ, {"OMNI_RUN_ID": "r1"}):
            result = MCP.call_tool("notify_user", {"title": "Deploy blocked", "body": "Needs a key",
                                                   "severity": "urgent", "dedupe_key": "deploy"})
        self.assertEqual(calls, [{"op": "notify", "title": "Deploy blocked", "body": "Needs a key",
                                  "severity": "urgent", "dedupe_key": "deploy", "run_id": "r1"}])
        self.assertEqual(result, {"id": "n1", "stored": True, "severity": "urgent",
                                  "run_id": "r1", "agent": "Nightly"})

    def test_notify_user_outside_a_run_captures_this_project(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            (project / "AGENTS.md").write_text("x")
            nested = project / "src"
            nested.mkdir()
            calls = []

            def scheduler(payload):
                calls.append(payload)
                if payload["op"] == "list_runs":
                    return {"runs": []}
                return {"id": "n2", "severity": "attention"}

            with mock.patch.object(MCP, "scheduler_request", side_effect=scheduler), \
                    mock.patch.dict(MCP.os.environ, {}, clear=True), \
                    mock.patch.object(MCP.OMNI, "process_ancestors", return_value=[123]), \
                    mock.patch.object(MCP.os, "getcwd", return_value=str(nested)):
                result = MCP.call_tool("notify_user", {"title": "Done", "include_diff": False})
        self.assertEqual(calls[-1], {"op": "notify", "title": "Done", "workspace": str(project),
                                     "include_diff": False})
        self.assertEqual(result, {"id": "n2", "stored": True, "severity": "attention"})

    def test_notify_user_rejects_bad_input_before_storing_anything(self):
        for arguments in ({}, {"title": "  "}, {"title": 3}, {"title": "x", "severity": "loud"}):
            with self.subTest(arguments=arguments), \
                    mock.patch.object(MCP, "scheduler_request", return_value={"runs": []}) as request, \
                    mock.patch.dict(MCP.os.environ, {"OMNI_RUN_ID": "r1"}):
                result = MCP.response_for({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                           "params": {"name": "notify_user", "arguments": arguments}})
                self.assertTrue(result["result"]["isError"])
                self.assertFalse([c for c in request.call_args_list if c.args[0].get("op") == "notify"])

    def test_notify_user_is_the_only_notification_tool_and_cannot_read_the_inbox(self):
        names = {tool["name"] for tool in MCP.TOOLS}
        self.assertIn("notify_user", names)
        self.assertFalse(names & {"list_notifications", "read_notifications", "clear_notifications"})
        notify = next(tool for tool in MCP.TOOLS if tool["name"] == "notify_user")
        self.assertEqual(notify["inputSchema"]["required"], ["title"])
        self.assertNotIn("command", notify["inputSchema"]["properties"])
        self.assertFalse(notify["annotations"]["readOnlyHint"])

    def test_create_agent_starts_from_the_persons_defaults(self):
        settings = {"settings": {"timezone": "Europe/Paris", "default_timeout_seconds": 3600,
                                 "agent_defaults": {"provider": "codex", "overlap": "parallel",
                                                    "models": {"codex": "gpt-5", "claude": "opus"},
                                                    "permission_modes": {"codex": "on-request", "claude": "plan"}}},
                    "loop_mode": "persistent"}
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(MCP, "scheduler_request", return_value=settings):
            agent = MCP.build_agent({"name": "A", "schedule": "daily at 09:00", "workspace": directory})
            claude = MCP.build_agent({"name": "B", "provider": "claude", "schedule": "daily at 09:00",
                                      "workspace": directory})
        self.assertEqual((agent["provider"], agent["model"], agent["permission_mode"], agent["overlap"]),
                         ("codex", "gpt-5", "on-request", "parallel"))
        self.assertEqual((claude["provider"], claude["model"], claude["permission_mode"]), ("claude", "opus", "plan"))
        definition = next(tool for tool in MCP.TOOLS if tool["name"] == "create_agent")
        self.assertEqual(definition["inputSchema"]["required"], ["name", "schedule"])

    def test_settings_stay_read_only_over_mcp(self):
        names = {tool["name"] for tool in MCP.TOOLS}
        self.assertFalse({name for name in names if "setting" in name} - {"get_settings"})
        definition = next(tool for tool in MCP.TOOLS if tool["name"] == "get_settings")
        self.assertTrue(definition["annotations"]["readOnlyHint"])

    def test_create_rejects_duplicate_before_building(self):
        with mock.patch.object(MCP, "scheduler_request", return_value={
                "agents": [{"name": "A"}]}), \
                mock.patch.object(MCP, "build_agent") as build, \
                self.assertRaisesRegex(ValueError, "already exists"):
            MCP.call_tool("create_agent", {"name": "A", "provider": "codex",
                                            "schedule": "daily at 09:00"})
        build.assert_not_called()

    def test_create_and_update_persist_then_refetch(self):
        built = {"name": "A"}
        create_responses = iter([{"agents": []}, {"ok": True}, {"agent": {"name": "A"}}])
        with mock.patch.object(MCP, "scheduler_request", side_effect=lambda payload: next(create_responses)) as request, \
                mock.patch.object(MCP, "build_agent", return_value=built):
            self.assertEqual(MCP.call_tool("create_agent", {"name": "A"}),
                             {"agent": {"name": "A"}})
        self.assertEqual([call.args[0]["op"] for call in request.call_args_list],
                         ["list_agents", "create_agent", "show_agent"])

        existing = {"name": "A", "provider": "codex"}
        update_responses = iter([{"agent": existing}, {"ok": True},
                                 {"agent": {"name": "A", "provider": "claude"}}])
        with mock.patch.object(MCP, "scheduler_request", side_effect=lambda payload: next(update_responses)) as request, \
                mock.patch.object(MCP, "build_agent", return_value=built) as build:
            result = MCP.call_tool("update_agent", {"name": "A", "provider": "claude"})
        build.assert_called_once_with({"name": "A", "provider": "claude"}, existing)
        self.assertEqual(result["agent"]["provider"], "claude")
        self.assertEqual([call.args[0]["op"] for call in request.call_args_list],
                         ["show_agent", "update_agent", "show_agent"])

    def test_get_run_returns_the_schedulers_report_read_only(self):
        report = {"id": "r1", "agent": "A", "summary": None, "summary_state": "pending", "diff_files": []}
        with mock.patch.object(MCP, "scheduler_request", return_value={"ok": True, "run": report}) as request:
            self.assertEqual(MCP.call_tool("get_run", {"id": " r1 "}), {"run": report})
        request.assert_called_once_with({"op": "get_run", "id": "r1"})
        definition = next(tool for tool in MCP.TOOLS if tool["name"] == "get_run")
        self.assertTrue(definition["annotations"]["readOnlyHint"])
        self.assertEqual(definition["inputSchema"]["required"], ["id"])
        self.assertIn("untrusted", definition["description"])
        self.assertNotIn("run_diff", {tool["name"] for tool in MCP.TOOLS})

    def test_get_run_errors_are_tool_errors(self):
        with mock.patch.object(MCP, "scheduler_request", side_effect=ValueError("Run not found")):
            response = MCP.response_for({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                         "params": {"name": "get_run", "arguments": {"id": "nope"}}})
        self.assertTrue(response["result"]["isError"])
        self.assertIn("Run not found", response["result"]["content"][0]["text"])
        with mock.patch.object(MCP, "scheduler_request") as request:
            for arguments in ({}, {"id": " "}, {"id": 3}):
                with self.subTest(arguments=arguments), self.assertRaisesRegex(ValueError, "run ID"):
                    MCP.call_tool("get_run", arguments)
        request.assert_not_called()

    def test_permission_modes_are_the_ones_an_agent_file_may_hold(self):
        self.assertIs(MCP.PERMISSION_MODES, MCP.OMNI.STORED_PERMISSION_MODES)
        self.assertIn("auto", MCP.PERMISSION_MODES["claude"])
        self.assertFalse({"auto-mode", "default"} & (MCP.PERMISSION_MODES["claude"] | MCP.PERMISSION_MODES["codex"]))
        self.assertNotIn("run_screen", {tool["name"] for tool in MCP.TOOLS})

    def test_get_run_is_taught_in_the_instructions_and_guide(self):
        self.assertIn("get_run", MCP.INSTRUCTIONS)
        self.assertIn("## Run reports: `get_run`", MCP.GUIDE)

    def test_agents_carry_validated_appearance(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = {"name": "A", "provider": "codex", "workspace": directory, "group": " Work / Code ",
                     "icon": "sf:hammer", "color": ""}
            validated = MCP.validate_agent(agent)
            self.assertEqual((validated["group"], validated["icon"]), ("Work/Code", "sf:hammer"))
            self.assertNotIn("color", validated)
            with self.assertRaisesRegex(ValueError, "Icon"):
                MCP.validate_agent({**agent, "icon": "sf:Trash"})
        properties = next(tool for tool in MCP.TOOLS if tool["name"] == "update_agent")["inputSchema"]["properties"]
        self.assertEqual(properties["color"]["enum"], [""] + list(MCP.OMNI.AGENT_COLORS))
        for key in ("group", "icon"):
            self.assertIn(key, properties)

    def test_update_agent_can_clear_appearance(self):
        existing = {"name": "A", "provider": "codex", "workspace": "/tmp", "group": "Work", "color": "red",
                    "schedule": {"phrase": "daily"}}
        with mock.patch.object(MCP, "scheduler_request", return_value={"settings": {"timezone": "UTC"}}):
            agent = MCP.build_agent({"name": "A", "group": "", "icon": "emoji:🚀"}, existing)
        self.assertNotIn("group", agent)
        self.assertEqual((agent["icon"], agent["color"]), ("emoji:🚀", "red"))

    def test_rename_agent_tool(self):
        with mock.patch.object(MCP, "scheduler_request", return_value={"ok": True, "agent": {"name": "B"}}) as request:
            self.assertEqual(MCP.call_tool("rename_agent", {"name": "A", "new_name": " B "}), {"agent": {"name": "B"}})
        request.assert_called_once_with({"op": "rename_agent", "name": "A", "new_name": "B"})
        with mock.patch.object(MCP, "scheduler_request") as request, self.assertRaisesRegex(ValueError, "new_name"):
            MCP.call_tool("rename_agent", {"name": "A", "new_name": " "})
        request.assert_not_called()
        definition = next(tool for tool in MCP.TOOLS if tool["name"] == "rename_agent")
        self.assertEqual(definition["inputSchema"]["required"], ["name", "new_name"])
        self.assertIn("rename_agent", MCP.INSTRUCTIONS)

    def test_list_agents_shows_appearance_only_when_set(self):
        agents = [{"name": "A", "group": "Work", "icon": "sf:bell", "system_prompt": "x"}, {"name": "B"}]
        with mock.patch.object(MCP, "scheduler_request", return_value={"agents": agents}):
            result = MCP.call_tool("list_agents", {})
        self.assertEqual((result["agents"][0]["group"], result["agents"][0]["icon"]), ("Work", "sf:bell"))
        self.assertNotIn("group", result["agents"][1])

    def test_unknown_tool_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown Omni tool"):
            MCP.call_tool("shell", {})


class StdioServerTests(unittest.TestCase):
    class Input:
        def __init__(self, value):
            self.buffer = io.BytesIO(value)

    def test_main_survives_malformed_frames_and_ignores_notifications(self):
        source = self.Input(
            b"not json\n"
            b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
            b'{"jsonrpc":"2.0","id":7,"method":"ping"}\n')
        output = io.StringIO()
        with mock.patch.object(MCP.sys, "stdin", source), \
                mock.patch.object(MCP.sys, "stdout", output):
            MCP.main()
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(responses), 2)
        self.assertEqual(responses[0]["error"]["code"], -32700)
        self.assertEqual(responses[1], {"jsonrpc": "2.0", "id": 7, "result": {}})


if __name__ == "__main__":
    unittest.main()
