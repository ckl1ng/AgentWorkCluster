"""QQ Bot WebSocket gateway for the Agent platform."""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import time
from urllib.parse import urlparse
from dataclasses import asdict, dataclass
from threading import RLock
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx
import websockets
from cryptography.fernet import Fernet, InvalidToken
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse


MENTION_RE = re.compile(r"<@!?[A-Za-z0-9_-]+>")
SUPPORTED_EVENTS = {"GROUP_AT_MESSAGE_CREATE": "group", "C2C_MESSAGE_CREATE": "c2c"}
GROUP_AND_C2C_EVENT_INTENT = 1 << 25
MAX_QQ_INTENTS = (1 << 31) - 1

# QQ Gateway opcodes. 12 and 13 belong to the retired HTTP callback flow and
# must never be treated as WebSocket events.
OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11
OP_HTTP_CALLBACK_ACK = 12
OP_CALLBACK_VERIFY = 13

event_logger = logging.getLogger("qq_gateway.events")


def _now() -> float:
    return time.time()


class Settings:
    def __init__(self) -> None:
        self.app_id = os.getenv("QQ_APP_ID", "")
        self.client_secret = os.getenv("QQ_CLIENT_SECRET", "")
        self.api_base_url = os.getenv("QQ_API_BASE_URL", "https://api.bot.qq.com").rstrip("/")
        self.token_api_base_url = os.getenv("QQ_TOKEN_API_BASE_URL", "https://bots.qq.com").rstrip("/")
        self.bot_id = os.getenv("QQ_BOT_ID", self.app_id)
        self.agent_url = os.getenv("QQ_AGENT_INTERNAL_URL", "http://127.0.0.1:9011").rstrip("/")
        self.agent_service_secret = os.getenv("AGENT_SERVICE_SECRET", "")
        self.agent_id = os.getenv("QQ_DEFAULT_AGENT_ID", "")
        self.owner_user_id = os.getenv("QQ_DEFAULT_OWNER_USER_ID", "")
        self.database_path = os.getenv("QQ_GATEWAY_DATABASE_PATH", "./data/qq-gateway.db")
        self.master_key = os.getenv("QQ_GATEWAY_MASTER_KEY", "")
        self.signature_mode = os.getenv("QQ_WEBHOOK_SIGNATURE_MODE", "ed25519").lower()
        self.max_content_length = int(os.getenv("QQ_MAX_CONTENT_LENGTH", "5000"))
        self.run_timeout = float(os.getenv("QQ_AGENT_RUN_TIMEOUT_SECONDS", "240"))
        self.retry_after_seconds = float(os.getenv("QQ_EVENT_RETRY_AFTER_SECONDS", "10"))
        self.passive_reply_window = float(os.getenv("QQ_PASSIVE_REPLY_WINDOW_SECONDS", "290"))
        self.retry_interval_seconds = float(os.getenv("QQ_EVENT_RETRY_INTERVAL_SECONDS", "5"))
        self.max_event_tasks = int(os.getenv("QQ_MAX_EVENT_TASKS", "32"))
        self.intents = int(os.getenv("QQ_INTENTS", str(GROUP_AND_C2C_EVENT_INTENT)))

    def validate(self) -> None:
        required = {"AGENT_SERVICE_SECRET": self.agent_service_secret, "QQ_GATEWAY_MASTER_KEY": self.master_key}
        missing = [name for name, value in required.items() if not value or value.startswith("replace-with-")]
        if missing:
            raise RuntimeError("Missing QQ gateway configuration: " + ", ".join(missing))
        legacy_values = (self.app_id, self.client_secret, self.agent_id, self.owner_user_id)
        configured_legacy = tuple(value for value in legacy_values if value and not value.startswith("replace-with-"))
        if configured_legacy and len(configured_legacy) != len(legacy_values):
            raise RuntimeError("Legacy QQ_* configuration must include app, secret, agent, and owner values")
        if self.owner_user_id and not self.owner_user_id.startswith("replace-with-"):
            try:
                int(self.owner_user_id)
            except ValueError as exc:
                raise RuntimeError("QQ_DEFAULT_OWNER_USER_ID must be an integer") from exc
        if self.signature_mode not in {"ed25519", "hmac-sha256", "none"}:
            raise RuntimeError("QQ_WEBHOOK_SIGNATURE_MODE must be ed25519, hmac-sha256, or none")
        if min(self.run_timeout, self.retry_after_seconds, self.passive_reply_window, self.retry_interval_seconds) <= 0:
            raise RuntimeError("QQ Gateway timeout and retry settings must be positive")
        if self.max_event_tasks < 1:
            raise RuntimeError("QQ_MAX_EVENT_TASKS must be positive")
        if not 1 <= self.intents <= MAX_QQ_INTENTS:
            raise RuntimeError("QQ_INTENTS must be a positive 32-bit integer")

class GatewayStore:
    """Durable encrypted inbox and provider-to-Agent conversation mapping."""

    def __init__(self, path: str, master_key: str) -> None:
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = RLock()
        self.cipher = Fernet(master_key.encode("utf-8"))
        with self.lock:
            self.db.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS inbox_events (
                  event_key TEXT PRIMARY KEY, payload_encrypted BLOB NOT NULL,
                  status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                  run_id TEXT, last_error TEXT NOT NULL DEFAULT '',
                  created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS inbox_events_retry_idx ON inbox_events(status, updated_at);
                CREATE TABLE IF NOT EXISTS channel_conversations (
                  scope_key TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                  agent_id TEXT NOT NULL, owner_user_id INTEGER NOT NULL,
                  created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS outbound_messages (
                  event_key TEXT PRIMARY KEY, status TEXT NOT NULL,
                  provider_message_id TEXT, last_error TEXT NOT NULL DEFAULT '',
                  attempts INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS qq_connections (
                  agent_id TEXT PRIMARY KEY, owner_user_id INTEGER NOT NULL,
                  app_id TEXT NOT NULL, client_secret_encrypted BLOB NOT NULL,
                  bot_id TEXT NOT NULL DEFAULT '', api_base_url TEXT NOT NULL,
                  intents INTEGER NOT NULL DEFAULT 33554432, status TEXT NOT NULL DEFAULT 'disconnected',
                  last_error TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL
                );
                """
            )
            self.db.commit()

    def _encrypt(self, value: Dict[str, Any]) -> bytes:
        return self.cipher.encrypt(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))

    def _decrypt(self, value: bytes) -> Dict[str, Any]:
        try:
            return json.loads(self.cipher.decrypt(value).decode("utf-8"))
        except (InvalidToken, TypeError, ValueError) as exc:
            raise RuntimeError("Stored gateway payload cannot be decrypted") from exc

    def claim_event(self, event_key: str, payload: Dict[str, Any]) -> str:
        now = _now()
        with self.lock:
            row = self.db.execute("SELECT status, updated_at FROM inbox_events WHERE event_key = ?", (event_key,)).fetchone()
            if row is None:
                self.db.execute(
                    "INSERT INTO inbox_events(event_key, payload_encrypted, status, attempts, created_at, updated_at) VALUES (?, ?, 'processing', 1, ?, ?)",
                    (event_key, self._encrypt(payload), now, now),
                )
                self.db.commit()
                return "claimed"
            if row["status"] in {"completed", "expired"}:
                return "duplicate"
            if row["status"] == "processing" and now - float(row["updated_at"]) < settings.retry_after_seconds:
                return "duplicate"
            self.db.execute(
                "UPDATE inbox_events SET status = 'processing', attempts = attempts + 1, updated_at = ?, last_error = '' WHERE event_key = ?",
                (now, event_key),
            )
            self.db.commit()
            return "claimed"

    def pending_events(self, limit: int = 20) -> List[Dict[str, Any]]:
        cutoff = _now() - settings.retry_after_seconds
        with self.lock:
            rows = self.db.execute(
                "SELECT event_key, payload_encrypted FROM inbox_events WHERE status IN ('processing', 'failed') AND updated_at < ? ORDER BY updated_at LIMIT ?",
                (cutoff, limit),
            ).fetchall()
        return [{"event_key": row["event_key"], "payload": self._decrypt(row["payload_encrypted"])} for row in rows]

    def mark_run(self, event_key: str, run_id: str) -> None:
        with self.lock:
            self.db.execute("UPDATE inbox_events SET run_id = ?, updated_at = ? WHERE event_key = ?", (run_id, _now(), event_key))
            self.db.commit()

    def touch_event(self, event_key: str) -> None:
        with self.lock:
            self.db.execute("UPDATE inbox_events SET updated_at = ? WHERE event_key = ? AND status = 'processing'", (_now(), event_key))
            self.db.commit()

    def complete_event(self, event_key: str) -> None:
        with self.lock:
            self.db.execute("UPDATE inbox_events SET status = 'completed', updated_at = ? WHERE event_key = ?", (_now(), event_key))
            self.db.commit()

    def fail_event(self, event_key: str, error: str) -> None:
        with self.lock:
            self.db.execute("UPDATE inbox_events SET status = 'failed', last_error = ?, updated_at = ? WHERE event_key = ?", (error[:500], _now(), event_key))
            self.db.commit()

    def expire_event(self, event_key: str, error: str) -> None:
        with self.lock:
            self.db.execute("UPDATE inbox_events SET status = 'expired', last_error = ?, updated_at = ? WHERE event_key = ?", (error[:500], _now(), event_key))
            self.db.commit()

    def get_conversation(self, scope_key: str) -> Optional[str]:
        with self.lock:
            row = self.db.execute("SELECT conversation_id FROM channel_conversations WHERE scope_key = ?", (scope_key,)).fetchone()
        return row["conversation_id"] if row else None

    def save_conversation(self, scope_key: str, conversation_id: str, agent_id: str, owner_user_id: int) -> None:
        with self.lock:
            self.db.execute(
                """INSERT INTO channel_conversations(scope_key, conversation_id, agent_id, owner_user_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(scope_key) DO UPDATE SET conversation_id = excluded.conversation_id, updated_at = excluded.updated_at""",
                (scope_key, conversation_id, agent_id, owner_user_id, _now(), _now()),
            )
            self.db.commit()

    def claim_outbound(self, event_key: str) -> str:
        with self.lock:
            row = self.db.execute("SELECT status, updated_at FROM outbound_messages WHERE event_key = ?", (event_key,)).fetchone()
            if row is not None and row["status"] == "sent":
                return "sent"
            if row is not None and row["status"] == "sending" and _now() - float(row["updated_at"]) < settings.retry_after_seconds:
                return "inflight"
            if row is None:
                self.db.execute("INSERT INTO outbound_messages(event_key, status, attempts, updated_at) VALUES (?, 'sending', 1, ?)", (event_key, _now()))
            else:
                self.db.execute("UPDATE outbound_messages SET status = 'sending', attempts = attempts + 1, updated_at = ? WHERE event_key = ?", (_now(), event_key))
            self.db.commit()
            return "claimed"

    def complete_outbound(self, event_key: str, message_id: str = "") -> None:
        with self.lock:
            self.db.execute("UPDATE outbound_messages SET status = 'sent', provider_message_id = ?, updated_at = ? WHERE event_key = ?", (message_id, _now(), event_key))
            self.db.commit()

    def fail_outbound(self, event_key: str, error: str) -> None:
        with self.lock:
            self.db.execute("UPDATE outbound_messages SET status = 'failed', last_error = ?, updated_at = ? WHERE event_key = ?", (error[:500], _now(), event_key))
            self.db.commit()

    def save_connection(self, config: "QQConnectionConfig") -> None:
        with self.lock:
            self.db.execute(
                """INSERT INTO qq_connections(agent_id, owner_user_id, app_id, client_secret_encrypted,
                   bot_id, api_base_url, intents, status, last_error, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'connecting', '', ?)
                   ON CONFLICT(agent_id) DO UPDATE SET owner_user_id=excluded.owner_user_id,
                   app_id=excluded.app_id, client_secret_encrypted=excluded.client_secret_encrypted,
                   bot_id=excluded.bot_id, api_base_url=excluded.api_base_url, intents=excluded.intents,
                   status='connecting', last_error='', updated_at=excluded.updated_at""",
                (config.agent_id, config.owner_user_id, config.app_id,
                 self.cipher.encrypt(config.client_secret.encode("utf-8")), config.bot_id,
                 config.api_base_url, config.intents, _now()),
            )
            self.db.commit()

    def load_connections(self) -> List["QQConnectionConfig"]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM qq_connections ORDER BY updated_at").fetchall()
        return [QQConnectionConfig(
            agent_id=row["agent_id"], owner_user_id=int(row["owner_user_id"]), app_id=row["app_id"],
            client_secret=self.cipher.decrypt(row["client_secret_encrypted"]).decode("utf-8"),
            bot_id=row["bot_id"], api_base_url=row["api_base_url"], intents=int(row["intents"]),
        ) for row in rows]

    def connection_status(self, agent_id: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            row = self.db.execute("SELECT agent_id, owner_user_id, app_id, bot_id, api_base_url, intents, status, last_error, updated_at FROM qq_connections WHERE agent_id = ?", (agent_id,)).fetchone()
        return dict(row) if row else None

    def set_connection_status(self, agent_id: str, status: str, error: str = "", bot_id: Optional[str] = None) -> None:
        with self.lock:
            if bot_id is None:
                self.db.execute("UPDATE qq_connections SET status = ?, last_error = ?, updated_at = ? WHERE agent_id = ?", (status, error[:500], _now(), agent_id))
            else:
                self.db.execute("UPDATE qq_connections SET status = ?, bot_id = ?, last_error = ?, updated_at = ? WHERE agent_id = ?", (status, bot_id, error[:500], _now(), agent_id))
            self.db.commit()

    def delete_connection(self, agent_id: str) -> bool:
        with self.lock:
            result = self.db.execute("DELETE FROM qq_connections WHERE agent_id = ?", (agent_id,))
            self.db.commit()
        return result.rowcount > 0


@dataclass
class NormalizedEvent:
    event_key: str
    event_id: str
    event_type: str
    scope_type: str
    scope_id: str
    sender_id: str
    content: str
    bot_id: str
    message_id: str
    received_at: float = 0.0

    def payload(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class QQConnectionConfig:
    agent_id: str
    owner_user_id: int
    app_id: str
    client_secret: str
    bot_id: str = ""
    api_base_url: str = "https://api.bot.qq.com"
    intents: int = GROUP_AND_C2C_EVENT_INTENT


class QQApiClient:
    def __init__(self, config: QQConnectionConfig) -> None:
        self.config = config
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0))
        self.token = ""
        self.token_expires_at = 0.0
        self.lock = asyncio.Lock()

    async def close(self) -> None:
        await self.client.aclose()

    @staticmethod
    def _token_from_body(body: Any) -> Tuple[str, int]:
        if not isinstance(body, dict):
            raise RuntimeError("QQ token response is not a JSON object")
        data = body.get("data", body)
        if not isinstance(data, dict):
            data = {}
        token = data.get("access_token") or data.get("accessToken") or data.get("token")
        if not token:
            code = body.get("code")
            message = body.get("message") or body.get("msg") or body.get("error")
            detail = "QQ token response does not contain access_token"
            if code is not None or message:
                detail += " (code={}, message={})".format(code if code is not None else "unknown", str(message or "unknown")[:200])
            raise RuntimeError(detail)
        try:
            expires_in = max(60, int(data.get("expires_in", data.get("expiresIn", 7200))))
        except (TypeError, ValueError):
            expires_in = 7200
        return str(token), expires_in

    async def access_token(self, force: bool = False) -> str:
        async with self.lock:
            if self.token and not force and _now() < self.token_expires_at:
                return self.token
            response = await self.client.post(
                settings.token_api_base_url + "/app/getAppAccessToken",
                json={"appId": self.config.app_id, "clientSecret": self.config.client_secret},
            )
            response.raise_for_status()
            token, expires_in = self._token_from_body(response.json())
            self.token = str(token)
            self.token_expires_at = _now() + expires_in - 300
            return self.token

    async def send_message(self, event: NormalizedEvent, content: str) -> str:
        path = "/v2/groups/{}/messages" if event.scope_type == "group" else "/v2/users/{}/messages"
        url = self.config.api_base_url + path.format(event.scope_id)
        payload = {"msg_type": 0, "content": content[:settings.max_content_length], "msg_id": event.message_id or event.event_id}
        last_error = ""
        for attempt in range(3):
            token = await self.access_token(force=attempt > 0 and last_error == "401")
            response = await self.client.post(url, headers={"Authorization": "QQBot " + token}, json=payload)
            if response.status_code == 401:
                last_error = "401"
                continue
            if response.status_code == 429 or response.status_code >= 500:
                last_error = "QQ API HTTP {}".format(response.status_code)
                await asyncio.sleep(min(4.0, 0.5 * (2 ** attempt)))
                continue
            response.raise_for_status()
            body = response.json() if response.content else {}
            return str(body.get("id", body.get("message_id", "")))
        raise RuntimeError(last_error or "QQ message send failed")

    async def gateway_url(self) -> str:
        token = await self.access_token()
        response = await self.client.get(
            # /websocket/ is the returned WebSocket upgrade endpoint, not an
            # HTTP discovery endpoint. Fetch the WSS URL from /gateway first.
            self.config.api_base_url + "/gateway",
            headers={"Authorization": "QQBot " + token},
        )
        response.raise_for_status()
        body = response.json()
        data = body.get("data", body) if isinstance(body, dict) else {}
        url = data.get("url") or data.get("websocket_url")
        if not url:
            raise RuntimeError("QQ websocket response does not contain url")
        url = str(url)
        if urlparse(url).scheme not in {"ws", "wss"}:
            raise RuntimeError("QQ gateway response must contain a ws:// or wss:// URL")
        return url


settings = Settings()
store: Optional[GatewayStore] = None
background_tasks: Set[asyncio.Task] = set()
scope_locks: Dict[str, asyncio.Lock] = {}
scope_locks_lock = asyncio.Lock()
runtimes: Dict[str, "QQRuntime"] = {}
runtime_lock = asyncio.Lock()
app = FastAPI(title="QQ Agent Gateway", version="0.1.0")


def identify_payload(token: str, intents: int) -> Dict[str, Any]:
    return {
        "op": OP_IDENTIFY,
        "d": {
            "token": token,
            "intents": intents,
            "shard": [0, 1],
            "properties": {"$os": "linux", "$browser": "agentWorkCluster", "$device": "agentWorkCluster"},
        },
    }


def resume_payload(token: str, session_id: str, sequence: int) -> Dict[str, Any]:
    return {"op": OP_RESUME, "d": {"token": token, "session_id": session_id, "seq": sequence}}


def heartbeat_payload(sequence: Optional[int]) -> Dict[str, Any]:
    return {"op": OP_HEARTBEAT, "d": sequence}


async def scope_lock_for(scope_key: str) -> asyncio.Lock:
    async with scope_locks_lock:
        lock = scope_locks.get(scope_key)
        if lock is None:
            lock = asyncio.Lock()
            scope_locks[scope_key] = lock
        return lock


def _signature(event_ts: str, plain_token: str, client_secret: Optional[str] = None) -> str:
    message = (event_ts + plain_token).encode("utf-8")
    if settings.signature_mode == "none":
        return ""
    if settings.signature_mode == "hmac-sha256":
        return hmac.new((client_secret or settings.client_secret).encode("utf-8"), message, hashlib.sha256).hexdigest()
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(client_secret or settings.client_secret))
        return key.sign(message).hex()
    except (ValueError, TypeError) as exc:
        raise RuntimeError("QQ_CLIENT_SECRET must be a 32-byte hex seed for ed25519 webhook validation") from exc


def normalize_event(bot_id: str, payload: Dict[str, Any]) -> Optional[NormalizedEvent]:
    event_type = str(payload.get("t") or payload.get("event_type") or "")
    scope_type = SUPPORTED_EVENTS.get(event_type)
    if scope_type is None:
        return None
    data = payload.get("d") if isinstance(payload.get("d"), dict) else payload
    event_id = str(payload.get("id") or data.get("id") or data.get("event_id") or "")
    if not event_id:
        return None
    if scope_type == "group":
        scope_id = str(data.get("group_openid") or "")
        author = data.get("author") if isinstance(data.get("author"), dict) else {}
        sender_id = str(author.get("member_openid") or data.get("openid") or "")
    else:
        author = data.get("author") if isinstance(data.get("author"), dict) else {}
        # C2C_MESSAGE_CREATE places the user's openid under author.user_openid
        # in the current QQ Gateway payload; retain older top-level aliases.
        scope_id = str(author.get("user_openid") or data.get("openid") or data.get("user_openid") or "")
        sender_id = str(author.get("user_openid") or scope_id)
    content = MENTION_RE.sub("", str(data.get("content") or "")).strip()
    if not scope_id or not content:
        return None
    event_key = "qq:{}:{}".format(bot_id, event_id)
    return NormalizedEvent(event_key, event_id, event_type, scope_type, scope_id, sender_id, content[:settings.max_content_length], bot_id, str(data.get("id") or event_id), _now())


async def submit_to_agent(event: NormalizedEvent, config: QQConnectionConfig, conversation_id: Optional[str]) -> Dict[str, Any]:
    payload = {
        "provider": "qq", "bot_id": event.bot_id, "event_id": event.event_id, "event_type": event.event_type,
        "scope_type": event.scope_type, "scope_id": event.scope_id, "sender_id": event.sender_id,
        "content": event.content, "agent_id": config.agent_id, "owner_user_id": config.owner_user_id,
        "conversation_id": conversation_id, "title": "QQ {} {}".format(event.scope_type, event.scope_id),
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            settings.agent_url + "/internal/v1/channel-events", json=payload,
            headers={"Authorization": "Service " + settings.agent_service_secret},
        )
        response.raise_for_status()
        return response.json()


def event_passive_deadline(event: NormalizedEvent) -> float:
    # Old persisted payloads did not include received_at; give those a conservative fresh window.
    received_at = event.received_at if event.received_at > 0 else _now()
    return received_at + settings.passive_reply_window


async def wait_for_run(run_id: str, config: QQConnectionConfig, event: NormalizedEvent) -> Dict[str, Any]:
    deadline = min(_now() + settings.run_timeout, event_passive_deadline(event))
    async with httpx.AsyncClient(timeout=10.0) as client:
        while _now() < deadline:
            response = await client.get(
                settings.agent_url + "/internal/v1/channel-runs/" + run_id,
                params={"owner_user_id": config.owner_user_id},
                headers={"Authorization": "Service " + settings.agent_service_secret},
            )
            response.raise_for_status()
            body = response.json()
            assert store is not None
            store.touch_event(event.event_key)
            if body.get("state") in {"completed", "failed", "cancelled"}:
                return body
            await asyncio.sleep(1.0)
    raise TimeoutError("Agent run timed out")


async def process_event(event: NormalizedEvent, runtime: "QQRuntime") -> None:
    assert store is not None
    config = runtime.config
    scope_key = "{}:{}:{}".format(event.bot_id, event.scope_type, event.scope_id)
    try:
        if _now() >= event_passive_deadline(event):
            store.expire_event(event.event_key, "QQ passive reply window expired before processing")
            runtime.record_event("expired", event, reason="before_processing")
            return
        store.touch_event(event.event_key)
        async with await scope_lock_for(scope_key):
            conversation_id = store.get_conversation(scope_key)
            result = await submit_to_agent(event, config, conversation_id)
            if not conversation_id and result.get("conversation_id"):
                store.save_conversation(scope_key, result["conversation_id"], config.agent_id, config.owner_user_id)
        run_id = str(result["run_id"])
        store.mark_run(event.event_key, run_id)
        runtime.record_event("agent_submitted", event, run_id=run_id)
        run = await wait_for_run(run_id, config, event)
        runtime.record_event("run_completed", event, run_id=run_id, run_state=str(run.get("state") or "unknown"))
        if _now() >= event_passive_deadline(event):
            store.expire_event(event.event_key, "QQ passive reply window expired before reply")
            runtime.record_event("expired", event, run_id=run_id, reason="before_reply")
            return
        content = str(run.get("final_content") or "").strip() or "抱歉，这次处理没有生成可发送的回复。"
        outbound_claim = store.claim_outbound(event.event_key)
        if outbound_claim == "sent":
            store.complete_event(event.event_key)
            return
        if outbound_claim == "inflight":
            runtime.record_event("outbound_inflight", event, run_id=run_id)
            return
        try:
            message_id = await runtime.api.send_message(event, content)
            store.complete_outbound(event.event_key, message_id)
            store.complete_event(event.event_key)
            runtime.record_event("reply_sent", event, run_id=run_id, message_id=message_id)
        except Exception as exc:
            store.fail_outbound(event.event_key, str(exc))
            raise
    except Exception as exc:
        store.fail_event(event.event_key, str(exc))
        runtime.record_event("failed", event, error_type=type(exc).__name__)


class QQRuntime:
    """One durable Agent-to-QQ connection managed through the QQ WebSocket gateway."""

    def __init__(self, config: QQConnectionConfig) -> None:
        self.config = config
        self.api = QQApiClient(config)
        self.stop_event = asyncio.Event()
        self.task: Optional[asyncio.Task] = None
        # Keep the gateway session in memory so short reconnects can use Resume.
        self.session_id = ""
        self.sequence: Optional[int] = None
        self.event_slots = asyncio.Semaphore(settings.max_event_tasks)
        self.awaiting_heartbeat_ack = False
        self.last_heartbeat_at = 0.0
        self.last_heartbeat_ack_at = 0.0
        self.reconnect_count = 0
        self.resume_count = 0
        self.dispatches_received = 0
        self.events_received = 0
        self.events_ignored = 0
        self.events_scheduled = 0
        self.events_claimed = 0
        self.events_duplicates = 0
        self.events_agent_submitted = 0
        self.events_run_completed = 0
        self.events_reply_sent = 0
        self.events_failed = 0
        self.events_expired = 0
        self.last_dispatch_at = 0.0
        self.last_opcode = 0
        self.active_event_keys: Set[str] = set()

    def record_event(self, stage: str, event: NormalizedEvent, **fields: Any) -> None:
        metric_name = "events_" + stage
        if hasattr(self, metric_name):
            setattr(self, metric_name, getattr(self, metric_name) + 1)
        payload = {
            "stage": stage,
            "agent_id": self.config.agent_id,
            "bot_id": event.bot_id,
            "event_id": event.event_id,
            "event_type": event.event_type,
            "scope_type": event.scope_type,
            "run_id": fields.pop("run_id", ""),
            **fields,
        }
        event_logger.info("qq_event %s", json.dumps(payload, separators=(",", ":"), ensure_ascii=False))

    def start(self) -> None:
        self.task = asyncio.create_task(self.run())
        background_tasks.add(self.task)
        self.task.add_done_callback(background_tasks.discard)

    async def stop(self) -> None:
        self.stop_event.set()
        if self.task and self.task is not asyncio.current_task():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await self.api.close()

    async def run(self) -> None:
        delay = 1.0
        while not self.stop_event.is_set():
            try:
                assert store is not None
                store.set_connection_status(self.config.agent_id, "connecting")
                url = await self.api.gateway_url()
                async with websockets.connect(url, ping_interval=None, close_timeout=5, max_size=2 ** 20) as socket:
                    protocol_close = False
                    hello = json.loads(await socket.recv())
                    if not isinstance(hello, dict) or int(hello.get("op", -1)) != OP_HELLO:
                        raise RuntimeError("QQ gateway did not send Hello opcode 10")
                    data = hello.get("d") if isinstance(hello, dict) else {}
                    if not isinstance(data, dict) or not data.get("heartbeat_interval"):
                        raise RuntimeError("QQ Hello does not contain heartbeat_interval")
                    interval = max(1.0, float(data.get("heartbeat_interval", 45000)) / 1000.0)
                    token = "QQBot " + await self.api.access_token()
                    if self.session_id and self.sequence is not None:
                        await socket.send(json.dumps(resume_payload(token, self.session_id, self.sequence)))
                        self.resume_count += 1
                    else:
                        await socket.send(json.dumps(identify_payload(token, self.config.intents)))
                    store.set_connection_status(self.config.agent_id, "connecting")
                    delay = 1.0
                    heartbeat_at = _now() + interval
                    self.awaiting_heartbeat_ack = False
                    while not self.stop_event.is_set():
                        try:
                            raw = await asyncio.wait_for(socket.recv(), timeout=max(0.0, heartbeat_at - _now()))
                        except asyncio.TimeoutError:
                            if self.awaiting_heartbeat_ack:
                                raise RuntimeError("QQ heartbeat ACK timed out")
                            await socket.send(json.dumps(heartbeat_payload(self.sequence)))
                            self.awaiting_heartbeat_ack = True
                            self.last_heartbeat_at = _now()
                            heartbeat_at = _now() + interval
                            continue
                        if raw is None:
                            protocol_close = True
                            break
                        payload = json.loads(raw)
                        op = int(payload.get("op", 0))
                        self.last_opcode = op
                        if op == OP_DISPATCH:
                            self.dispatches_received += 1
                            self.last_dispatch_at = _now()
                            if payload.get("s") is not None:
                                self.sequence = int(payload["s"])
                            event_type = str(payload.get("t") or "")
                            event_data = payload.get("d") if isinstance(payload.get("d"), dict) else {}
                            if event_type == "READY":
                                self.session_id = str(event_data.get("session_id") or self.session_id)
                                store.set_connection_status(self.config.agent_id, "connected")
                                user = event_data.get("user") if isinstance(event_data.get("user"), dict) else {}
                                bot_id = str(user.get("id") or self.config.bot_id or self.config.app_id)
                                if bot_id != self.config.bot_id:
                                    self.config.bot_id = bot_id
                                    store.set_connection_status(self.config.agent_id, "connected", bot_id=bot_id)
                            elif event_type == "RESUMED":
                                store.set_connection_status(self.config.agent_id, "connected")
                            event = normalize_event(self.config.bot_id or self.config.app_id, payload)
                            if event is not None:
                                assert store is not None
                                if event.event_key in self.active_event_keys:
                                    self.events_duplicates += 1
                                    self.record_event("duplicate", event, reason="active")
                                    continue
                                claim = store.claim_event(event.event_key, event.payload())
                                if claim == "claimed":
                                    if _schedule(event, self):
                                        self.events_claimed += 1
                                        self.events_scheduled += 1
                                        self.record_event("received", event)
                                else:
                                    self.events_duplicates += 1
                                    self.record_event("duplicate", event)
                            else:
                                self.events_ignored += 1
                        elif op == OP_HEARTBEAT:
                            await socket.send(json.dumps(heartbeat_payload(self.sequence)))
                            self.awaiting_heartbeat_ack = True
                            self.last_heartbeat_at = _now()
                            heartbeat_at = _now() + interval
                        elif op == OP_HEARTBEAT_ACK:
                            self.awaiting_heartbeat_ack = False
                            self.last_heartbeat_ack_at = _now()
                        elif op == OP_INVALID_SESSION:
                            # Invalid Session includes a boolean indicating resumability.
                            if payload.get("d") is not True:
                                self.session_id = ""
                                self.sequence = None
                            protocol_close = True
                            break
                        elif op == OP_RECONNECT:
                            protocol_close = True
                            break
                    if protocol_close and not self.stop_event.is_set():
                        self.reconnect_count += 1
                        try:
                            await asyncio.wait_for(self.stop_event.wait(), timeout=min(delay, 30.0))
                        except asyncio.TimeoutError:
                            pass
                        delay = min(delay * 2, 30.0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.reconnect_count += 1
                if store is not None:
                    store.set_connection_status(self.config.agent_id, "error", str(exc))
                if not self.stop_event.is_set():
                    try:
                        await asyncio.wait_for(self.stop_event.wait(), timeout=min(delay, 30.0))
                    except asyncio.TimeoutError:
                        pass
                delay = min(delay * 2, 30.0)
        if store is not None:
            store.set_connection_status(self.config.agent_id, "disconnected")


async def _run_event(event: NormalizedEvent, runtime: "QQRuntime") -> None:
    try:
        async with runtime.event_slots:
            await process_event(event, runtime)
    finally:
        runtime.active_event_keys.discard(event.event_key)


def _schedule(event: NormalizedEvent, runtime: "QQRuntime") -> bool:
    if event.event_key in runtime.active_event_keys:
        return False
    runtime.active_event_keys.add(event.event_key)
    task = asyncio.create_task(_run_event(event, runtime))
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    return True


async def retry_pending_events() -> None:
    assert store is not None
    for item in store.pending_events():
        event = NormalizedEvent(**item["payload"])
        runtime = next((item for item in runtimes.values() if item.config.bot_id == event.bot_id or item.config.app_id == event.bot_id), None)
        if runtime is not None and event.event_key not in runtime.active_event_keys and store.claim_event(event.event_key, event.payload()) == "claimed":
            _schedule(event, runtime)


async def retry_loop() -> None:
    while True:
        await asyncio.sleep(settings.retry_interval_seconds)
        await retry_pending_events()


async def start_connection(config: QQConnectionConfig) -> QQRuntime:
    assert store is not None
    store.save_connection(config)
    async with runtime_lock:
        previous = runtimes.pop(config.agent_id, None)
        if previous is not None:
            await previous.stop()
        runtime = QQRuntime(config)
        runtimes[config.agent_id] = runtime
        runtime.start()
        return runtime


async def stop_connection(agent_id: str, remove: bool = True) -> bool:
    async with runtime_lock:
        runtime = runtimes.pop(agent_id, None)
        if runtime is not None:
            await runtime.stop()
        if remove and store is not None:
            return store.delete_connection(agent_id)
        return runtime is not None


def runtime_for_bot(bot_id: str) -> Optional[QQRuntime]:
    for runtime in runtimes.values():
        if bot_id in {runtime.config.bot_id, runtime.config.app_id}:
            return runtime
    return None


@app.on_event("startup")
async def startup() -> None:
    global store
    settings.validate()
    store = GatewayStore(settings.database_path, settings.master_key)
    configs = store.load_connections()
    if all(value and not value.startswith("replace-with-") for value in (settings.app_id, settings.client_secret, settings.agent_id, settings.owner_user_id)):
        legacy = QQConnectionConfig(
            agent_id=settings.agent_id, owner_user_id=int(settings.owner_user_id), app_id=settings.app_id,
            client_secret=settings.client_secret, bot_id=settings.bot_id, api_base_url=settings.api_base_url,
            intents=settings.intents,
        )
        configs = [item for item in configs if item.agent_id != legacy.agent_id] + [legacy]
    for config in configs:
        await start_connection(config)
    await retry_pending_events()
    task = asyncio.create_task(retry_loop())
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)


@app.on_event("shutdown")
async def shutdown() -> None:
    for agent_id in list(runtimes):
        await stop_connection(agent_id, remove=False)
    for task in list(background_tasks):
        task.cancel()
    if background_tasks:
        await asyncio.gather(*background_tasks, return_exceptions=True)
    if store is not None:
        store.db.close()


@app.get("/healthz")
async def healthz() -> Dict[str, Any]:
    connected = sum(1 for runtime in runtimes.values() if (store and (store.connection_status(runtime.config.agent_id) or {}).get("status") == "connected"))
    return {
        "status": "ok", "configured": bool(runtimes), "connections": len(runtimes), "connected": connected,
        "connection_details": [
            {
                "agent_id": runtime.config.agent_id,
                "status": (store.connection_status(runtime.config.agent_id) or {}).get("status", "unknown") if store else "unknown",
                "bot_id": runtime.config.bot_id,
                "session_active": bool(runtime.session_id),
                "sequence": runtime.sequence,
                "reconnects": runtime.reconnect_count,
                "resumes": runtime.resume_count,
                "heartbeat_ack_pending": runtime.awaiting_heartbeat_ack,
                "last_opcode": runtime.last_opcode,
                "last_dispatch_at": runtime.last_dispatch_at,
                "dispatches_received": runtime.dispatches_received,
                "events_received": runtime.events_received,
                "events_ignored": runtime.events_ignored,
                "events_claimed": runtime.events_claimed,
                "events_duplicates": runtime.events_duplicates,
                "events_scheduled": runtime.events_scheduled,
                "events_agent_submitted": runtime.events_agent_submitted,
                "events_run_completed": runtime.events_run_completed,
                "events_reply_sent": runtime.events_reply_sent,
                "events_failed": runtime.events_failed,
                "events_expired": runtime.events_expired,
            }
            for runtime in runtimes.values()
        ],
    }


async def require_service(authorization: Optional[str]) -> None:
    if not authorization or not authorization.startswith("Service ") or not secrets.compare_digest(authorization[8:].strip(), settings.agent_service_secret):
        raise HTTPException(status_code=401, detail="服务认证无效")


@app.get("/internal/v1/qq/connections/{agent_id}")
async def get_connection(agent_id: str, authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    await require_service(authorization)
    if store is None:
        raise HTTPException(status_code=503, detail="QQ Gateway 尚未启动")
    status = store.connection_status(agent_id)
    if status is None:
        return {"agent_id": agent_id, "configured": False, "status": "disconnected"}
    status["configured"] = True
    status.pop("app_id", None)
    return status


@app.post("/internal/v1/qq/connections")
async def connect_connection(request: Request, authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    await require_service(authorization)
    try:
        payload = json.loads((await request.body()).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="无效的 QQ 连接配置") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="QQ 连接配置必须是对象")
    agent_id, app_id, client_secret = str(payload.get("agent_id") or "").strip(), str(payload.get("app_id") or "").strip(), str(payload.get("client_secret") or "")
    owner_user_id = payload.get("owner_user_id")
    if not agent_id or not app_id or not client_secret or not isinstance(owner_user_id, int) or owner_user_id < 1:
        raise HTTPException(status_code=422, detail="QQ 连接配置缺少 agent_id、owner_user_id、app_id 或 client_secret")
    intents = int(payload.get("intents", settings.intents))
    if not 1 <= intents <= MAX_QQ_INTENTS:
        raise HTTPException(status_code=422, detail="QQ intents 无效")
    config = QQConnectionConfig(
        agent_id=agent_id, owner_user_id=owner_user_id, app_id=app_id, client_secret=client_secret,
        bot_id=str(payload.get("bot_id") or "").strip(), api_base_url=settings.api_base_url, intents=intents,
    )
    await start_connection(config)
    return {"agent_id": agent_id, "configured": True, "status": "connecting", "app_id": app_id, "bot_id": config.bot_id}


@app.delete("/internal/v1/qq/connections/{agent_id}")
async def disconnect_connection(agent_id: str, authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    await require_service(authorization)
    await stop_connection(agent_id)
    return {"agent_id": agent_id, "configured": False, "status": "disconnected"}


@app.post("/qq/webhook/{bot_id}")
async def webhook(bot_id: str, request: Request) -> JSONResponse:
    raise HTTPException(status_code=410, detail="QQ Gateway 已切换为 WebSocket-only，不再支持 Webhook")


@app.post("/qq/webhook")
async def webhook_default(request: Request) -> JSONResponse:
    raise HTTPException(status_code=410, detail="QQ Gateway 已切换为 WebSocket-only，不再支持 Webhook")
