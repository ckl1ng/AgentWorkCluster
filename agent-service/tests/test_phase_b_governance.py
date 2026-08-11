import tempfile
import unittest

from cryptography.fernet import Fernet

from app.harness import tool_declarations
from app.main import AgentStore


def agent_payload(**overrides):
    value = {
        "name": "Phase B", "base_url": "https://model.example/v1", "api_key": "model-secret",
        "model_id": "test-model", "system_prompt": "system", "memory_enabled": True,
    }
    value.update(overrides)
    return value


class PhaseBGovernanceTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = AgentStore(self.directory.name + "/agent.db", Fernet.generate_key().decode("ascii"))
        self.agent = self.store.create_agent(11, agent_payload())
        self.conversation = self.store.create_conversation(self.agent["id"], 11)

    def tearDown(self):
        self.directory.cleanup()

    def test_write_tool_requires_an_approval_bound_to_its_arguments(self):
        tool = self.store.create_tool(11, {
            "name": "create_draft", "description": "create", "kind": "http",
            "config": {"url": "https://api.example.test/drafts", "method": "POST", "parameter_locations": {"title": "body"}},
            "input_schema": {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]},
            "side_effect": "write", "confirmation_mode": "per_call", "rate_limit_per_run": 1,
        })
        self.store.set_agent_tools(self.agent["id"], 11, [tool["id"]])
        run = self.store.create_run(self.conversation["id"], 11, "create a draft")
        confirmation = self.store.create_confirmation(run["id"], "call-1", tool["name"], {"title": "one"}, {"messages": [], "call": {}, "tool": {}})
        self.assertNotIn("one", str(self.store.db.execute("SELECT arguments FROM tool_confirmations").fetchone()[0]))
        with self.assertRaises(ValueError):
            self.store.decide_confirmation(confirmation["id"], run["id"], 11, "0" * 64, True)
        approved = self.store.decide_confirmation(confirmation["id"], run["id"], 11, confirmation["arguments_hash"], True)
        self.assertEqual(approved["state"], "approved")

    def test_memory_is_explicit_explainable_and_conflicts_are_not_silent(self):
        first = self.store.create_memory(self.agent["id"], 11, {"content": "使用中文回答", "kind": "preference"})
        second = self.store.create_memory(self.agent["id"], 11, {"content": "Use English", "kind": "preference"})
        self.assertEqual(first["conflict_state"], "active")
        self.assertEqual(second["conflict_state"], "conflicted")
        memories = self.store.list_memories(self.agent["id"], 11)
        self.assertEqual({item["conflict_state"] for item in memories}, {"conflicted"})
        self.assertTrue(self.store.delete_memory(second["id"], self.agent["id"], 11))

    def test_legacy_unconfirmed_writes_are_never_declared_to_a_model(self):
        tools = [
            {"name": "read", "description": "", "config": {"method": "GET"}, "input_schema": {"type": "object"}},
            {"name": "unsafe", "description": "", "config": {"method": "POST"}, "input_schema": {"type": "object"}},
            {"name": "write", "description": "", "config": {"method": "POST"}, "input_schema": {"type": "object"},
             "side_effect": "write", "confirmation_mode": "per_call"},
        ]
        self.assertEqual([item["function"]["name"] for item in tool_declarations(tools)], ["read", "write"])

    def test_builtin_tools_are_optional_assignments(self):
        tools = self.store.list_tools(11)
        builtin_ids = {tool["id"] for tool in tools if tool["builtin"]}

        self.assertEqual(len(builtin_ids), 3)
        self.assertEqual(self.store.tool_ids(self.agent["id"]), [])
        self.store.set_agent_tools(self.agent["id"], 11, list(builtin_ids))
        self.assertEqual(set(self.store.tool_ids(self.agent["id"])), builtin_ids)
        self.store.set_agent_tools(self.agent["id"], 11, [])
        self.assertEqual(self.store.tool_ids(self.agent["id"]), [])

    def test_evaluation_comparison_reports_a_regression(self):
        baseline = self.store.create_evaluation_run("phase-b")
        candidate = self.store.create_evaluation_run("phase-b")
        self.store.record_evaluation_result(baseline["id"], "case-a", True, 1, 1, {})
        self.store.record_evaluation_result(candidate["id"], "case-a", False, 0, 1, {}, failure_category="policy")
        comparison = self.store.compare_evaluation_runs(baseline["id"], candidate["id"])
        self.assertEqual(comparison["regressions"], ["case-a"])
        self.assertFalse(comparison["passed"])


if __name__ == "__main__":
    unittest.main()
