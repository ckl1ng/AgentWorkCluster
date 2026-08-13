import asyncio
import json
import tempfile
import unittest

from cryptography.fernet import Fernet

from app.harness import execute_local_tool
from app.main import AgentStore, channel_message_content, runtime_system_messages


def payload():
    return {"name": "Autonomy", "base_url": "https://model.example/v1", "api_key": "secret", "model_id": "test", "system_prompt": "system"}


class AutonomousStatusTest(unittest.TestCase):
    def test_status_is_private_and_channel_metadata_is_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AgentStore(directory + "/agent.db", Fernet.generate_key().decode("ascii"))
            agent = store.create_agent(7, payload())
            first = store.create_conversation(agent["id"], 7)
            second = store.create_conversation(agent["id"], 7)
            store.set_conversation_channel(first["id"], 7, "qq", "group", "group-1")
            store.update_channel_status(first["id"], 7, "open-id-1", "张三")
            store.update_channel_status(first["id"], 7, "open-id-1", "张三")
            store.update_channel_status(first["id"], 7, "open-id-1", "张三的新昵称")
            status = store.conversation_status(first["id"], 7)
            self.assertEqual(status["source"], "user")
            self.assertEqual(set(status), {"source", "time", "updated_at"})
            self.assertTrue(status["time"])
            self.assertIn("+08:00", status["time"])
            system_info = store.conversation_system_info(first["id"], 7)
            self.assertEqual(first["source"], "web")
            self.assertEqual(store.get_conversation(first["id"], 7)["source"], "qq_group")
            self.assertEqual(system_info, {"provider": "qq", "scope_type": "group", "scope_id": "group-1", "source": "qq_group", "members": [{"openid": "open-id-1", "name": "张三"}]})
            self.assertEqual(store.conversation_system_info(second["id"], 7)["members"], [])

    def test_group_message_content_includes_sender_name(self):
        self.assertEqual(channel_message_content("请帮忙", "group", "张三", "member-1"), "张三：请帮忙")
        self.assertEqual(channel_message_content("你好", "c2c", "李四", "user-1"), "你好")

    def test_schedule_idempotency_and_due_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AgentStore(directory + "/agent.db", Fernet.generate_key().decode("ascii"))
            agent = store.create_agent(7, payload())
            conversation = store.create_conversation(agent["id"], 7)
            first = store.create_schedule(agent["id"], 7, conversation["id"], "2020-01-01T08:00:00+08:00", "提醒", "daily")
            duplicate = store.create_schedule(agent["id"], 7, conversation["id"], "2030-01-01T08:00:00+08:00", "other", "daily")
            self.assertFalse(first["duplicate"])
            self.assertTrue(duplicate["duplicate"])
            self.assertEqual(first["run_at"], "2020-01-01T08:00:00+08:00")
            self.assertEqual(duplicate["run_at"], "2020-01-01T08:00:00+08:00")
            due = store.claim_due_schedules()
            self.assertEqual([item["id"] for item in due], [first["id"]])
            self.assertEqual(due[0]["source_conversation_id"], conversation["id"])
            self.assertEqual(store.claim_due_schedules(), [])

    def test_schedule_requires_beijing_time(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AgentStore(directory + "/agent.db", Fernet.generate_key().decode("ascii"))
            agent = store.create_agent(7, payload())
            conversation = store.create_conversation(agent["id"], 7)
            with self.assertRaisesRegex(ValueError, "北京时间"):
                store.create_schedule(agent["id"], 7, conversation["id"], "2030-01-01T00:00:00Z", "提醒", "utc")

    def test_current_time_tool_returns_beijing_time(self):
        result = asyncio.run(execute_local_tool({
            "config": {"builtin": "current_time"},
            "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        }, {}, 1024))
        value = json.loads(result["content"])
        self.assertEqual(value["timezone"], "Asia/Shanghai")
        self.assertTrue(value["iso"].endswith("+08:00"))

    def test_runtime_system_messages_include_time_and_qq_identity_context(self):
        messages = runtime_system_messages(
            {"time": "2026-08-13T10:00:00+08:00", "updated_at": "2026-08-13T02:00:00Z"},
            {"provider": "qq", "scope_type": "group", "scope_id": "group-openid", "members": [{"name": "张三", "openid": "member-openid"}]},
        )
        self.assertEqual(len(messages), 2)
        self.assertIn("current Beijing time", messages[0]["content"])
        self.assertIn("Asia/Shanghai", messages[0]["content"])
        self.assertIn("conversation status time is 2026-08-13T10:00:00+08:00", messages[0]["content"])
        self.assertIn("scope_type=group", messages[1]["content"])
        self.assertIn("group-openid", messages[1]["content"])
        self.assertIn("member-openid", messages[1]["content"])

    def test_delete_conversation_permanently_erases_conversation_records(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AgentStore(directory + "/agent.db", Fernet.generate_key().decode("ascii"))
            agent = store.create_agent(7, payload())
            conversation = store.create_conversation(agent["id"], 7)
            run = store.create_run(conversation["id"], 7, "删除这段会话")
            store.add_message(conversation["id"], run["id"], "assistant", "回复", 0)
            store.update_channel_status(conversation["id"], 7, "open-id-1", "张三")
            store.update_run(run["id"], "cancelled")

            self.assertTrue(store.delete_conversation(conversation["id"], 7))
            self.assertIsNone(store.get_conversation(conversation["id"], 7))
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM messages WHERE conversation_id = ?", (conversation["id"],)).fetchone()[0], 0)
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM runs WHERE conversation_id = ?", (conversation["id"],)).fetchone()[0], 0)
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM agent_status WHERE conversation_id = ?", (conversation["id"],)).fetchone()[0], 0)
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM conversation_channel_identities WHERE conversation_id = ?", (conversation["id"],)).fetchone()[0], 0)
