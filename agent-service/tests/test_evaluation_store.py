import tempfile
import unittest

from cryptography.fernet import Fernet

from app.main import AgentStore


class EvaluationStoreTest(unittest.TestCase):
    def test_run_creation_writes_a_durable_outbox_event(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AgentStore(directory + "/agents.db", Fernet.generate_key().decode("ascii"))
            agent = store.create_agent(7, {
                "name": "Outbox test", "base_url": "https://example.test/v1", "api_key": "test-key",
                "model_id": "test-model", "system_prompt": "test",
            })
            conversation = store.create_conversation(agent["id"], 7)
            run = store.create_run(conversation["id"], 7, "durable task")
            events = store.pending_outbox_events()
            self.assertEqual(events[0]["aggregate_id"], run["id"])
            self.assertEqual(events[0]["event_type"], "agent.run.queued")
            store.mark_outbox_published(events[0]["id"])
            self.assertEqual(store.pending_outbox_events(), [])

    def test_cases_are_encrypted_and_results_keep_failure_category(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AgentStore(directory + "/agents.db", Fernet.generate_key().decode("ascii"))
            case = store.create_evaluation_case({
                "id": "baseline-case", "input": "private task", "selected_context": "private context",
                "expected_assertions": {"state": "completed"}, "tags": ["baseline"],
            })
            self.assertEqual(case["input"], "private task")
            run = store.create_evaluation_run("phase0-test", "agent-v1", "model-1")
            store.record_evaluation_result(run["id"], case["id"], True, 1.0, 4, {"output_tokens": 2})
            row = store.db.execute("SELECT input_encrypted FROM evaluation_cases WHERE id = ?", (case["id"],)).fetchone()
            self.assertNotIn(b"private task", row[0])

    def test_unknown_failure_category_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AgentStore(directory + "/agents.db", Fernet.generate_key().decode("ascii"))
            run = store.create_evaluation_run("phase0-test")
            with self.assertRaises(ValueError):
                store.record_evaluation_result(run["id"], "missing-case", False, 0.0, 1, {}, failure_category="invalid")
