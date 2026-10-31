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
                mock.patch.object(MCP.time, "sleep") as sleep:
            result = MCP.scheduler_request({"op": "ping"})
        self.assertTrue(result["ok"])
        launch.assert_called_once_with(["open", "-g", "-b", "com.omniterm.app"],
                                       stdout=MCP.subprocess.DEVNULL,
                                       stderr=MCP.subprocess.DEVNULL)
        sleep.assert_called_once_with(0.4)
        first.close.assert_called_once_with()
        second.close.assert_called_once_with()

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

    def test_invalid_scheduler_responses_exhaust_retries_and_close_every_socket(self):
        clients = []
        for _ in range(8):
            client = mock.Mock()
            client.recv.side_effect = [b'{"ok":false,"error":"rejected"}', b""]
            clients.append(client)
        with mock.patch.object(MCP.socket, "socket", side_effect=clients), \
                mock.patch.object(MCP.subprocess, "run") as launch, \
                mock.patch.object(MCP.time, "sleep") as sleep, \
                self.assertRaisesRegex(RuntimeError, "rejected"):
            MCP.scheduler_request({"op": "bad"})
        launch.assert_called_once()
        self.assertEqual(sleep.call_count, 7)
        for client in clients:
            client.close.assert_called_once_with()


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
        with mock.patch.object(MCP, "scheduler_request", return_value={"settings": {"timezone": "UTC"}}):
            return MCP.build_agent(arguments, existing)

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
        request.assert_called_once_with({"op": "list_runs", "name": "A"})
        self.assertEqual([run["id"] for run in result["runs"]], ["1", "3", "5"])
        self.assertTrue(result["runs"][0]["resumable"])

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
