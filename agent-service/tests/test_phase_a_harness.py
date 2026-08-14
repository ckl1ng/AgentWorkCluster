import asyncio
import json
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet

from app.harness import ModelTurn, _stdio_request, execute_stdio_mcp_tool, prepare_context, tool_declarations
from app.main import AgentStore
from app import main


def agent_payload(**overrides):
    data = {
        "name": "Phase A", "base_url": "https://model.example/v1", "api_key": "model-secret",
        "model_id": "test-model", "system_prompt": "private system instructions",
        "run_policy": {"max_tool_calls": 2, "max_concurrent_runs": 1,
                       "daily_token_budget": 0, "monthly_token_budget": 0, "context_window": 2048},
    }
    data.update(overrides)
    return data


class PhaseAStorageTest(unittest.TestCase):
    def test_sensitive_runtime_columns_never_keep_plaintext(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AgentStore(directory + "/agent.db", Fernet.generate_key().decode("ascii"))
            agent = store.create_agent(9, agent_payload())
            conversation = store.create_conversation(agent["id"], 9)
            run = store.create_run(conversation["id"], 9, "private user task")
            store.try_start_run(run["id"])
            store.add_event(run["id"], "agent.message.delta", {"content": "private model delta"})
            store.add_message(conversation["id"], run["id"], "assistant", "private final answer", 0)
            store.update_run(run["id"], "completed", final_content="private final answer")

            agent_row = store.db.execute(
                "SELECT system_prompt, encrypted_system_prompt FROM agents WHERE id = ?", (agent["id"],)
            ).fetchone()
            message_rows = store.db.execute(
                "SELECT content, content_encrypted FROM messages WHERE conversation_id = ?", (conversation["id"],)
            ).fetchall()
            run_row = store.db.execute(
                "SELECT final_content, final_content_encrypted FROM runs WHERE id = ?", (run["id"],)
            ).fetchone()
            trace_row = store.db.execute(
                "SELECT payload, payload_encrypted, redacted_payload FROM trace_events WHERE run_id = ?", (run["id"],)
            ).fetchone()
            version_row = store.db.execute(
                "SELECT snapshot, snapshot_encrypted FROM agent_versions WHERE agent_id = ?", (agent["id"],)
            ).fetchone()

            self.assertEqual(agent_row["system_prompt"], "")
            self.assertNotIn(b"private system", agent_row["encrypted_system_prompt"])
            self.assertTrue(all(row["content"] == "" for row in message_rows))
            self.assertTrue(all(b"private" not in row["content_encrypted"] for row in message_rows))
            self.assertEqual(run_row["final_content"], "")
            self.assertNotIn(b"private final", run_row["final_content_encrypted"])
            self.assertEqual(trace_row["payload"], "{}")
            self.assertNotIn("private model", trace_row["redacted_payload"])
            self.assertEqual(version_row["snapshot"], "{}")
            self.assertNotIn(b"private system", version_row["snapshot_encrypted"])

    def test_concurrency_policy_rejects_a_second_active_run(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AgentStore(directory + "/agent.db", Fernet.generate_key().decode("ascii"))
            agent = store.create_agent(9, agent_payload())
            conversation = store.create_conversation(agent["id"], 9)
            first = store.create_run(conversation["id"], 9, "first")
            second = store.create_run(conversation["id"], 9, "second")
            self.assertEqual(first["state"], "queued")
            self.assertIn("并发", second["error"])

    def test_conversation_history_excludes_durable_tool_context(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AgentStore(directory + "/agent.db", Fernet.generate_key().decode("ascii"))
            agent = store.create_agent(9, agent_payload())
            conversation = store.create_conversation(agent["id"], 9)
            run = store.create_run(conversation["id"], 9, "look this up")
            store.add_message(conversation["id"], run["id"], "tool", '{"private":"raw result"}', 0)
            store.add_message(conversation["id"], run["id"], "assistant", "final answer", 0)

            history = store.conversation_messages(conversation["id"], 9)

            self.assertEqual([message["role"] for message in history["messages"]], ["user", "assistant"])
            self.assertNotIn("raw result", str(history["messages"]))

    def test_recovery_claims_running_work_but_fresh_duplicates_do_not(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AgentStore(directory + "/agent.db", Fernet.generate_key().decode("ascii"))
            agent = store.create_agent(9, agent_payload())
            conversation = store.create_conversation(agent["id"], 9)
            run = store.create_run(conversation["id"], 9, "recover")
            self.assertTrue(store.try_start_run(run["id"]))
            self.assertFalse(store.try_start_run(run["id"]))
            self.assertTrue(store.try_start_run(run["id"], recover=True))
            current = store.get_run(run["id"], 9)
            self.assertEqual(current["attempt"], 2)

    def test_redis_outage_keeps_the_durable_run_queued(self):
        class UnavailableRedis:
            async def xadd(self, *_args, **_kwargs):
                raise ConnectionError("redis unavailable")

            async def aclose(self):
                pass

        with tempfile.TemporaryDirectory() as directory:
            previous_store, previous_url = main.store, main.settings.redis_url
            try:
                store = AgentStore(directory + "/agent.db", Fernet.generate_key().decode("ascii"))
                main.store, main.settings.redis_url = store, "redis://unavailable"
                agent = store.create_agent(9, agent_payload())
                conversation = store.create_conversation(agent["id"], 9)
                run = store.create_run(conversation["id"], 9, "durable")
                from unittest.mock import patch
                with patch("redis.asyncio.Redis.from_url", return_value=UnavailableRedis()):
                    asyncio.run(main.enqueue_run(run["id"]))
                self.assertEqual(store.get_run(run["id"], 9)["state"], "queued")
                self.assertEqual(len(store.pending_outbox_events()), 1)
            finally:
                main.store, main.settings.redis_url = previous_store, previous_url


class PhaseAHarnessTest(unittest.TestCase):
    def test_stdio_mcp_error_result_is_not_reported_as_success(self):
        tool = {
            "name": "fetch", "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            "config": {"command": "tool", "remote_tool_name": "fetch"},
        }
        response = {"result": {"isError": True, "content": [{"type": "text", "text": "fetch failed"}]}}
        with patch("app.harness._execute_mcp_stdio", new=AsyncMock(return_value=response)):
            with self.assertRaisesRegex(RuntimeError, "fetch failed"):
                asyncio.run(execute_stdio_mcp_tool(tool, {}, 1024))

    def test_stdio_request_uses_jsonl_framing(self):
        class Writer:
            def __init__(self):
                self.data = b""

            def write(self, data):
                self.data += data

            async def drain(self):
                pass

        class Reader:
            async def readline(self):
                return b'{"jsonrpc":"2.0","id":1,"result":{}}\n'

        class Process:
            def __init__(self):
                self.stdin = Writer()
                self.stdout = Reader()

        process = Process()
        result = asyncio.run(_stdio_request(process, {"jsonrpc": "2.0", "id": 1, "method": "initialize"}, 1))
        self.assertEqual(result["id"], 1)
        self.assertEqual(process.stdin.data, b'{"jsonrpc":"2.0","id":1,"method":"initialize"}\n')

    def test_context_budget_keeps_the_newest_task_and_reports_trimming(self):
        history = [{"role": "user", "content": "old-{} ".format(index) * 200} for index in range(10)]
        history.append({"role": "user", "content": "newest task must survive"})
        messages, manifest = prepare_context("system " * 2000, history, {"context_window": 2048}, 512, 0)
        self.assertIn("newest task must survive", messages[-1]["content"])
        self.assertGreater(manifest["history_dropped"], 0)
        self.assertTrue(manifest["system_prompt_truncated"])

    def test_segmented_tool_calls_are_merged_by_index(self):
        turn = ModelTurn()
        turn.merge({"choices": [{"delta": {"tool_calls": [{
            "index": 0, "id": "call-", "function": {"name": "weather_", "arguments": "{\"ci"},
        }]}}]})
        turn.merge({"choices": [{"delta": {"tool_calls": [{
            "index": 0, "id": "1", "function": {"name": "lookup", "arguments": "ty\":\"上海\"}"},
        }]}, "finish_reason": "tool_calls"}], "usage": {"total_tokens": 12}})
        self.assertEqual(turn.tool_calls[0]["id"], "call-1")
        self.assertEqual(turn.tool_calls[0]["name"], "weather_lookup")
        self.assertEqual(json.loads(turn.tool_calls[0]["arguments"]), {"city": "上海"})
        self.assertEqual(turn.usage["total_tokens"], 12)

    def test_reasoning_delta_is_collected_separately_from_answer(self):
        turn = ModelTurn()
        turn.merge({"choices": [{"delta": {"reasoning_content": "先分析", "content": "答案"}}]})
        turn.merge({"choices": [{"delta": {"reasoning": "再核对", "content": "继续"}}]})
        self.assertEqual(turn.reasoning_text, "先分析再核对")
        self.assertEqual(turn.text, "答案继续")

    def test_only_read_tools_fit_into_the_declared_budget(self):
        tools = [
            {"name": "read", "description": "read", "config": {"method": "GET"},
             "input_schema": {"type": "object", "properties": {}}},
            {"name": "write", "description": "write", "config": {"method": "POST"},
             "input_schema": {"type": "object", "properties": {}}},
        ]
        declarations = tool_declarations(tools, 500)
        self.assertEqual([item["function"]["name"] for item in declarations], ["read"])


if __name__ == "__main__":
    unittest.main()
