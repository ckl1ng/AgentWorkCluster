import os
import tempfile
import unittest

from cryptography.fernet import Fernet

os.environ.setdefault("QQ_APP_ID", "app-test")
os.environ.setdefault("QQ_CLIENT_SECRET", "secret-test")
os.environ.setdefault("AGENT_SERVICE_SECRET", "agent-secret")
os.environ.setdefault("QQ_DEFAULT_AGENT_ID", "agent-id")
os.environ.setdefault("QQ_DEFAULT_OWNER_USER_ID", "7")
os.environ.setdefault("QQ_GATEWAY_MASTER_KEY", Fernet.generate_key().decode("ascii"))
os.environ.setdefault("QQ_WEBHOOK_SIGNATURE_MODE", "hmac-sha256")

from app.main import GatewayStore, normalize_event, settings, _signature


class GatewayTest(unittest.TestCase):
    def test_group_event_is_normalized_and_mentions_removed(self):
        event = normalize_event("bot", {
            "id": "event-1", "t": "GROUP_AT_MESSAGE_CREATE",
            "d": {"group_openid": "group-1", "author": {"member_openid": "user-1"}, "content": "<@bot> hello"},
        })
        self.assertIsNotNone(event)
        self.assertEqual(event.scope_type, "group")
        self.assertEqual(event.content, "hello")

    def test_unsupported_event_is_ignored(self):
        self.assertIsNone(normalize_event("bot", {"id": "event-2", "t": "GROUP_ADD_ROBOT", "d": {}}))

    def test_inbox_and_outbound_are_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            store = GatewayStore(directory + "/gateway.db", Fernet.generate_key().decode("ascii"))
            event = {"event_id": "e", "content": "hello"}
            self.assertEqual(store.claim_event("k", event), "claimed")
            self.assertEqual(store.claim_event("k", event), "duplicate")
            self.assertTrue(store.claim_outbound("k"))
            store.complete_outbound("k", "message-1")
            self.assertFalse(store.claim_outbound("k"))
            store.db.close()

    def test_expired_event_is_not_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            store = GatewayStore(directory + "/gateway.db", Fernet.generate_key().decode("ascii"))
            event = {"event_id": "e", "content": "hello"}
            self.assertEqual(store.claim_event("k", event), "claimed")
            store.expire_event("k", "passive reply window expired")
            self.assertEqual(store.claim_event("k", event), "duplicate")
            self.assertEqual(store.pending_events(), [])
            store.db.close()

    def test_hmac_challenge_signature_is_deterministic(self):
        old_mode = settings.signature_mode
        settings.signature_mode = "hmac-sha256"
        self.assertEqual(_signature("1", "token"), _signature("1", "token"))
        settings.signature_mode = old_mode


if __name__ == "__main__":
    unittest.main()
