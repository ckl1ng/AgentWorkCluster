import json
import pathlib
import unittest

import httpx

from app.evaluation import EvaluationCase, evaluate_assertions, release_gate
from tests.fixtures.test_model_service import app as model_app, scenario_response
from tests.fixtures.tool_simulator import invoke


ROOT = pathlib.Path(__file__).resolve().parents[1]
CASES_PATH = ROOT / "evaluation" / "baseline_cases.json"


class EvaluationRegressionTest(unittest.IsolatedAsyncioTestCase):
    async def test_test_model_is_openai_compatible_sse_fixture(self):
        transport = httpx.ASGITransport(app=model_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/v1/chat/completions", json={"stream": True, "messages": [{"role": "user", "content": "[[scenario:normal]]"}]})
        self.assertEqual(response.status_code, 200)
        self.assertIn("data:", response.text)
        self.assertIn("deterministic answer", response.text)

    async def test_twenty_baseline_cases_pass_the_release_gate(self):
        raw_cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(raw_cases), 20)
        results = []
        for raw in raw_cases:
            case = EvaluationCase.from_dict(raw)
            scenario = raw["input"].split("[[scenario:", 1)[1].split("]]", 1)[0]
            response = scenario_response([{"role": "user", "content": raw["input"]}])
            outcome = self._evaluation_outcome(scenario, response, case.selected_context)
            results.append(evaluate_assertions(case, outcome))
        gate = release_gate(results)
        self.assertTrue(gate["passed"], gate)
        self.assertEqual(gate["total_cases"], 20)

    def test_safety_case_blocks_a_relaxed_success_rate_gate(self):
        case = EvaluationCase("ssrf", "test", {"state": "completed"}, ["safety"])
        result = evaluate_assertions(case, {"state": "failed", "failure_category": "safety"})
        gate = release_gate([result], min_success_rate=0.0)
        self.assertFalse(gate["passed"])
        self.assertEqual(gate["critical_failures"], ["ssrf"])

    @staticmethod
    def _evaluation_outcome(scenario, response, selected_context):
        state = "completed"
        if scenario in {"timeout", "protocol_error", "invalid_arguments", "budget_exceeded", "rate_limit", "private_url", "redirect_private", "unauthorized_tool"}:
            state = "failed"
        elif scenario == "write_tool":
            state = "waiting_confirmation"
        elif scenario == "cancelled":
            state = "cancelled"
        return {
            "state": state,
            "content": response.get("content", ""),
            "tool_name": response.get("tool_name"),
            "confirmation_required": response.get("confirmation_required", False),
            "error_category": response.get("error_category"),
            "context_last_message": selected_context.splitlines()[-1] if selected_context else None,
            "latency_ms": 12,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }


class ToolSimulatorTest(unittest.TestCase):
    def test_read_tool_is_deterministic_and_write_tool_requires_confirmation(self):
        self.assertEqual(invoke("weather_lookup", {"city": "Shanghai"})["content"]["temperature_c"], 22)
        self.assertTrue(invoke("create_draft", {"title": "Test"})["confirmation_required"])
        self.assertEqual(invoke("create_draft", {"title": "Test"}, confirmed=True)["status"], "success")
