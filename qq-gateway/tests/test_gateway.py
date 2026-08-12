import asyncio
import json
import logging
import os
import tempfile
import unittest

from cryptography.fernet import Fernet
import websockets

os.environ.setdefault("QQ_APP_ID", "app-test")
os.environ.setdefault("QQ_CLIENT_SECRET", "secret-test")
os.environ.setdefault("AGENT_SERVICE_SECRET", "agent-secret")
os.environ.setdefault("QQ_DEFAULT_AGENT_ID", "agent-id")
os.environ.setdefault("QQ_DEFAULT_OWNER_USER_ID", "7")
os.environ.setdefault("QQ_GATEWAY_MASTER_KEY", Fernet.generate_key().decode("ascii"))
os.environ.setdefault("QQ_WEBHOOK_SIGNATURE_MODE", "hmac-sha256")

from app.main import (
    OP_CALLBACK_VERIFY, OP_DISPATCH, OP_HEARTBEAT, OP_HEARTBEAT_ACK, OP_HTTP_CALLBACK_ACK,
    OP_IDENTIFY, OP_INVALID_SESSION, OP_RECONNECT, OP_RESUME, OP_HELLO,
    GatewayStore, QQApiClient, QQConnectionConfig, QQRuntime, heartbeat_payload, identify_payload, normalize_event, resume_payload, settings, _signature,
)
import app.main as gateway_main


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

    def test_c2c_event_uses_author_user_openid(self):
        event = normalize_event("bot", {
            "id": "event-c2c", "t": "C2C_MESSAGE_CREATE",
            "d": {"author": {"user_openid": "user-1"}, "content": "hello"},
        })
        self.assertIsNotNone(event)
        self.assertEqual(event.scope_type, "c2c")
        self.assertEqual(event.scope_id, "user-1")
        self.assertEqual(event.sender_id, "user-1")

    def test_inbox_and_outbound_are_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            store = GatewayStore(directory + "/gateway.db", Fernet.generate_key().decode("ascii"))
            event = {"event_id": "e", "content": "hello"}
            self.assertEqual(store.claim_event("k", event), "claimed")
            self.assertEqual(store.claim_event("k", event), "duplicate")
            self.assertEqual(store.claim_outbound("k"), "claimed")
            self.assertEqual(store.claim_outbound("k"), "inflight")
            store.complete_outbound("k", "message-1")
            self.assertEqual(store.claim_outbound("k"), "sent")
            store.db.close()

    def test_event_claim_prevents_duplicate_work(self):
        with tempfile.TemporaryDirectory() as directory:
            store = GatewayStore(directory + "/gateway.db", Fernet.generate_key().decode("ascii"))
            event = normalize_event("bot", {
                "id": "event-claim", "t": "C2C_MESSAGE_CREATE",
                "d": {"author": {"user_openid": "user-1"}, "content": "hello"},
            })
            self.assertIsNotNone(event)
            self.assertEqual(store.claim_event(event.event_key, event.payload()), "claimed")
            self.assertEqual(store.claim_event(event.event_key, event.payload()), "duplicate")
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

    def test_websocket_protocol_payloads(self):
        self.assertEqual(
            [OP_DISPATCH, OP_HEARTBEAT, OP_IDENTIFY, OP_RESUME, OP_RECONNECT, OP_INVALID_SESSION, OP_HELLO, OP_HEARTBEAT_ACK, OP_HTTP_CALLBACK_ACK, OP_CALLBACK_VERIFY],
            [0, 1, 2, 6, 7, 9, 10, 11, 12, 13],
        )
        self.assertEqual(identify_payload("QQBot token", 33554432), {
            "op": 2,
            "d": {
                "token": "QQBot token",
                "intents": 33554432,
                "shard": [0, 1],
                "properties": {"$os": "linux", "$browser": "agentWorkCluster", "$device": "agentWorkCluster"},
            },
        })
        self.assertEqual(resume_payload("QQBot token", "session-1", 42), {
            "op": 6, "d": {"token": "QQBot token", "session_id": "session-1", "seq": 42},
        })
        self.assertEqual(heartbeat_payload(None), {"op": 1, "d": None})
        self.assertEqual(heartbeat_payload(42), {"op": 1, "d": 42})

    def test_token_response_aliases(self):
        # Keep response parsing compatible with QQ deployments that use camelCase
        # or return the token under a data wrapper.
        self.assertEqual(QQApiClient._token_from_body({"access_token": "a"}), ("a", 7200))
        self.assertEqual(QQApiClient._token_from_body({"data": {"accessToken": "b", "expiresIn": 3600}}), ("b", 3600))

    def test_structured_event_log_excludes_content_and_secret(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            runtime = QQRuntime(QQConnectionConfig("agent", 7, "app", "client-secret", "bot"))
            event = normalize_event("bot", {
                "id": "event-log", "t": "C2C_MESSAGE_CREATE",
                "d": {"author": {"user_openid": "user-1"}, "content": "do not log this"},
            })
            self.assertIsNotNone(event)
            with self.assertLogs("qq_gateway.events", level=logging.INFO) as captured:
                runtime.record_event("received", event)
            output = "\n".join(captured.output)
            self.assertIn('"event_id":"event-log"', output)
            self.assertNotIn("do not log this", output)
            self.assertNotIn("client-secret", output)
            loop.run_until_complete(runtime.api.close())
        finally:
            asyncio.set_event_loop(None)
            loop.close()


class GatewayProtocolIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_missing_heartbeat_ack_reconnects(self):
        received = []
        reconnected = asyncio.Event()

        async def handler(socket):
            connection_number = len(received)
            await socket.send(json.dumps({"op": OP_HELLO, "d": {"heartbeat_interval": 1000}}))
            received.append([json.loads(await asyncio.wait_for(socket.recv(), timeout=2))])
            if connection_number == 0:
                # Read the heartbeat but intentionally omit opcode 11. The
                # runtime must reconnect instead of leaving a stale session up.
                received[connection_number].append(json.loads(await asyncio.wait_for(socket.recv(), timeout=2)))
                await asyncio.sleep(2)
            else:
                reconnected.set()
                await asyncio.sleep(0.1)

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        previous_store = gateway_main.store
        runtime = QQRuntime(QQConnectionConfig("agent-heartbeat", 7, "app-1", "secret", "bot-1"))

        async def fake_gateway_url():
            return "ws://127.0.0.1:{}".format(port)

        async def fake_access_token(force=False):
            return "token-1"

        runtime.api.gateway_url = fake_gateway_url
        runtime.api.access_token = fake_access_token
        try:
            with tempfile.TemporaryDirectory() as directory:
                gateway_main.store = GatewayStore(directory + "/gateway.db", Fernet.generate_key().decode("ascii"))
                runtime.start()
                await asyncio.wait_for(reconnected.wait(), timeout=6)
                self.assertEqual(received[0][0]["op"], OP_IDENTIFY)
                self.assertEqual(received[0][1], {"op": OP_HEARTBEAT, "d": None})
                self.assertEqual(received[1][0]["op"], OP_IDENTIFY)
                self.assertGreaterEqual(runtime.reconnect_count, 1)
        finally:
            await runtime.stop()
            if gateway_main.store is not None:
                gateway_main.store.db.close()
            gateway_main.store = previous_store
            server.close()
            await server.wait_closed()

    async def test_reconnect_uses_resume_with_ready_session_and_sequence(self):
        received = []
        resumed = asyncio.Event()

        async def handler(socket):
            connection_number = len(received)
            await socket.send(json.dumps({"op": OP_HELLO, "d": {"heartbeat_interval": 1000}}))
            received.append(json.loads(await asyncio.wait_for(socket.recv(), timeout=2)))
            if connection_number == 0:
                await socket.send(json.dumps({
                    "op": OP_DISPATCH, "s": 7, "t": "READY",
                    "d": {"session_id": "session-1", "user": {"id": "bot-1"}},
                }))
                await socket.send(json.dumps({"op": OP_RECONNECT, "d": {}}))
            else:
                resumed.set()
                await asyncio.sleep(0.1)

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        previous_store = gateway_main.store
        runtime = QQRuntime(QQConnectionConfig("agent-1", 7, "app-1", "secret", "bot-1"))

        async def fake_gateway_url():
            return "ws://127.0.0.1:{}".format(port)

        async def fake_access_token(force=False):
            return "token-1"

        runtime.api.gateway_url = fake_gateway_url
        runtime.api.access_token = fake_access_token
        try:
            with tempfile.TemporaryDirectory() as directory:
                gateway_main.store = GatewayStore(directory + "/gateway.db", Fernet.generate_key().decode("ascii"))
                runtime.start()
                await asyncio.wait_for(resumed.wait(), timeout=5)
                self.assertEqual(received[0]["op"], OP_IDENTIFY)
                self.assertEqual(received[1], {"op": OP_RESUME, "d": {"token": "QQBot token-1", "session_id": "session-1", "seq": 7}})
                self.assertEqual(runtime.resume_count, 1)
        finally:
            await runtime.stop()
            if gateway_main.store is not None:
                gateway_main.store.db.close()
            gateway_main.store = previous_store
            server.close()
            await server.wait_closed()

    async def test_non_resumable_invalid_session_reidentifies(self):
        received = []
        reidentified = asyncio.Event()

        async def handler(socket):
            connection_number = len(received)
            await socket.send(json.dumps({"op": OP_HELLO, "d": {"heartbeat_interval": 1000}}))
            received.append(json.loads(await asyncio.wait_for(socket.recv(), timeout=2)))
            if connection_number == 0:
                await socket.send(json.dumps({
                    "op": OP_DISPATCH, "s": 7, "t": "READY",
                    "d": {"session_id": "session-1", "user": {"id": "bot-1"}},
                }))
                await socket.send(json.dumps({"op": OP_INVALID_SESSION, "d": False}))
            else:
                reidentified.set()
                await asyncio.sleep(0.1)

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        previous_store = gateway_main.store
        runtime = QQRuntime(QQConnectionConfig("agent-2", 7, "app-1", "secret", "bot-1"))

        async def fake_gateway_url():
            return "ws://127.0.0.1:{}".format(port)

        async def fake_access_token(force=False):
            return "token-1"

        runtime.api.gateway_url = fake_gateway_url
        runtime.api.access_token = fake_access_token
        try:
            with tempfile.TemporaryDirectory() as directory:
                gateway_main.store = GatewayStore(directory + "/gateway.db", Fernet.generate_key().decode("ascii"))
                runtime.start()
                await asyncio.wait_for(reidentified.wait(), timeout=5)
                self.assertEqual(received[0]["op"], OP_IDENTIFY)
                self.assertEqual(received[1]["op"], OP_IDENTIFY)
                self.assertEqual(runtime.session_id, "")
                self.assertIsNone(runtime.sequence)
                self.assertEqual(runtime.reconnect_count, 1)
        finally:
            await runtime.stop()
            if gateway_main.store is not None:
                gateway_main.store.db.close()
            gateway_main.store = previous_store
            server.close()
            await server.wait_closed()


if __name__ == "__main__":
    unittest.main()
