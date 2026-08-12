import tempfile
import unittest

from cryptography.fernet import Fernet

from app.main import AgentStore


def payload():
    return {"name": "Autonomy", "base_url": "https://model.example/v1", "api_key": "secret", "model_id": "test", "system_prompt": "system"}


class AutonomousStatusTest(unittest.TestCase):
    def test_status_is_private_to_each_conversation_and_contains_names_only(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AgentStore(directory + "/agent.db", Fernet.generate_key().decode("ascii"))
            agent = store.create_agent(7, payload())
            first = store.create_conversation(agent["id"], 7)
            second = store.create_conversation(agent["id"], 7)
            store.update_channel_status(first["id"], 7, "张三")
            status = store.conversation_status(first["id"], 7)
            self.assertEqual(status["source"], "user")
            self.assertEqual(status["group_members"], ["张三"])
            self.assertTrue(status["time"])
            self.assertEqual(store.conversation_status(second["id"], 7)["group_members"], [])

    def test_schedule_idempotency_and_due_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AgentStore(directory + "/agent.db", Fernet.generate_key().decode("ascii"))
            agent = store.create_agent(7, payload())
            first = store.create_schedule(agent["id"], 7, "2020-01-01T00:00:00Z", "提醒", "daily")
            duplicate = store.create_schedule(agent["id"], 7, "2030-01-01T00:00:00Z", "other", "daily")
            self.assertFalse(first["duplicate"])
            self.assertTrue(duplicate["duplicate"])
            self.assertEqual([item["id"] for item in store.claim_due_schedules()], [first["id"]])
            self.assertEqual(store.claim_due_schedules(), [])

