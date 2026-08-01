import importlib.machinery
import unittest
from pathlib import Path
from unittest import mock


MCP = importlib.machinery.SourceFileLoader(
    "omni_mcp", str(Path(__file__).parents[1] / "helpers/omni-mcp")
).load_module()


class MCPProtocolTests(unittest.TestCase):
    def test_initialize_negotiates_supported_version(self):
        response = MCP.response_for({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        })
        self.assertEqual(response["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("tools", response["result"]["capabilities"])

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


if __name__ == "__main__":
    unittest.main()
