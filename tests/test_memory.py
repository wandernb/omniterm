import copy
import importlib.util
from pathlib import Path
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location(
    "omni_memory", Path(__file__).parents[1] / "helpers/omni_memory.py")
MEMORY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MEMORY)


class NativeMemoryService:
    def __init__(self, agents=None):
        self.agents = copy.deepcopy(agents or {})
        self.requests = []
        self.reject_update = False

    def __call__(self, request):
        self.requests.append(copy.deepcopy(request))
        if request["op"] == "list_agents":
            return {"ok": True, "agents": copy.deepcopy(list(self.agents.values()))}
        agent = request["agent"]
        if request["op"] == "update_agent":
            current = self.agents[agent["name"]]
            expected = {key: value for key, value in current.items()
                        if key not in ("next_run", "last_run", "timezone")}
            if self.reject_update or request["expected_memory_definition"] != expected:
                return {"ok": False, "error": "The memory agent changed concurrently"}
        elif agent["name"] in self.agents:
            return {"ok": False, "error": "Agent already exists"}
        self.agents[agent["name"]] = copy.deepcopy(agent)
        return {"ok": True, "agent": copy.deepcopy(agent)}


class MemoryAutomationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        self.native = NativeMemoryService()

    def configure(self, **arguments):
        return MEMORY.configure_memory(self.native, workspace=self.workspace, clock=lambda: 1000, **arguments)

    def test_install_routes_scheduled_inference_through_native_state_owner(self):
        result = self.configure(provider="claude")
        agent = self.native.agents[MEMORY.AGENT_NAME]
        self.assertTrue(result["changed"])
        self.assertEqual([request["op"] for request in self.native.requests], ["list_agents", "create_agent"])
        self.assertEqual(agent["provider"], "claude")
        self.assertEqual(agent["schedule"]["interval_seconds"], 900)
        self.assertEqual(agent["next_run"], 1900)
        self.assertEqual(agent["session_policy"], "fresh")
        self.assertEqual(agent["overlap"], "replace")
        self.assertEqual(agent["permission_mode"], "auto")
        self.assertEqual(agent["advanced_args"], [])
        self.assertTrue(MEMORY.is_managed_memory_agent(agent))

    def test_repeated_install_preserves_schedule_without_another_mutation(self):
        self.configure(provider="codex")
        self.native.requests.clear()
        result = self.configure()
        self.assertFalse(result["changed"])
        self.assertEqual([request["op"] for request in self.native.requests], ["list_agents"])
        self.assertEqual(result["agent"]["next_run"], 1900)
        self.assertEqual(result["agent"]["provider"], "codex")

    def test_codex_memory_agent_runs_at_low_reasoning_effort(self):
        self.configure(provider="codex")
        agent = self.native.agents[MEMORY.AGENT_NAME]
        self.assertEqual(agent["advanced_args"], ["-c", 'model_reasoning_effort="low"'])
        self.configure(provider="claude")
        self.assertEqual(self.native.agents[MEMORY.AGENT_NAME]["advanced_args"], [])

    def test_pause_and_provider_changes_keep_atomic_ownership_guard(self):
        self.configure(provider="claude")
        previous = copy.deepcopy(self.native.agents[MEMORY.AGENT_NAME])
        result = self.configure(provider="codex", enabled=False)
        self.assertTrue(result["changed"])
        self.assertFalse(result["agent"]["enabled"])
        self.assertIsNone(result["agent"]["next_run"])
        update = self.native.requests[-1]
        self.assertEqual(update["op"], "update_agent")
        self.assertEqual(update["memory_owner"], MEMORY.MANAGED_ID)
        self.assertEqual(update["expected_memory_definition"],
                         {key: value for key, value in previous.items() if key != "next_run"})

    def test_new_agent_defaults_to_codex_on_its_cheap_model(self):
        self.configure()
        agent = self.native.agents[MEMORY.AGENT_NAME]
        self.assertEqual(agent["provider"], "codex")
        self.assertEqual(agent["model"], MEMORY.MEMORY_CODEX_MODEL)
        self.assertEqual(agent["permission_mode"], "")

    def test_existing_model_is_kept_unless_a_new_one_is_given(self):
        legacy = {**MEMORY.agent_definition("codex", self.workspace), "model": ""}
        self.native = NativeMemoryService({MEMORY.AGENT_NAME: legacy})
        self.assertFalse(self.configure()["changed"])
        self.configure(model="gpt-6-sol")
        self.assertEqual(self.native.agents[MEMORY.AGENT_NAME]["model"], "gpt-6-sol")
        self.configure(enabled=False)
        self.assertEqual(self.native.agents[MEMORY.AGENT_NAME]["model"], "gpt-6-sol")

    def test_provider_switch_drops_the_other_providers_model(self):
        self.configure(provider="codex")
        self.configure(provider="claude")
        self.assertEqual(self.native.agents[MEMORY.AGENT_NAME]["model"], "")
        self.configure(provider="codex")
        self.assertEqual(self.native.agents[MEMORY.AGENT_NAME]["model"], MEMORY.MEMORY_CODEX_MODEL)

    def test_unrelated_or_edited_same_name_agent_is_never_overwritten(self):
        for existing in ({"name": MEMORY.AGENT_NAME, "startup_prompt": "Unrelated work"},
                         {**MEMORY.agent_definition("claude", self.workspace), "system_prompt": "Changed by person"}):
            with self.subTest(existing=existing):
                native = NativeMemoryService({MEMORY.AGENT_NAME: existing})
                with self.assertRaisesRegex(ValueError, "unrelated agent"):
                    MEMORY.configure_memory(native, workspace=self.workspace)
                self.assertEqual(native.agents[MEMORY.AGENT_NAME], existing)
                self.assertEqual([request["op"] for request in native.requests], ["list_agents"])

    def test_concurrent_change_is_reported_without_retrying_an_overwrite(self):
        self.configure()
        before = copy.deepcopy(self.native.agents)
        self.native.reject_update = True
        with self.assertRaisesRegex(ValueError, "changed concurrently"):
            self.configure(enabled=False)
        self.assertEqual(self.native.agents, before)
        self.assertEqual(self.native.requests[-1]["op"], "update_agent")

    def test_legacy_v1_agent_is_upgraded_in_place(self):
        legacy = {**MEMORY.agent_definition("claude", self.workspace),
                  "system_prompt": MEMORY.LEGACY_SYSTEM_PROMPT_V1,
                  "startup_prompt": MEMORY.LEGACY_STARTUP_PROMPT_V1, "run_budget_seconds": 180}
        native = NativeMemoryService({MEMORY.AGENT_NAME: legacy})
        result = MEMORY.configure_memory(native, workspace=self.workspace, clock=lambda: 1000)
        self.assertTrue(result["changed"])
        upgraded = native.agents[MEMORY.AGENT_NAME]
        self.assertEqual(upgraded["system_prompt"], MEMORY.SYSTEM_PROMPT)
        self.assertEqual(upgraded["run_budget_seconds"], 600)
        self.assertEqual(native.requests[-1]["op"], "update_agent")

    def test_single_batch_claude_agent_is_upgraded_to_codex_multi_batch_consolidation(self):
        legacy = {**MEMORY.agent_definition("claude", self.workspace),
                  "system_prompt": MEMORY.LEGACY_SYSTEM_PROMPT_V3,
                  "startup_prompt": MEMORY.LEGACY_STARTUP_PROMPT_V3}
        native = NativeMemoryService({MEMORY.AGENT_NAME: legacy})
        MEMORY.configure_memory(native, provider="codex", workspace=self.workspace, clock=lambda: 1000)
        upgraded = native.agents[MEMORY.AGENT_NAME]
        self.assertEqual((upgraded["provider"], upgraded["model"]), ("codex", MEMORY.MEMORY_CODEX_MODEL))
        self.assertEqual(upgraded["startup_prompt"], MEMORY.STARTUP_PROMPT)
        self.assertIn("three batches", upgraded["system_prompt"])
        self.assertIn("up to three bounded batches", upgraded["startup_prompt"])

    def test_live_v4_agent_is_recognised_and_upgraded_to_the_update_contract(self):
        live = {**MEMORY.agent_definition("codex", self.workspace),
                "system_prompt": MEMORY.LEGACY_SYSTEM_PROMPT_V4, "startup_prompt": MEMORY.LEGACY_STARTUP_PROMPT_V4}
        self.assertTrue(MEMORY.is_managed_memory_agent(live))
        native = NativeMemoryService({MEMORY.AGENT_NAME: live})
        result = MEMORY.configure_memory(native, workspace=self.workspace, clock=lambda: 1000)
        self.assertTrue(result["changed"])
        upgraded = native.agents[MEMORY.AGENT_NAME]
        self.assertEqual((upgraded["system_prompt"], upgraded["startup_prompt"]),
                         (MEMORY.SYSTEM_PROMPT, MEMORY.STARTUP_PROMPT))
        self.assertNotEqual(MEMORY.SYSTEM_PROMPT, MEMORY.LEGACY_SYSTEM_PROMPT_V4)
        self.assertTrue(MEMORY.LEGACY_SYSTEM_PROMPT_V4.startswith(MEMORY.SYSTEM_MARKER + "\n"))
        self.assertFalse(MEMORY.configure_memory(native, workspace=self.workspace, clock=lambda: 1000)["changed"])

    def test_prompt_decides_add_update_or_noop_against_similar_memories(self):
        prompt = MEMORY.SYSTEM_PROMPT
        for phrase in ("ADD", "UPDATE", "NOOP", "Prefer UPDATE to ADD", "similar existing memories",
                       "keep_sources true", "repeated evidence", "role user", "standing rule",
                       "Do not extract secrets", "[redacted]"):
            self.assertIn(phrase, prompt)
        self.assertIn("ADD, UPDATE or NOOP", MEMORY.STARTUP_PROMPT)

    def test_prompt_keeps_native_marker_and_asks_for_serial_typed_work(self):
        self.assertTrue(MEMORY.SYSTEM_PROMPT.startswith(MEMORY.SYSTEM_MARKER + "\n"))
        self.assertTrue(MEMORY.LEGACY_SYSTEM_PROMPT_V1.startswith(MEMORY.SYSTEM_MARKER + "\n"))
        for phrase in ("one tool call at a time", "dismiss_memory_candidates", "memory_type", "procedural",
                       "a claim, not a verified outcome"):
            self.assertIn(phrase, MEMORY.SYSTEM_PROMPT)
        self.assertNotIn("dismiss_memory_candidate,", MEMORY.SYSTEM_PROMPT)

    def test_invalid_configuration_does_not_create_an_agent(self):
        for arguments in ({"provider": "unknown", "workspace": self.workspace},
                          {"provider": "claude", "workspace": self.workspace / "missing"},
                          {"provider": "claude", "workspace": self.workspace, "enabled": "yes"},
                          {"provider": "codex", "workspace": self.workspace, "model": "--dangerously-bypass"},
                          {"provider": "codex", "workspace": self.workspace, "model": "gpt 6"},
                          {"provider": "codex", "workspace": self.workspace, "model": "x" * 101},
                          {"provider": "codex", "workspace": self.workspace, "model": 5}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                MEMORY.configure_memory(self.native, **arguments)
        self.assertFalse(self.native.agents)
        self.assertTrue(all(request["op"] == "list_agents" for request in self.native.requests))


if __name__ == "__main__":
    unittest.main()
