"""QQ Bot Webhook gateway for the Agent platform."""

import asyncio
import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
from dataclasses import asdict, dataclass
from threading import RLock
from typing import Any, Dict, List, Optional, Set

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse


MENTION_RE = re.compile(r"<@!?[A-Za-z0-9_-]+>")
SUPPORTED_EVENTS = {"GROUP_AT_MESSAGE_CREATE": "group", "C2C_MESSAGE_CREATE": "c2c"}


def _now() -> float:
    return time.time()


class Settings:
    def __init__(self) -> None:
        self.app_id = os.getenv("QQ_APP_ID", "")
        self.client_secret = os.getenv("QQ_CLIENT_SECRET", "")
        self.api_base_url = os.getenv("QQ_API_BASE_URL", "https://api.bot.qq.com").rstrip("/")
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

    def validate(self) -> None:
        required = {
            "QQ_APP_ID": self.app_id,
            "QQ_CLIENT_SECRET": self.client_secret,
            "AGENT_SERVICE_SECRET": self.agent_service_secret,
            "QQ_DEFAULT_AGENT_ID": self.agent_id,
            "QQ_DEFAULT_OWNER_USER_ID": self.owner_user_id,
            "QQ_GATEWAY_MASTER_KEY": self.master_key,
        }
        missing = [name for name, value in required.items() if not value or value.startswith("replace-with-")]
        if missing:
            raise RuntimeError("Missing QQ gateway configuration: " + ", ".join(missing))
        try:
            int(self.owner_user_id)
        except ValueError as exc:
            raise RuntimeError("QQ_DEFAULT_OWNER_USER_ID must be an integer") from exc
        if self.signature_mode not in {"ed25519", "hmac-sha256", "none"}:
            raise RuntimeError("QQ_WEBHOOK_SIGNATURE_MODE must be ed25519, hmac-sha256, or none")
        if min(self.run_timeout, self.retry_after_seconds, self.passive_reply_window, self.retry_interval_seconds) <= 0:
            raise RuntimeError("QQ Gateway timeout and retry settings must be positive")

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

    def claim_outbound(self, event_key: str) -> bool:
        with self.lock:
            row = self.db.execute("SELECT status FROM outbound_messages WHERE event_key = ?", (event_key,)).fetchone()
            if row is not None and row["status"] == "sent":
                return False
            if row is None:
                self.db.execute("INSERT INTO outbound_messages(event_key, status, attempts, updated_at) VALUES (?, 'sending', 1, ?)", (event_key, _now()))
            else:
                self.db.execute("UPDATE outbound_messages SET status = 'sending', attempts = attempts + 1, updated_at = ? WHERE event_key = ?", (_now(), event_key))
            self.db.commit()
            return True

    def complete_outbound(self, event_key: str, message_id: str = "") -> None:
        with self.lock:
            self.db.execute("UPDATE outbound_messages SET status = 'sent', provider_message_id = ?, updated_at = ? WHERE event_key = ?", (message_id, _now(), event_key))
            self.db.commit()

    def fail_outbound(self, event_key: str, error: str) -> None:
        with self.lock:
            self.db.execute("UPDATE outbound_messages SET status = 'failed', last_error = ?, updated_at = ? WHERE event_key = ?", (error[:500], _now(), event_key))
            self.db.commit()


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


class QQApiClient:
    def __init__(self) -> None:
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0))
        self.token = ""
        self.token_expires_at = 0.0
        self.lock = asyncio.Lock()

    async def close(self) -> None:
        await self.client.aclose()

    async def access_token(self, force: bool = False) -> str:
        async with self.lock:
            if self.token and not force and _now() < self.token_expires_at:
                return self.token
            response = await self.client.post(
                settings.api_base_url + "/app/getAppAccessToken",
                json={"appId": settings.app_id, "clientSecret": settings.client_secret},
            )
            response.raise_for_status()
            body = response.json()
            data = body.get("data", body) if isinstance(body, dict) else {}
            token = data.get("access_token")
            if not token:
                raise RuntimeError("QQ token response does not contain access_token")
            expires_in = max(60, int(data.get("expires_in", 7200)))
            self.token = str(token)
            self.token_expires_at = _now() + expires_in - 300
            return self.token

    async def send_message(self, event: NormalizedEvent, content: str) -> str:
        path = "/v2/groups/{}/messages" if event.scope_type == "group" else "/v2/users/{}/messages"
        url = settings.api_base_url + path.format(event.scope_id)
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


settings = Settings()
store: Optional[GatewayStore] = None
qq_api = QQApiClient()
background_tasks: Set[asyncio.Task] = set()
scope_lock = asyncio.Lock()
app = FastAPI(title="QQ Agent Gateway", version="0.1.0")


def _signature(event_ts: str, plain_token: str) -> str:
    message = (event_ts + plain_token).encode("utf-8")
    if settings.signature_mode == "none":
        return ""
    if settings.signature_mode == "hmac-sha256":
        return hmac.new(settings.client_secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(settings.client_secret))
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
        scope_id = str(data.get("openid") or data.get("user_openid") or "")
        sender_id = scope_id
    content = MENTION_RE.sub("", str(data.get("content") or "")).strip()
    if not scope_id or not content:
        return None
    event_key = "qq:{}:{}".format(bot_id, event_id)
    return NormalizedEvent(event_key, event_id, event_type, scope_type, scope_id, sender_id, content[:settings.max_content_length], bot_id, str(data.get("id") or event_id), _now())


async def submit_to_agent(event: NormalizedEvent, conversation_id: Optional[str]) -> Dict[str, Any]:
    payload = {
        "provider": "qq", "bot_id": event.bot_id, "event_id": event.event_id, "event_type": event.event_type,
        "scope_type": event.scope_type, "scope_id": event.scope_id, "sender_id": event.sender_id,
        "content": event.content, "agent_id": settings.agent_id, "owner_user_id": int(settings.owner_user_id),
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


async def wait_for_run(run_id: str, event: NormalizedEvent) -> Dict[str, Any]:
    deadline = min(_now() + settings.run_timeout, event_passive_deadline(event))
    async with httpx.AsyncClient(timeout=10.0) as client:
        while _now() < deadline:
            response = await client.get(
                settings.agent_url + "/internal/v1/channel-runs/" + run_id,
                params={"owner_user_id": settings.owner_user_id},
                headers={"Authorization": "Service " + settings.agent_service_secret},
            )
            response.raise_for_status()
            body = response.json()
            if body.get("state") in {"completed", "failed", "cancelled"}:
                return body
            await asyncio.sleep(1.0)
    raise TimeoutError("Agent run timed out")


async def process_event(event: NormalizedEvent) -> None:
    assert store is not None
    scope_key = "{}:{}:{}".format(event.bot_id, event.scope_type, event.scope_id)
    try:
        if _now() >= event_passive_deadline(event):
            store.expire_event(event.event_key, "QQ passive reply window expired before processing")
            return
        async with scope_lock:
            conversation_id = store.get_conversation(scope_key)
            result = await submit_to_agent(event, conversation_id)
            if not conversation_id and result.get("conversation_id"):
                store.save_conversation(scope_key, result["conversation_id"], settings.agent_id, int(settings.owner_user_id))
        run_id = str(result["run_id"])
        store.mark_run(event.event_key, run_id)
        run = await wait_for_run(run_id, event)
        if _now() >= event_passive_deadline(event):
            store.expire_event(event.event_key, "QQ passive reply window expired before reply")
            return
        content = str(run.get("final_content") or "").strip() or "抱歉，这次处理没有生成可发送的回复。"
        if not store.claim_outbound(event.event_key):
            store.complete_event(event.event_key)
            return
        try:
            message_id = await qq_api.send_message(event, content)
            store.complete_outbound(event.event_key, message_id)
            store.complete_event(event.event_key)
        except Exception as exc:
            store.fail_outbound(event.event_key, str(exc))
            raise
    except Exception as exc:
        store.fail_event(event.event_key, str(exc))


def _schedule(event: NormalizedEvent) -> None:
    task = asyncio.create_task(process_event(event))
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)


async def retry_pending_events() -> None:
    assert store is not None
    for item in store.pending_events():
        event = NormalizedEvent(**item["payload"])
        if store.claim_event(event.event_key, event.payload()) == "claimed":
            _schedule(event)


async def retry_loop() -> None:
    while True:
        await asyncio.sleep(settings.retry_interval_seconds)
        await retry_pending_events()


@app.on_event("startup")
async def startup() -> None:
    global store
    settings.validate()
    store = GatewayStore(settings.database_path, settings.master_key)
    await retry_pending_events()
    task = asyncio.create_task(retry_loop())
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)


@app.on_event("shutdown")
async def shutdown() -> None:
    for task in list(background_tasks):
        task.cancel()
    if background_tasks:
        await asyncio.gather(*background_tasks, return_exceptions=True)
    await qq_api.close()
    if store is not None:
        store.db.close()


@app.get("/healthz")
async def healthz() -> Dict[str, Any]:
    return {"status": "ok", "configured": bool(settings.app_id and settings.agent_id and settings.agent_service_secret)}


@app.post("/qq/webhook/{bot_id}")
async def webhook(bot_id: str, request: Request) -> JSONResponse:
    if settings.bot_id and bot_id != settings.bot_id:
        raise HTTPException(status_code=404, detail="QQ bot 未找到")
    try:
        payload = json.loads((await request.body()).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="无效的 QQ 事件 JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="QQ 事件必须是对象")
    if int(payload.get("op", 0)) == 13:
        data = payload.get("d") if isinstance(payload.get("d"), dict) else payload
        plain_token, event_ts = str(data.get("plain_token", "")), str(data.get("event_ts", ""))
        if not plain_token or not event_ts:
            raise HTTPException(status_code=400, detail="QQ 验证事件缺少字段")
        return JSONResponse({"plain_token": plain_token, "signature": _signature(event_ts, plain_token)})
    event = normalize_event(bot_id, payload)
    if event is None:
        return JSONResponse({"accepted": False, "reason": "unsupported_or_empty_event"}, status_code=202)
    assert store is not None
    if store.claim_event(event.event_key, event.payload()) == "claimed":
        _schedule(event)
    return JSONResponse({"accepted": True, "event_id": event.event_id}, status_code=202)


@app.post("/qq/webhook")
async def webhook_default(request: Request) -> JSONResponse:
    return await webhook(settings.bot_id or settings.app_id, request)
