import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet

from app import main
from app.main import AgentStore


def agent_payload():
    return {
        "name": "Weather agent", "base_url": "https://model.example/v1", "api_key": "model-secret",
        "model_id": "test-model", "system_prompt": "system", "memory_enabled": False,
    }


class BuiltinWeatherToolTest(unittest.TestCase):
    def test_builtin_catalog_and_frozen_snapshot_never_store_weather_key(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AgentStore(directory + "/agent.db", Fernet.generate_key().decode("ascii"))
            agent = store.create_agent(42, agent_payload())
            weather = next(tool for tool in store.list_tools(42) if tool["name"] == "amap_weather")

            self.assertTrue(weather["builtin"])
            self.assertEqual(weather["config"], {"builtin": "amap_weather"})
            self.assertNotIn("key", str(weather))

            store.set_agent_tools(agent["id"], 42, [weather["id"]])
            conversation = store.create_conversation(agent["id"], 42)
            run = store.create_run(conversation["id"], 42, "查询天气")
            snapshot = store.run_snapshot(run["id"])
            self.assertEqual(snapshot["tools"][0]["config"], {"builtin": "amap_weather"})
            self.assertNotIn("AMAP_WEATHER_API_KEY", str(snapshot))

    def test_missing_environment_key_returns_clear_error_without_request(self):
        tool = {"kind": "http", "config": {"builtin": "amap_weather"}}
        with patch.dict(os.environ, {"AMAP_WEATHER_API_KEY": ""}):
            with self.assertRaisesRegex(RuntimeError, "AMAP_WEATHER_API_KEY"):
                asyncio.run(main.execute_tool(tool, {"city": "310000"}))

    def test_runtime_reads_key_only_for_the_fixed_outbound_request(self):
        tool = {"kind": "http", "config": {"builtin": "amap_weather"}}
        response = {"status": "ok", "content": "weather"}
        with patch.dict(os.environ, {"AMAP_WEATHER_API_KEY": "test-weather-key"}):
            with patch("app.harness.execute_http_tool", new=AsyncMock(return_value=response)) as execute_http:
                result = asyncio.run(main.execute_tool(tool, {"city": "310000", "extensions": "all"}))

        self.assertEqual(result, response)
        request_tool, arguments, allow_http, response_limit = execute_http.await_args.args
        self.assertEqual(request_tool["config"]["url"], "https://restapi.amap.com/v3/weather/weatherInfo")
        self.assertEqual(arguments, {
            "key": "test-weather-key", "city": "310000", "extensions": "all", "output": "JSON",
        })
        self.assertEqual(allow_http, main.settings.allow_http)
        self.assertEqual(response_limit, main.settings.tool_response_limit)


if __name__ == "__main__":
    unittest.main()
