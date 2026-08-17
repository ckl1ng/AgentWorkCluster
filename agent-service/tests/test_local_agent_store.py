import tempfile
import unittest

from cryptography.fernet import Fernet

from app.main import AgentStore, settings


def agent_payload(**overrides):
    value = {
        "name": "Local Agent", "base_url": "https://model.example/v1", "api_key": "model-secret",
        "model_id": "test-model", "system_prompt": "system",
    }
    value.update(overrides)
    return value


class LocalAgentStoreTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = AgentStore(self.directory.name + "/agent.db", Fernet.generate_key().decode("ascii"))
        self.agent = self.store.create_agent(7, agent_payload())
        self.conversation = self.store.create_conversation(self.agent["id"], 7)
        self.old_secret = settings.service_secret
        settings.service_secret = "test-device-token-secret"

    def tearDown(self):
        settings.service_secret = self.old_secret
        self.directory.cleanup()

    def test_pairing_issues_a_one_time_refresh_credential(self):
        pairing = self.store.start_pairing({"display_name": "workstation", "platform": "linux"})
        device = self.store.approve_pairing(pairing["pairing_id"], pairing["code"], 7)
        claimed = self.store.claim_pairing(pairing["pairing_id"], pairing["pairing_secret"])

        self.assertEqual(device["status"], "offline")
        self.assertNotIn("credential_hash", device)
        self.assertEqual(claimed["state"], "approved")
        self.assertIsNone(self.store.claim_pairing(pairing["pairing_id"], pairing["pairing_secret"]))
        self.assertNotIn("refresh_token", claimed)
        pairing_columns = {row[1] for row in self.store.db.execute("PRAGMA table_info(pairing_sessions)").fetchall()}
        self.assertNotIn("credential_encrypted", pairing_columns)
        access = self.store.issue_device_access_token(pairing["pairing_secret"])
        self.assertEqual(self.store.authenticate_device_access_token(access["access_token"]), device["id"])
        self.assertTrue(self.store.revoke_local_device(device["id"], 7))
        self.assertIsNone(self.store.authenticate_device_access_token(access["access_token"]))
        self.assertIsNone(self.store.issue_device_access_token(pairing["pairing_secret"]))
        workspace = self.store.register_device_workspace(device["id"], {"display_name": "chat-server"})
        self.assertIsNone(workspace)

    def test_channel_event_deduplication_is_scoped_to_provider_bot_and_event(self):
        self.assertIsNone(self.store.channel_event_result("qq", "bot-a", "event-a"))
        self.store.remember_channel_event("qq", "bot-a", "event-a", self.conversation["id"], "run-a", 7)

        result = self.store.channel_event_result("qq", "bot-a", "event-a")
        self.assertEqual(result, {"conversation_id": self.conversation["id"], "run_id": "run-a"})
        self.assertIsNone(self.store.channel_event_result("qq", "bot-b", "event-a"))
        with self.assertRaises(self.store.db.integrity_error):
            self.store.remember_channel_event("qq", "bot-a", "event-a", self.conversation["id"], "run-b", 7)

    def test_local_run_creates_dispatch_without_cloud_outbox_event(self):
        device = self.store.create_local_device(7, {"display_name": "workstation", "platform": "linux"})
        workspace = self.store.add_local_workspace(7, device["id"], {"display_name": "chat-server"})
        self.store.bind_local_agent(self.agent["id"], 7, device["id"], workspace["id"], "server_proxy")

        run = self.store.create_run(self.conversation["id"], 7, "inspect tests")

        self.assertEqual(run["state"], "queued")
        self.assertEqual(run["local_dispatch"]["executor_state"], "pending")
        self.assertEqual(run["local_dispatch"]["device_id"], device["id"])
        self.assertEqual(self.store.pending_outbox_events(), [])

    def test_awc_run_requires_an_online_cli_websocket(self):
        device = self.store.create_local_device(7, {"display_name": "awc"})
        workspace = self.store.add_local_workspace(7, device["id"], {"display_name": "project"})
        awc = self.store.create_agent(7, agent_payload(
            name="AWC", api_key="", base_url="awc://local", model_id="default",
            execution_target="local", model_mode="local_direct", default_device_id=device["id"], default_workspace_id=workspace["id"],
        ))
        conversation = self.store.create_conversation(awc["id"], 7)
        self.assertEqual(self.store.create_run(conversation["id"], 7, "offline")["error"], "AWC CLI 未通过 WebSocket 连接，无法发送消息")
        self.store.set_local_device_status(device["id"], "online")
        run = self.store.create_run(conversation["id"], 7, "online")
        self.assertEqual(run["local_dispatch"]["executor_state"], "pending")
        offer = self.store.offer_local_run(device["id"])
        self.assertEqual(offer["profile"], "default")

    def test_workspace_from_another_device_cannot_be_bound(self):
        first = self.store.create_local_device(7, {"display_name": "first"})
        second = self.store.create_local_device(7, {"display_name": "second"})
        workspace = self.store.add_local_workspace(7, first["id"], {"display_name": "project"})

        with self.assertRaisesRegex(ValueError, "设备或工作区"):
            self.store.bind_local_agent(self.agent["id"], 7, second["id"], workspace["id"], "server_proxy")

    def test_local_direct_binding_requires_a_device_registered_model_and_never_snapshots_a_key(self):
        direct_agent = self.store.create_agent(7, agent_payload(
            name="Direct Agent", api_key="", execution_target="local", model_mode="local_direct",
        ))
        direct_conversation = self.store.create_conversation(direct_agent["id"], 7)
        device = self.store.create_local_device(7, {"display_name": "workstation"})
        workspace = self.store.add_local_workspace(7, device["id"], {"display_name": "chat-server"})

        with self.assertRaisesRegex(ValueError, "尚未登记"):
            self.store.bind_local_agent(direct_agent["id"], 7, device["id"], workspace["id"], "local_direct")
        registered = self.store.register_local_model(device["id"], {
            "agent_id": direct_agent["id"], "base_url": "https://local-model.example/v1", "model_id": "local-model",
        })
        bound = self.store.bind_local_agent(direct_agent["id"], 7, device["id"], workspace["id"], "local_direct")
        run = self.store.create_run(direct_conversation["id"], 7, "inspect tests")

        self.assertEqual(registered["device_id"], device["id"])
        self.assertEqual(bound["model_mode"], "local_direct")
        self.assertEqual(bound["model"]["api_key_configured"], True)
        self.assertEqual(self.store.decrypt_api_key(self.store.db.execute("SELECT encrypted_api_key FROM agents WHERE id = ?", (direct_agent["id"],)).fetchone()["encrypted_api_key"]), "")
        self.assertNotIn("encrypted_api_key", self.store.run_snapshot(run["id"]))
        offer = self.store.offer_local_run(device["id"])
        self.assertEqual(offer["run_id"], run["id"])
        self.assertTrue(self.store.claim_local_run(run["id"], device["id"], offer["lease_id"], "session-1"))
        event = self.store.append_local_run_event(run["id"], device["id"], offer["lease_id"], 1, "agent.message.delta", {"content": "local output"})
        self.assertEqual(event["sequence"], 1)
        self.assertIsNone(self.store.append_local_run_event(run["id"], device["id"], offer["lease_id"], 1, "agent.message.delta", {"content": "duplicate"}))
        self.assertTrue(self.store.finish_local_run(run["id"], device["id"], offer["lease_id"], "completed", "local output"))
        self.assertEqual(self.store.get_run(run["id"], 7)["state"], "completed")
        self.assertEqual(self.store.model_messages(direct_conversation["id"], direct_conversation["context_epoch"])[-1], {"role": "assistant", "content": "local output"})

    def test_expired_offer_can_be_reissued_and_claim_stays_atomic(self):
        direct_agent = self.store.create_agent(7, agent_payload(name="Direct Agent", api_key="", execution_target="local", model_mode="local_direct"))
        conversation = self.store.create_conversation(direct_agent["id"], 7)
        device = self.store.create_local_device(7, {"display_name": "workstation"})
        workspace = self.store.add_local_workspace(7, device["id"], {"display_name": "chat-server"})
        self.store.register_local_model(device["id"], {"agent_id": direct_agent["id"], "base_url": "https://local-model.example/v1", "model_id": "local-model"})
        self.store.bind_local_agent(direct_agent["id"], 7, device["id"], workspace["id"], "local_direct")
        run = self.store.create_run(conversation["id"], 7, "retry after disconnect")

        first = self.store.offer_local_run(device["id"])
        self.store.db.execute("UPDATE local_run_dispatches SET lease_expires_at = ? WHERE run_id = ?", ("2000-01-01T00:00:00Z", run["id"]))
        self.store.db.commit()
        second = self.store.offer_local_run(device["id"])

        self.assertNotEqual(first["lease_id"], second["lease_id"])
        self.assertFalse(self.store.claim_local_run(run["id"], device["id"], first["lease_id"], "stale-session"))
        self.assertTrue(self.store.claim_local_run(run["id"], device["id"], second["lease_id"], "session-1"))
        self.assertEqual(self.store.get_run(run["id"], 7)["state"], "running")

    def test_codex_executor_bind_needs_no_local_model_and_offers_carry_executor(self):
        codex_agent = self.store.create_agent(7, agent_payload(
            name="Codex Agent", api_key="", execution_target="local", model_mode="local_direct",
        ))
        conversation = self.store.create_conversation(codex_agent["id"], 7)
        device = self.store.create_local_device(7, {"display_name": "workstation"})
        workspace = self.store.add_local_workspace(7, device["id"], {"display_name": "project"})

        # codex executor binds without requiring a device-registered local model
        bound = self.store.bind_local_agent(codex_agent["id"], 7, device["id"], workspace["id"], "local_direct", "codex")
        self.assertEqual(bound["executor_kind"], "codex")
        # server never holds a model key for a black-box external executor
        self.assertFalse(bound["model"]["api_key_configured"])

        run = self.store.create_run(conversation["id"], 7, "inspect tests")
        offer = self.store.offer_local_run(device["id"])
        self.assertEqual(offer["run_id"], run["id"])
        self.assertEqual(offer["executor"], "codex")
        self.assertNotIn("encrypted_api_key", self.store.run_snapshot(run["id"]))

    def test_local_direct_task_claim_and_completion_reaches_proposer_review(self):
        direct_agent = self.store.create_agent(7, agent_payload(
            name="Task Direct Agent", api_key="", execution_target="local", model_mode="local_direct",
        ))
        device = self.store.create_local_device(7, {"display_name": "workstation"})
        workspace = self.store.add_local_workspace(7, device["id"], {"display_name": "project"})
        self.store.register_local_model(device["id"], {
            "agent_id": direct_agent["id"], "base_url": "https://local-model.example/v1", "model_id": "local-model",
        })
        self.store.bind_local_agent(direct_agent["id"], 7, device["id"], workspace["id"], "local_direct")
        task = self.store.create_task(7, {
            "title": "local task", "goal": "produce a local result", "assigned_agent_id": direct_agent["id"],
        })

        offer = self.store.offer_local_run(device["id"])
        self.assertTrue(self.store.claim_local_run(task["run_id"], device["id"], offer["lease_id"], "session-task"))
        self.assertEqual(self.store.get_task(task["id"], 7)["state"], "in_progress")
        self.assertTrue(self.store.finish_local_run(task["run_id"], device["id"], offer["lease_id"], "completed", "local task result"))

        completed = self.store.get_task(task["id"], 7)
        self.assertEqual(completed["state"], "awaiting_proposer_close")
        self.assertEqual(self.store.task_results(task["id"], 7)[0]["result"], "local task result")


if __name__ == "__main__":
    unittest.main()
