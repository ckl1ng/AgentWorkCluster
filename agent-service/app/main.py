import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Body, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from jsonschema import Draft202012Validator
from pydantic import BaseModel, Field

from .evaluation import FAILURE_CATEGORIES
from .db import Connection, Row
from .harness import (
    ModelTurn, discover_mcp_stdio, execute_http_tool, execute_local_tool, execute_mcp_tool,
    execute_stdio_mcp_tool, prepare_context, safe_http_transport, stream_chat, tool_declarations,
)
from .safety import audit_payload, assert_public_peer, assert_safe_public_url, openapi_operations, redact, require_object_schema
from .state_machine import require_transition
from .task_state_machine import require_transition as require_task_transition


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def now_offset(days: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat().replace("+00:00", "Z")


def credential_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def opaque_token(prefix: str) -> str:
    return "{}_{}".format(prefix, secrets.token_urlsafe(32))


class Settings:
    def __init__(self) -> None:
        self.database_path = os.getenv("AGENT_DATABASE_PATH", "./data/agents.db")
        self.database_url = os.getenv("AGENT_DATABASE_URL", "")
        if not self.database_url and os.getenv("PGHOST"):
            self.database_url = "postgresql://{}:{}@{}:{}/{}".format(
                quote(os.getenv("PGUSER", "agent"), safe=""), quote(os.getenv("PGPASSWORD", ""), safe=""),
                os.environ["PGHOST"], os.getenv("PGPORT", "5432"), os.getenv("PGDATABASE", "agent"),
            )
        self.auth_url = os.getenv(
            "CHAT_AUTH_INTROSPECTION_URL",
            "http://127.0.0.1:9010/internal/v1/auth/introspect",
        )
        self.service_secret = os.getenv("AGENT_SERVICE_SECRET", "")
        self.master_key = os.getenv("AGENT_MASTER_KEY", "")
        self.qq_gateway_url = os.getenv("QQ_GATEWAY_INTERNAL_URL", "http://127.0.0.1:9013").rstrip("/")
        self.allow_http = os.getenv("AGENT_ALLOW_HTTP", "false").lower() == "true"
        self.tool_response_limit = int(os.getenv("AGENT_TOOL_RESPONSE_LIMIT", str(1024 * 1024)))
        self.redis_url = os.getenv("REDIS_URL", "")

    def validate(self) -> None:
        if not self.service_secret:
            raise RuntimeError("AGENT_SERVICE_SECRET is required")
        if not self.master_key:
            raise RuntimeError("AGENT_MASTER_KEY is required")

BUILTIN_WEB_SEARCH_COMMAND = sys.executable
BUILTIN_WEB_SEARCH_ARGS = [os.path.join(os.path.dirname(__file__), "web_search_mcp.py")]

BUILTIN_TOOLS = [
    {
        "name": "current_time", "description": "获取当前 UTC 时间。", "kind": "local",
        "config": {"builtin": "current_time"},
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "confirmation_mode": "none", "side_effect": "read", "provider_version": "local-v1",
    },
    {
        "name": "search_web", "description": "使用多个搜索引擎搜索互联网。", "kind": "mcp_stdio",
        "config": {"command": BUILTIN_WEB_SEARCH_COMMAND, "args": BUILTIN_WEB_SEARCH_ARGS, "remote_tool_name": "search_web", "timeout_seconds": 30, "source": "simple-web-search-mcp"},
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}, "engines": {"type": "string"}, "max_results": {"type": "integer", "minimum": 1}, "think": {"type": "boolean"}}, "required": ["query"], "additionalProperties": False},
        "confirmation_mode": "none", "side_effect": "read", "provider_version": "mcp-stdio-v1",
    },
    {
        "name": "read_url", "description": "读取并提取网页内容。", "kind": "mcp_stdio",
        "config": {"command": BUILTIN_WEB_SEARCH_COMMAND, "args": BUILTIN_WEB_SEARCH_ARGS, "remote_tool_name": "read_url", "timeout_seconds": 30, "source": "simple-web-search-mcp"},
        "input_schema": {"type": "object", "properties": {"url": {"type": "string", "format": "uri"}}, "required": ["url"], "additionalProperties": False},
        "confirmation_mode": "none", "side_effect": "read", "provider_version": "mcp-stdio-v1",
    },
]

TASK_TOOL_DEFINITIONS = [
    {
        "id": "task:post_progress", "name": "post_progress", "kind": "task", "enabled": True,
        "description": "记录当前 Task 的可审计进展和短调度摘要。",
        "input_schema": {
            "type": "object", "properties": {
                "progress": {"type": "string", "minLength": 1, "maxLength": 5000},
                "dispatch_summary": {"type": "string", "maxLength": 280},
            }, "required": ["progress"], "additionalProperties": False,
        }, "confirmation_mode": "none", "side_effect": "read", "rate_limit_per_run": 20,
    },
    {
        "id": "task:submit_result", "name": "submit_result", "kind": "task", "enabled": True,
        "description": "提交 Task 的结构化结果，供提出者验收；不会关闭 Task。",
        "input_schema": {
            "type": "object", "properties": {
                "result": {"type": "string", "minLength": 1, "maxLength": 50000},
                "evidence_manifest": {"type": "object"},
                "risk_summary": {"type": "string", "maxLength": 2000},
            }, "required": ["result"], "additionalProperties": False,
        }, "confirmation_mode": "none", "side_effect": "read", "rate_limit_per_run": 1,
    },
    {
        "id": "task:request_proposer_decision", "name": "request_proposer_decision", "kind": "task", "enabled": True,
        "description": "在需求、权限、预算或执行条件不明确时请求提出者决策。",
        "input_schema": {
            "type": "object", "properties": {
                "reason": {"type": "string", "minLength": 1, "maxLength": 5000},
                "dispatch_summary": {"type": "string", "maxLength": 280},
            }, "required": ["reason"], "additionalProperties": False,
        }, "confirmation_mode": "none", "side_effect": "read", "rate_limit_per_run": 3,
    },
    {
        "id": "task:delegate_task", "name": "delegate_task", "kind": "task", "enabled": True,
        "description": "向指定 Cloud Agent 创建一个有独立上下文和预算的直系子任务。",
        "input_schema": {
            "type": "object", "properties": {
                "target_agent_id": {"type": "string", "minLength": 1, "maxLength": 64},
                "title": {"type": "string", "minLength": 1, "maxLength": 160},
                "goal": {"type": "string", "minLength": 1, "maxLength": 50000},
                "input_package": {"type": "string", "minLength": 1, "maxLength": 50000},
                "budget_snapshot": {"type": "object"},
                "reason": {"type": "string", "minLength": 1, "maxLength": 1000},
            }, "required": ["target_agent_id", "title", "goal", "input_package", "budget_snapshot", "reason"], "additionalProperties": False,
        }, "confirmation_mode": "none", "side_effect": "read", "rate_limit_per_run": 4,
    },
    {
        "id": "task:accept_assignment", "name": "accept_assignment", "kind": "task", "enabled": True,
        "description": "确认当前 Assignment 仍由本执行者接手。",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "confirmation_mode": "none", "side_effect": "read", "rate_limit_per_run": 1,
    },
    {
        "id": "task:decline_assignment", "name": "decline_assignment", "kind": "task", "enabled": True,
        "description": "拒绝当前 Assignment，并将任务交回提出者处理。",
        "input_schema": {"type": "object", "properties": {"reason": {"type": "string", "minLength": 1, "maxLength": 1000}}, "required": ["reason"], "additionalProperties": False},
        "confirmation_mode": "none", "side_effect": "read", "rate_limit_per_run": 1,
    },
    {
        "id": "task:collect_child_result", "name": "collect_child_result", "kind": "task", "enabled": True,
        "description": "读取直系子任务已提交的结构化结果，并作为显式输入包返回当前任务。",
        "input_schema": {"type": "object", "properties": {"child_task_id": {"type": "string", "minLength": 1, "maxLength": 64}}, "required": ["child_task_id"], "additionalProperties": False},
        "confirmation_mode": "none", "side_effect": "read", "rate_limit_per_run": 8,
    },
    {
        "id": "task:close_delegated_task", "name": "close_delegated_task", "kind": "task", "enabled": True,
        "description": "关闭由当前 Agent 提出的、已交付结果的直系子任务。",
        "input_schema": {"type": "object", "properties": {"child_task_id": {"type": "string", "minLength": 1, "maxLength": 64}, "result_summary": {"type": "string", "minLength": 1, "maxLength": 50000}}, "required": ["child_task_id", "result_summary"], "additionalProperties": False},
        "confirmation_mode": "none", "side_effect": "read", "rate_limit_per_run": 8,
    },
]

TASK_BUDGET_LIMITS = {
    "max_total_tokens": (1, 100_000_000),
    "max_tool_calls": (0, 100_000),
    "max_concurrent_runs": (1, 100),
    "max_depth": (1, 16),
    "max_subtasks": (1, 1_000),
}

settings = Settings()


class TaskAccessPolicy:
    """Authorize a principal against explicit Task ownership and Assignment records."""

    @staticmethod
    def can_read(task: Row, owner_id: int, principal: Dict[str, str], assignments: List[Row]) -> bool:
        if principal == {"kind": "user", "id": str(owner_id)}:
            return True
        if task["proposer_kind"] == principal["kind"] and task["proposer_id"] == principal["id"]:
            return True
        if task["proposer_kind"] == "agent" and principal["kind"] == "cloud_agent" and task["proposer_id"] == principal["id"]:
            return True
        return any(
            item["executor_kind"] == principal["kind"] and item["executor_id"] == principal["id"]
            for item in assignments
        )


class AgentStore:
    """Storage-agnostic Agent repository backed by PostgreSQL in production."""

    def __init__(self, target: str, master_key: str) -> None:
        if not target.startswith(("postgresql://", "postgres://")):
            parent = os.path.dirname(os.path.abspath(target))
            os.makedirs(parent, exist_ok=True)
        self.db = Connection(target)
        self.lock = threading.RLock()
        self.cipher = Fernet(master_key.encode("utf-8"))
        self._migrate()

    def _migrate(self) -> None:
        if self.db.postgres:
            # Production schema ownership belongs to Alembic. This cheap query
            # also makes startup fail before serving traffic if migration failed.
            self.db.execute("SELECT 1 FROM agents LIMIT 1")
            return
        with self.lock:
            self.db.executescript(
                """
                PRAGMA foreign_keys = ON;
                CREATE TABLE IF NOT EXISTS agents (
                  id TEXT PRIMARY KEY, owner_user_id INTEGER NOT NULL,
                  name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
                  avatar_url TEXT NOT NULL DEFAULT '', state TEXT NOT NULL,
                  current_version INTEGER NOT NULL, model_display_name TEXT NOT NULL,
                  model_base_url TEXT NOT NULL, model_id TEXT NOT NULL,
                  encrypted_api_key BLOB NOT NULL, temperature REAL NOT NULL,
                  max_tokens INTEGER NOT NULL, timeout_seconds INTEGER NOT NULL,
                  system_prompt TEXT NOT NULL DEFAULT '', encrypted_system_prompt BLOB,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_versions (
                  id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, version INTEGER NOT NULL,
                  snapshot TEXT NOT NULL DEFAULT '{}', snapshot_encrypted BLOB,
                  created_by_user_id INTEGER NOT NULL,
                  created_at TEXT NOT NULL, UNIQUE(agent_id, version)
                );
                CREATE TABLE IF NOT EXISTS conversations (
                  id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, owner_user_id INTEGER NOT NULL,
                  title TEXT NOT NULL, context_epoch INTEGER NOT NULL DEFAULT 0,
                  deleted_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                  id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, run_id TEXT,
                  role TEXT NOT NULL, content TEXT NOT NULL DEFAULT '', content_encrypted BLOB,
                  context_epoch INTEGER NOT NULL,
                  created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                  id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, agent_id TEXT NOT NULL,
                  agent_version INTEGER NOT NULL, initiated_by_user_id INTEGER NOT NULL,
                  task_id TEXT, assignment_id TEXT,
                  state TEXT NOT NULL, final_content TEXT NOT NULL DEFAULT '', final_content_encrypted BLOB,
                  error_message TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL, started_at TEXT, completed_at TEXT,
                  event_sequence INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS trace_events (
                  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                  event_type TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}', payload_encrypted BLOB,
                  redacted_payload TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
                  UNIQUE(run_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS tools (
                  id TEXT PRIMARY KEY, owner_user_id INTEGER NOT NULL, name TEXT NOT NULL,
                  description TEXT NOT NULL, kind TEXT NOT NULL, encrypted_config BLOB NOT NULL,
                  input_schema TEXT NOT NULL, confirmation_mode TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                  side_effect TEXT NOT NULL DEFAULT 'read', provider_version TEXT NOT NULL DEFAULT 'http-v1',
                  rate_limit_per_run INTEGER NOT NULL DEFAULT 6,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(owner_user_id, name)
                );
                CREATE TABLE IF NOT EXISTS agent_tools (
                  agent_id TEXT NOT NULL, tool_id TEXT NOT NULL, alias TEXT NOT NULL,
                  PRIMARY KEY(agent_id, tool_id)
                );
                CREATE TABLE IF NOT EXISTS run_snapshots (
                  run_id TEXT PRIMARY KEY, encrypted_snapshot BLOB NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tool_confirmations (
                  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, tool_call_id TEXT NOT NULL,
                  tool_name TEXT NOT NULL, arguments TEXT NOT NULL, state TEXT NOT NULL,
                  arguments_hash TEXT NOT NULL DEFAULT '', arguments_encrypted BLOB, checkpoint_encrypted BLOB,
                  rejection_reason TEXT NOT NULL DEFAULT '',
                  decided_by_user_id INTEGER, created_at TEXT NOT NULL, decided_at TEXT,
                  UNIQUE(run_id, tool_call_id)
                );
                CREATE TABLE IF NOT EXISTS memory_items (
                  id TEXT PRIMARY KEY, owner_user_id INTEGER NOT NULL, agent_id TEXT NOT NULL,
                  content_encrypted BLOB NOT NULL, embedding TEXT NOT NULL, expires_at TEXT,
                  source_message_id TEXT, scope TEXT NOT NULL DEFAULT 'agent', kind TEXT NOT NULL DEFAULT 'fact',
                  source_confidence TEXT NOT NULL DEFAULT 'user', importance INTEGER NOT NULL DEFAULT 50,
                  access_count INTEGER NOT NULL DEFAULT 0, conflict_state TEXT NOT NULL DEFAULT 'active',
                  last_accessed_at TEXT, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tool_invocations (
                  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, tool_id TEXT NOT NULL, tool_call_id TEXT NOT NULL,
                  status TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS tool_invocations_run_tool_idx ON tool_invocations (run_id, tool_id, created_at);
                CREATE TABLE IF NOT EXISTS evaluation_cases (
                  id TEXT PRIMARY KEY, agent_id TEXT, version_range TEXT NOT NULL DEFAULT '',
                  input_encrypted BLOB NOT NULL, selected_context_encrypted BLOB,
                  expected_assertions TEXT NOT NULL, tags TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evaluation_runs (
                  id TEXT PRIMARY KEY, harness_version TEXT NOT NULL, agent_version_id TEXT,
                  model_connection_id TEXT, aggregate_metrics TEXT NOT NULL DEFAULT '{}',
                  started_at TEXT NOT NULL, completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS evaluation_results (
                  id TEXT PRIMARY KEY, evaluation_run_id TEXT NOT NULL, case_id TEXT NOT NULL,
                  passed INTEGER NOT NULL, score REAL NOT NULL, latency_ms INTEGER NOT NULL,
                  usage TEXT NOT NULL DEFAULT '{}', tool_trace_ref TEXT,
                  failure_category TEXT, failure_detail TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                  UNIQUE(evaluation_run_id, case_id)
                );
                CREATE TABLE IF NOT EXISTS tasks (
                  id TEXT PRIMARY KEY, root_task_id TEXT NOT NULL, parent_task_id TEXT,
                  owner_user_id INTEGER NOT NULL, proposer_kind TEXT NOT NULL, proposer_id TEXT NOT NULL,
                  title TEXT NOT NULL, state TEXT NOT NULL, goal_encrypted BLOB NOT NULL,
                  result_summary_encrypted BLOB, assigned_agent_id TEXT,
                  conversation_id TEXT, context_scope_id TEXT, budget_snapshot TEXT NOT NULL DEFAULT '{}',
                  state_version INTEGER NOT NULL DEFAULT 1, context_sequence INTEGER NOT NULL DEFAULT 0,
                  dispatch_sequence INTEGER NOT NULL DEFAULT 0,
                  closed_by_kind TEXT, closed_by_id TEXT, closed_at TEXT,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS tasks_owner_state_idx ON tasks (owner_user_id, state, updated_at);
                CREATE INDEX IF NOT EXISTS tasks_root_idx ON tasks (root_task_id, created_at);
                CREATE TABLE IF NOT EXISTS task_context_events (
                  id TEXT PRIMARY KEY, task_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                  kind TEXT NOT NULL, content_encrypted BLOB, redacted_payload TEXT NOT NULL DEFAULT '{}',
                  actor_kind TEXT NOT NULL, actor_id TEXT NOT NULL, created_at TEXT NOT NULL,
                  UNIQUE(task_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS task_dispatch_events (
                  id TEXT PRIMARY KEY, task_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                  event_type TEXT NOT NULL, summary TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}',
                  actor_kind TEXT NOT NULL, actor_id TEXT NOT NULL, created_at TEXT NOT NULL,
                  UNIQUE(task_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS notifications (
                  id TEXT PRIMARY KEY, owner_user_id INTEGER NOT NULL, kind TEXT NOT NULL, task_id TEXT NOT NULL,
                  payload TEXT NOT NULL DEFAULT '{}', read_at TEXT, dedupe_key TEXT NOT NULL,
                  created_at TEXT NOT NULL, UNIQUE(owner_user_id, dedupe_key)
                );
                CREATE INDEX IF NOT EXISTS notifications_owner_idx ON notifications (owner_user_id, read_at, created_at);
                CREATE TABLE IF NOT EXISTS task_assignments (
                  id TEXT PRIMARY KEY, task_id TEXT NOT NULL, executor_kind TEXT NOT NULL,
                  executor_id TEXT NOT NULL, device_id TEXT, workspace_id TEXT,
                  state TEXT NOT NULL, lease_id TEXT, attempt INTEGER NOT NULL,
                  assigned_by_kind TEXT NOT NULL, assigned_by_id TEXT NOT NULL,
                  created_at TEXT NOT NULL, accepted_at TEXT, completed_at TEXT,
                  UNIQUE(task_id, attempt)
                );
                CREATE INDEX IF NOT EXISTS task_assignments_task_state_idx ON task_assignments (task_id, state, created_at);
                CREATE INDEX IF NOT EXISTS task_assignments_executor_idx ON task_assignments (executor_kind, executor_id, state, created_at);
                CREATE TABLE IF NOT EXISTS task_handoffs (
                  id TEXT PRIMARY KEY, from_task_id TEXT NOT NULL, to_task_id TEXT NOT NULL,
                  from_principal_kind TEXT NOT NULL, from_principal_id TEXT NOT NULL,
                  to_executor_kind TEXT NOT NULL, to_executor_id TEXT NOT NULL,
                  input_manifest TEXT NOT NULL DEFAULT '{}', input_encrypted BLOB,
                  schema_version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS task_handoffs_to_task_idx ON task_handoffs (to_task_id, created_at);
                CREATE TABLE IF NOT EXISTS task_results (
                  id TEXT PRIMARY KEY, task_id TEXT NOT NULL, assignment_id TEXT NOT NULL,
                  submitted_by_kind TEXT NOT NULL, submitted_by_id TEXT NOT NULL,
                  result_encrypted BLOB NOT NULL, evidence_manifest TEXT NOT NULL DEFAULT '{}',
                  risk_summary TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS task_results_task_idx ON task_results (task_id, created_at);
                CREATE TABLE IF NOT EXISTS task_command_deduplications (
                  id TEXT PRIMARY KEY, owner_user_id INTEGER NOT NULL, task_scope TEXT NOT NULL,
                  actor_kind TEXT NOT NULL, actor_id TEXT NOT NULL, operation TEXT NOT NULL,
                  idempotency_key TEXT NOT NULL, response_encrypted BLOB NOT NULL,
                  created_at TEXT NOT NULL,
                  UNIQUE(owner_user_id, task_scope, actor_kind, actor_id, operation, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS task_command_deduplications_task_idx ON task_command_deduplications (task_scope, created_at);
                CREATE TABLE IF NOT EXISTS outbox_events (
                  id TEXT PRIMARY KEY, aggregate_type TEXT NOT NULL, aggregate_id TEXT NOT NULL,
                  event_type TEXT NOT NULL, payload TEXT NOT NULL, published_at TEXT, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS outbox_events_pending_idx ON outbox_events (created_at) WHERE published_at IS NULL;
                CREATE TABLE IF NOT EXISTS channel_event_deduplications (
                  provider TEXT NOT NULL, bot_id TEXT NOT NULL, event_id TEXT NOT NULL,
                  conversation_id TEXT NOT NULL, run_id TEXT NOT NULL,
                  owner_user_id INTEGER NOT NULL, created_at TEXT NOT NULL,
                  PRIMARY KEY(provider, bot_id, event_id)
                );
                CREATE TABLE IF NOT EXISTS local_agent_devices (
                  id TEXT PRIMARY KEY, owner_user_id INTEGER NOT NULL, display_name TEXT NOT NULL,
                  platform TEXT NOT NULL DEFAULT '', cli_version TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'offline',
                  capabilities TEXT NOT NULL DEFAULT '[]', last_heartbeat_at TEXT, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS local_workspaces (
                  id TEXT PRIMARY KEY, device_id TEXT NOT NULL, display_name TEXT NOT NULL,
                  policy_version INTEGER NOT NULL DEFAULT 1, capabilities TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS local_agent_models (
                  agent_id TEXT PRIMARY KEY, device_id TEXT NOT NULL, model_base_url TEXT NOT NULL,
                  model_id TEXT NOT NULL, configured_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS local_run_dispatches (
                  run_id TEXT PRIMARY KEY, device_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
                  executor_state TEXT NOT NULL DEFAULT 'pending', lease_id TEXT, lease_expires_at TEXT,
                  local_session_id TEXT, last_acked_sequence INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS pairing_sessions (
                  id TEXT PRIMARY KEY, pairing_secret_hash TEXT NOT NULL, code_hash TEXT NOT NULL,
                  display_name TEXT NOT NULL, platform TEXT NOT NULL DEFAULT '', cli_version TEXT NOT NULL DEFAULT '',
                  capabilities TEXT NOT NULL DEFAULT '[]', state TEXT NOT NULL DEFAULT 'pending',
                  owner_user_id INTEGER, device_id TEXT,
                  expires_at TEXT NOT NULL, consumed_at TEXT, created_at TEXT NOT NULL, approved_at TEXT
                );
                """
            )
            self._ensure_column("agents", "run_policy", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column("agents", "memory_enabled", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column("agents", "memory_retention_days", "INTEGER NOT NULL DEFAULT 30")
            self._ensure_column("agents", "execution_target", "TEXT NOT NULL DEFAULT 'cloud'")
            self._ensure_column("agents", "default_device_id", "TEXT")
            self._ensure_column("agents", "default_workspace_id", "TEXT")
            self._ensure_column("agents", "model_mode", "TEXT NOT NULL DEFAULT 'server_proxy'")
            self._ensure_column("runs", "usage", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column("runs", "attempt", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column("runs", "task_id", "TEXT")
            self._ensure_column("runs", "assignment_id", "TEXT")
            self._ensure_column("runs", "final_content_encrypted", "BLOB")
            self._ensure_column("runs", "context_manifest_encrypted", "BLOB")
            self._ensure_column("runs", "event_sequence", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column("agents", "encrypted_system_prompt", "BLOB")
            self._ensure_column("agent_versions", "snapshot_encrypted", "BLOB")
            self._ensure_column("messages", "content_encrypted", "BLOB")
            self._ensure_column("trace_events", "payload_encrypted", "BLOB")
            self._ensure_column("trace_events", "redacted_payload", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column("tools", "side_effect", "TEXT NOT NULL DEFAULT 'read'")
            self._ensure_column("tools", "provider_version", "TEXT NOT NULL DEFAULT 'http-v1'")
            self._ensure_column("tools", "rate_limit_per_run", "INTEGER NOT NULL DEFAULT 6")
            self._ensure_column("tools", "execution_scope", "TEXT NOT NULL DEFAULT 'server'")
            self._ensure_column("tools", "capability_version", "TEXT NOT NULL DEFAULT '1'")
            self._ensure_column("tools", "workspace_required", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column("local_agent_devices", "credential_hash", "TEXT")
            self._ensure_column("tool_confirmations", "arguments_hash", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("tool_confirmations", "arguments_encrypted", "BLOB")
            self._ensure_column("tool_confirmations", "checkpoint_encrypted", "BLOB")
            self._ensure_column("tool_confirmations", "rejection_reason", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("memory_items", "scope", "TEXT NOT NULL DEFAULT 'agent'")
            self._ensure_column("memory_items", "kind", "TEXT NOT NULL DEFAULT 'fact'")
            self._ensure_column("memory_items", "source_confidence", "TEXT NOT NULL DEFAULT 'user'")
            self._ensure_column("memory_items", "importance", "INTEGER NOT NULL DEFAULT 50")
            self._ensure_column("memory_items", "access_count", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column("memory_items", "conflict_state", "TEXT NOT NULL DEFAULT 'active'")
            self._ensure_column("memory_items", "last_accessed_at", "TEXT")
            self._ensure_column("tasks", "conversation_id", "TEXT")
            self._ensure_column("tasks", "context_scope_id", "TEXT")
            self._ensure_column("tasks", "budget_snapshot", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column("tool_confirmations", "task_id", "TEXT")
            self._encrypt_legacy_plaintext()
            self._ensure_builtin_tools()
            self.db.commit()

    def _ensure_builtin_tools(self) -> None:
        """Install the built-in tool catalog for every owner without assigning it."""
        owners = {row[0] for row in self.db.execute("SELECT DISTINCT owner_user_id FROM agents").fetchall()}
        for owner_id in owners:
            for definition in BUILTIN_TOOLS:
                existing = self.db.execute(
                    "SELECT id FROM tools WHERE owner_user_id = ? AND name = ?", (owner_id, definition["name"])
                ).fetchone()
                if existing:
                    tool_id = existing[0]
                    self.db.execute(
                        "UPDATE tools SET encrypted_config = ?, provider_version = ?, updated_at = ? WHERE id = ?",
                        (self._encrypt_json(definition["config"]), definition["provider_version"], now(), tool_id),
                    )
                else:
                    tool_id = str(uuid.uuid4())
                    timestamp = now()
                    self.db.execute(
                        """INSERT INTO tools (id, owner_user_id, name, description, kind, encrypted_config, input_schema,
                           confirmation_mode, side_effect, provider_version, rate_limit_per_run, enabled, created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 6, 1, ?, ?)""",
                        (tool_id, owner_id, definition["name"], definition["description"], definition["kind"],
                         self._encrypt_json(definition["config"]), json.dumps(definition["input_schema"]),
                         definition["confirmation_mode"], definition["side_effect"], definition["provider_version"], timestamp, timestamp),
                    )

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        columns = {row[1] for row in self.db.execute("PRAGMA table_info({})".format(table)).fetchall()}
        if column not in columns:
            self.db.execute("ALTER TABLE {} ADD COLUMN {} {}".format(table, column, definition))

    def _encrypt_legacy_plaintext(self) -> None:
        """One-way local upgrade: encrypt legacy prototype plaintext in place."""
        for row in self.db.execute("SELECT id, system_prompt FROM agents WHERE encrypted_system_prompt IS NULL").fetchall():
            self.db.execute(
                "UPDATE agents SET encrypted_system_prompt = ?, system_prompt = '' WHERE id = ?",
                (self._encrypt_text(row["system_prompt"]), row["id"]),
            )
        for row in self.db.execute("SELECT id, snapshot FROM agent_versions WHERE snapshot_encrypted IS NULL").fetchall():
            self.db.execute(
                "UPDATE agent_versions SET snapshot_encrypted = ?, snapshot = '{}' WHERE id = ?",
                (self._encrypt_text(row["snapshot"]), row["id"]),
            )
        for row in self.db.execute("SELECT id, content FROM messages WHERE content_encrypted IS NULL").fetchall():
            self.db.execute(
                "UPDATE messages SET content_encrypted = ?, content = '' WHERE id = ?",
                (self._encrypt_text(row["content"]), row["id"]),
            )
        for row in self.db.execute("SELECT id, final_content FROM runs WHERE final_content_encrypted IS NULL").fetchall():
            self.db.execute(
                "UPDATE runs SET final_content_encrypted = ?, final_content = '' WHERE id = ?",
                (self._encrypt_text(row["final_content"]), row["id"]),
            )
        for row in self.db.execute("SELECT id, event_type, payload FROM trace_events WHERE payload_encrypted IS NULL").fetchall():
            payload = json.loads(row["payload"] or "{}")
            self.db.execute(
                "UPDATE trace_events SET payload_encrypted = ?, redacted_payload = ?, payload = '{}' WHERE id = ?",
                (self._encrypt_json(payload), json.dumps(audit_payload(row["event_type"], payload), separators=(",", ":")), row["id"]),
            )

    def create_evaluation_case(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Persist an encrypted baseline case for repeatable local evaluation."""
        case_id = str(data.get("id") or uuid.uuid4())
        assertions = data.get("expected_assertions")
        tags = data.get("tags")
        if not isinstance(assertions, dict) or not isinstance(tags, list) or not str(data.get("input", "")).strip():
            raise ValueError("评估用例缺少输入、断言或标签")
        record = {
            "id": case_id,
            "agent_id": data.get("agent_id"),
            "version_range": str(data.get("version_range", "")),
            "input_encrypted": self._encrypt_json({"value": str(data["input"])}),
            "selected_context_encrypted": self._encrypt_json({"value": str(data.get("selected_context", ""))}),
            "expected_assertions": json.dumps(assertions, separators=(",", ":")),
            "tags": json.dumps([str(tag) for tag in tags], separators=(",", ":")),
            "created_at": now(),
        }
        with self.lock:
            self.db.execute(
                """INSERT INTO evaluation_cases (id, agent_id, version_range, input_encrypted,
                   selected_context_encrypted, expected_assertions, tags, created_at)
                   VALUES (:id, :agent_id, :version_range, :input_encrypted, :selected_context_encrypted,
                   :expected_assertions, :tags, :created_at)""",
                record,
            )
            self.db.commit()
        return self.get_evaluation_case(case_id)  # type: ignore

    def get_evaluation_case(self, case_id: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            row = self.db.execute("SELECT * FROM evaluation_cases WHERE id = ?", (case_id,)).fetchone()
        if row is None:
            return None
        case = dict(row)
        case["input"] = self._decrypt_json(case.pop("input_encrypted"))["value"]
        case["selected_context"] = self._decrypt_json(case.pop("selected_context_encrypted"))["value"]
        case["expected_assertions"] = json.loads(case["expected_assertions"])
        case["tags"] = json.loads(case["tags"])
        return case

    def create_evaluation_run(self, harness_version: str, agent_version_id: Optional[str] = None,
                              model_connection_id: Optional[str] = None) -> Dict[str, Any]:
        record = {
            "id": str(uuid.uuid4()), "harness_version": harness_version,
            "agent_version_id": agent_version_id, "model_connection_id": model_connection_id,
            "aggregate_metrics": "{}", "started_at": now(), "completed_at": None,
        }
        with self.lock:
            self.db.execute(
                """INSERT INTO evaluation_runs (id, harness_version, agent_version_id, model_connection_id,
                   aggregate_metrics, started_at, completed_at)
                   VALUES (:id, :harness_version, :agent_version_id, :model_connection_id,
                   :aggregate_metrics, :started_at, :completed_at)""",
                record,
            )
            self.db.commit()
        return record

    def record_evaluation_result(self, evaluation_run_id: str, case_id: str, passed: bool, score: float,
                                 latency_ms: int, usage: Dict[str, Any], tool_trace_ref: Optional[str] = None,
                                 failure_category: Optional[str] = None, failure_detail: str = "") -> None:
        if failure_category is not None and failure_category not in FAILURE_CATEGORIES:
            raise ValueError("未知评估失败分类")
        if latency_ms < 0 or not 0 <= score <= 1:
            raise ValueError("评估分数或延迟无效")
        with self.lock:
            self.db.execute(
                """INSERT INTO evaluation_results (id, evaluation_run_id, case_id, passed, score, latency_ms,
                   usage, tool_trace_ref, failure_category, failure_detail, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(uuid.uuid4()), evaluation_run_id, case_id, int(passed), score, latency_ms,
                 json.dumps(usage, separators=(",", ":")), tool_trace_ref, failure_category,
                 failure_detail[:2000], now()),
            )
            self.db.commit()

    def evaluation_run(self, evaluation_run_id: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            row = self.db.execute("SELECT * FROM evaluation_runs WHERE id = ?", (evaluation_run_id,)).fetchone()
            if row is None:
                return None
            value = dict(row)
            rows = self.db.execute("SELECT * FROM evaluation_results WHERE evaluation_run_id = ? ORDER BY case_id", (evaluation_run_id,)).fetchall()
        results = [{**dict(item), "passed": bool(item["passed"]), "usage": json.loads(item["usage"] or "{}") } for item in rows]
        total = len(results)
        passed = sum(item["passed"] for item in results)
        value["aggregate_metrics"] = json.loads(value["aggregate_metrics"] or "{}")
        value["results"] = results
        value["summary"] = {"total_cases": total, "passed_cases": passed, "success_rate": passed / total if total else 0.0}
        return value

    def list_evaluation_runs(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self.lock:
            rows = self.db.execute("SELECT id FROM evaluation_runs ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
        return [self.evaluation_run(row["id"]) for row in rows]  # type: ignore

    def compare_evaluation_runs(self, baseline_id: str, candidate_id: str) -> Optional[Dict[str, Any]]:
        baseline, candidate = self.evaluation_run(baseline_id), self.evaluation_run(candidate_id)
        if baseline is None or candidate is None:
            return None
        base = {item["case_id"]: item for item in baseline["results"]}
        current = {item["case_id"]: item for item in candidate["results"]}
        regressions = sorted(case_id for case_id in base.keys() & current.keys() if base[case_id]["passed"] and not current[case_id]["passed"])
        improvements = sorted(case_id for case_id in base.keys() & current.keys() if not base[case_id]["passed"] and current[case_id]["passed"])
        return {"baseline": baseline["summary"], "candidate": candidate["summary"], "regressions": regressions, "improvements": improvements,
                "passed": not regressions and candidate["summary"]["success_rate"] >= baseline["summary"]["success_rate"]}

    def list_runs(self, owner_id: int, agent_id: Optional[str] = None, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        clauses, params = ["initiated_by_user_id = ?"], [owner_id]
        if agent_id:
            clauses.append("agent_id = ?")
            params.append(agent_id)
        if state:
            clauses.append("state = ?")
            params.append(state)
        params.append(limit)
        with self.lock:
            rows = self.db.execute(
                "SELECT id FROM runs WHERE {} ORDER BY created_at DESC LIMIT ?".format(" AND ".join(clauses)), tuple(params)
            ).fetchall()
        return [self.get_run(row["id"], owner_id) for row in rows]  # type: ignore

    def _json_value(self, value: Any) -> Dict[str, Any]:
        if isinstance(value, dict):
            return value
        try:
            return json.loads(value or "{}")
        except (TypeError, ValueError):
            return {}

    @staticmethod
    def _normalize_task_budget(value: Any) -> Dict[str, int]:
        if not isinstance(value, dict) or set(value) - set(TASK_BUDGET_LIMITS):
            raise ValueError("Task 预算字段无效")
        budget: Dict[str, int] = {}
        for key, raw in value.items():
            minimum, maximum = TASK_BUDGET_LIMITS[key]
            if isinstance(raw, bool) or not isinstance(raw, int) or not minimum <= raw <= maximum:
                raise ValueError("Task 预算值无效：{}".format(key))
            budget[key] = raw
        return budget

    def _task_budget_status(self, task_id: str) -> Optional[Dict[str, int]]:
        task = self.db.execute("SELECT budget_snapshot FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if task is None:
            return None
        budget = self._json_value(task["budget_snapshot"])
        usage_rows = self.db.execute("SELECT usage FROM runs WHERE task_id = ?", (task_id,)).fetchall()
        tokens_used = sum(
            int(self._json_value(row["usage"]).get("total_tokens") or 0)
            for row in usage_rows
        )
        tool_calls_used = int(self.db.execute(
            """SELECT COUNT(*) FROM tool_invocations i JOIN runs r ON r.id = i.run_id
               WHERE r.task_id = ?""", (task_id,)
        ).fetchone()[0])
        active_runs = int(self.db.execute(
            """SELECT COUNT(*) FROM runs WHERE task_id = ?
               AND state IN ('queued', 'running', 'waiting_confirmation')""", (task_id,)
        ).fetchone()[0])
        return {
            "max_total_tokens": int(budget.get("max_total_tokens", 0)),
            "max_tool_calls": int(budget.get("max_tool_calls", 0)),
            "max_concurrent_runs": int(budget.get("max_concurrent_runs", 0)),
            "tokens_used": tokens_used,
            "tool_calls_used": tool_calls_used,
            "active_runs": active_runs,
        }

    def _task_budget_error(self, task_id: str, include_next_run: bool = False) -> Optional[str]:
        status = self._task_budget_status(task_id)
        if status is None:
            return "任务未找到"
        if status["max_total_tokens"] and status["tokens_used"] >= status["max_total_tokens"]:
            return "Task token 预算已用尽"
        if status["max_tool_calls"] and status["tool_calls_used"] >= status["max_tool_calls"]:
            return "Task 工具调用预算已用尽"
        if status["max_concurrent_runs"] and status["active_runs"] + int(include_next_run) > status["max_concurrent_runs"]:
            return "Task 并发运行数已达到上限"
        return None

    def _mark_task_budget_exhausted(self, run_id: str) -> None:
        row = self.db.execute(
            """SELECT r.task_id, t.owner_user_id, t.title, t.state, t.state_version FROM runs r
               JOIN tasks t ON t.id = r.task_id WHERE r.id = ? AND r.task_id IS NOT NULL""", (run_id,)
        ).fetchone()
        if row is None or not self._task_budget_error(row["task_id"]):
            return
        if row["state"] in {"closed", "cancelled", "attention_required"}:
            return
        try:
            require_task_transition(row["state"], "attention_required")
        except ValueError:
            return
        state_version = int(row["state_version"]) + 1
        self.db.execute(
            "UPDATE tasks SET state = 'attention_required', state_version = ?, updated_at = ? WHERE id = ?",
            (state_version, now(), row["task_id"]),
        )
        self._append_task_dispatch(
            row["task_id"], "task.blocked", "Task 预算已用尽", "system", "budget",
            {"state_version": state_version},
        )
        self._create_notification(
            int(row["owner_user_id"]), "attention_required", row["task_id"],
            {"title": row["title"], "state": "attention_required", "reason": "budget_exhausted"},
            "{}:budget_exhausted:{}".format(row["task_id"], state_version),
        )

    def _principal(self, kind: str, identifier: Any) -> Dict[str, str]:
        return {"kind": kind, "id": str(identifier)}

    def _task_command_response(self, owner_id: int, task_id: Optional[str], principal: Dict[str, str],
                               operation: str, idempotency_key: Optional[str]) -> Optional[Dict[str, Any]]:
        if not idempotency_key:
            return None
        row = self.db.execute(
            """SELECT response_encrypted FROM task_command_deduplications
               WHERE owner_user_id = ? AND task_scope = ? AND actor_kind = ? AND actor_id = ?
                 AND operation = ? AND idempotency_key = ?""",
            (owner_id, task_id or "", principal["kind"], principal["id"], operation, idempotency_key),
        ).fetchone()
        return self._decrypt_json(row["response_encrypted"]) if row else None

    def _remember_task_command(self, owner_id: int, task_id: Optional[str], principal: Dict[str, str],
                               operation: str, idempotency_key: Optional[str], response: Dict[str, Any]) -> None:
        if not idempotency_key:
            return
        self.db.execute(
            """INSERT INTO task_command_deduplications
               (id, owner_user_id, task_scope, actor_kind, actor_id, operation, idempotency_key, response_encrypted, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(uuid.uuid4()), owner_id, task_id or "", principal["kind"], principal["id"], operation,
             idempotency_key, self._encrypt_json(response), now()),
        )

    def _task_public(self, row: Row) -> Dict[str, Any]:
        task = dict(row)
        task["goal"] = self._decrypt_text(task.pop("goal_encrypted"))
        encrypted_result = task.pop("result_summary_encrypted", None)
        task["result_summary"] = self._decrypt_text(encrypted_result) if encrypted_result else ""
        task["is_closed"] = task["state"] in {"closed", "cancelled"}
        return task

    def _task_for_owner(self, task_id: str, owner_id: int) -> Optional[Row]:
        return self.db.execute(
            "SELECT * FROM tasks WHERE id = ? AND owner_user_id = ?", (task_id, owner_id)
        ).fetchone()

    def _task_for_principal(self, task_id: str, owner_id: int, principal: Dict[str, str]) -> Optional[Row]:
        task = self._task_for_owner(task_id, owner_id)
        if task is None:
            return None
        assignments = self.db.execute(
            "SELECT * FROM task_assignments WHERE task_id = ?", (task_id,)
        ).fetchall()
        return task if TaskAccessPolicy.can_read(task, owner_id, principal, assignments) else None

    def _append_task_context(self, task_id: str, kind: str, content: str, actor_kind: str, actor_id: str,
                             redacted_payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        task = self.db.execute("SELECT context_sequence FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if task is None:
            raise ValueError("任务未找到")
        sequence = int(task["context_sequence"]) + 1
        event = {
            "id": str(uuid.uuid4()), "task_id": task_id, "sequence": sequence, "kind": kind,
            "content_encrypted": self._encrypt_text(content) if content else None,
            "redacted_payload": json.dumps(redacted_payload or {}, separators=(",", ":")),
            "actor_kind": actor_kind, "actor_id": actor_id, "created_at": now(),
        }
        self.db.execute(
            """INSERT INTO task_context_events
               (id, task_id, sequence, kind, content_encrypted, redacted_payload, actor_kind, actor_id, created_at)
               VALUES (:id, :task_id, :sequence, :kind, :content_encrypted, :redacted_payload, :actor_kind, :actor_id, :created_at)""",
            event,
        )
        self.db.execute("UPDATE tasks SET context_sequence = ?, updated_at = ? WHERE id = ?", (sequence, event["created_at"], task_id))
        return event

    def _append_task_dispatch(self, task_id: str, event_type: str, summary: str, actor_kind: str, actor_id: str,
                              metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        task = self.db.execute(
            "SELECT owner_user_id, dispatch_sequence FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if task is None:
            raise ValueError("任务未找到")
        sequence = int(task["dispatch_sequence"]) + 1
        event = {
            "id": str(uuid.uuid4()), "task_id": task_id, "sequence": sequence, "event_type": event_type,
            "summary": redact(summary)[:280], "metadata": json.dumps(metadata or {}, separators=(",", ":")),
            "actor_kind": actor_kind, "actor_id": actor_id, "created_at": now(),
        }
        self.db.execute(
            """INSERT INTO task_dispatch_events
               (id, task_id, sequence, event_type, summary, metadata, actor_kind, actor_id, created_at)
               VALUES (:id, :task_id, :sequence, :event_type, :summary, :metadata, :actor_kind, :actor_id, :created_at)""",
            event,
        )
        self.db.execute(
            """INSERT INTO outbox_events VALUES (?, 'task', ?, 'task.dispatch', ?, NULL, ?)""",
            (str(uuid.uuid4()), task_id, json.dumps({
                "task_id": task_id, "sequence": sequence, "event_type": event_type,
                "owner_user_id": int(task["owner_user_id"]), "summary": event["summary"],
                "metadata": self._json_value(event["metadata"]),
            }, separators=(",", ":")), event["created_at"]),
        )
        self.db.execute("UPDATE tasks SET dispatch_sequence = ?, updated_at = ? WHERE id = ?", (sequence, event["created_at"], task_id))
        realtime_event = {
            "type": event_type, "task_id": task_id, "sequence": sequence, "timestamp": event["created_at"],
            "owner_user_id": int(task["owner_user_id"]),
            "payload": {"summary": event["summary"], "metadata": self._json_value(event["metadata"])},
        }
        try:
            asyncio.get_running_loop().create_task(task_hub.publish(realtime_event))
        except RuntimeError:
            pass
        return event

    @staticmethod
    def _assignment_public(row: Row) -> Dict[str, Any]:
        return dict(row)

    def task_assignments(self, task_id: str, owner_id: int) -> Optional[List[Dict[str, Any]]]:
        with self.lock:
            if self._task_for_owner(task_id, owner_id) is None:
                return None
            rows = self.db.execute(
                "SELECT * FROM task_assignments WHERE task_id = ? ORDER BY attempt ASC", (task_id,)
            ).fetchall()
        return [self._assignment_public(row) for row in rows]

    def _create_task_assignment(self, task_id: str, owner_id: int, executor_kind: str, executor_id: str,
                                assigned_by: Dict[str, str], device_id: Optional[str] = None,
                                workspace_id: Optional[str] = None) -> Dict[str, Any]:
        if executor_kind == "cloud_agent":
            if self.get_agent(executor_id, owner_id) is None:
                raise ValueError("执行 Agent 未找到")
        elif executor_kind != "local_device":
            raise ValueError("不支持的执行目标")
        latest = self.db.execute(
            "SELECT COALESCE(MAX(attempt), 0) FROM task_assignments WHERE task_id = ?", (task_id,)
        ).fetchone()
        attempt = int(latest[0]) + 1
        record = {
            "id": str(uuid.uuid4()), "task_id": task_id, "executor_kind": executor_kind,
            "executor_id": executor_id, "device_id": device_id, "workspace_id": workspace_id,
            "state": "assigned", "lease_id": None, "attempt": attempt,
            "assigned_by_kind": assigned_by["kind"], "assigned_by_id": assigned_by["id"],
            "created_at": now(), "accepted_at": None, "completed_at": None,
        }
        self.db.execute(
            """INSERT INTO task_assignments
               (id, task_id, executor_kind, executor_id, device_id, workspace_id, state, lease_id, attempt,
                assigned_by_kind, assigned_by_id, created_at, accepted_at, completed_at)
               VALUES (:id, :task_id, :executor_kind, :executor_id, :device_id, :workspace_id, :state, :lease_id,
                :attempt, :assigned_by_kind, :assigned_by_id, :created_at, :accepted_at, :completed_at)""",
            record,
        )
        return record

    def _supersede_active_assignments(self, task_id: str) -> None:
        self.db.execute(
            """UPDATE task_assignments SET state = 'superseded', completed_at = ?
               WHERE task_id = ? AND state IN ('assigned', 'accepted')""", (now(), task_id),
        )

    def task_results(self, task_id: str, owner_id: int) -> Optional[List[Dict[str, Any]]]:
        with self.lock:
            if self._task_for_owner(task_id, owner_id) is None:
                return None
            rows = self.db.execute(
                "SELECT * FROM task_results WHERE task_id = ? ORDER BY created_at ASC", (task_id,)
            ).fetchall()
        results = []
        for row in rows:
            result = dict(row)
            result["result"] = self._decrypt_text(result.pop("result_encrypted"))
            result["evidence_manifest"] = self._json_value(result["evidence_manifest"])
            results.append(result)
        return results

    def task_runs(self, task_id: str, owner_id: int, limit: int = 100) -> Optional[List[Dict[str, Any]]]:
        with self.lock:
            if self._task_for_owner(task_id, owner_id) is None:
                return None
            rows = self.db.execute(
                """SELECT id FROM runs WHERE task_id = ? AND initiated_by_user_id = ?
                   ORDER BY created_at DESC LIMIT ?""", (task_id, owner_id, limit),
            ).fetchall()
        return [self.get_run(row["id"], owner_id) for row in rows]  # type: ignore

    def task_confirmations(self, task_id: str, owner_id: int) -> Optional[List[Dict[str, Any]]]:
        with self.lock:
            if self._task_for_owner(task_id, owner_id) is None:
                return None
            rows = self.db.execute(
                "SELECT id, run_id FROM tool_confirmations WHERE task_id = ? ORDER BY created_at ASC", (task_id,)
            ).fetchall()
        return [self.get_confirmation(row["id"], row["run_id"]) for row in rows]  # type: ignore

    def submit_task_result(self, task_id: str, owner_id: int, assignment_id: str,
                           principal: Dict[str, str], result: str,
                           evidence_manifest: Optional[Dict[str, Any]] = None,
                           risk_summary: str = "") -> Optional[Dict[str, Any]]:
        with self.lock:
            existing_task = self._task_for_owner(task_id, owner_id)
            if existing_task is None:
                return None
            task = self._task_for_principal(task_id, owner_id, principal)
            if task is None:
                raise PermissionError("当前主体无权访问任务")
            assignment = self.db.execute(
                """SELECT * FROM task_assignments WHERE id = ? AND task_id = ? AND executor_kind = ?
                   AND executor_id = ? AND state IN ('assigned', 'accepted')""",
                (assignment_id, task_id, principal["kind"], principal["id"]),
            ).fetchone()
            if assignment is None:
                raise PermissionError("当前主体不是有效任务执行者")
            require_task_transition(task["state"], "awaiting_proposer_close")
            timestamp = now()
            self.db.execute(
                """INSERT INTO task_results
                   (id, task_id, assignment_id, submitted_by_kind, submitted_by_id, result_encrypted,
                    evidence_manifest, risk_summary, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(uuid.uuid4()), task_id, assignment_id, principal["kind"], principal["id"],
                 self._encrypt_text(result), json.dumps(evidence_manifest or {}, separators=(",", ":")),
                 redact(risk_summary)[:2000], timestamp),
            )
            state_version = int(task["state_version"]) + 1
            self.db.execute(
                """UPDATE tasks SET state = 'awaiting_proposer_close', state_version = ?,
                   result_summary_encrypted = ?, updated_at = ? WHERE id = ?""",
                (state_version, self._encrypt_text(result), timestamp, task_id),
            )
            self.db.execute(
                "UPDATE task_assignments SET state = 'completed', completed_at = ? WHERE id = ?", (timestamp, assignment_id)
            )
            self._append_task_context(task_id, "task.result", result, principal["kind"], principal["id"], {"assignment_id": assignment_id})
            self._append_task_dispatch(
                task_id, "task.awaiting_proposer_close", "执行结果等待提出者收尾", principal["kind"], principal["id"],
                {"assignment_id": assignment_id, "state_version": state_version},
            )
            self._create_notification(
                int(task["owner_user_id"]), "awaiting_proposer_close", task_id,
                {"title": task["title"], "state": "awaiting_proposer_close"},
                "{}:awaiting_proposer_close:{}".format(task_id, state_version),
            )
            self.db.commit()
            updated = self._task_for_owner(task_id, owner_id)
        return self._task_public(updated) if updated else None

    def record_task_handoff(self, owner_id: int, from_task_id: str, to_task_id: str,
                            principal: Dict[str, str], to_executor_kind: str, to_executor_id: str,
                            input_manifest: Dict[str, Any], input_content: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            source = self._task_for_principal(from_task_id, owner_id, principal)
            destination = self._task_for_owner(to_task_id, owner_id)
            if source is None or destination is None:
                return None
            record = {
                "id": str(uuid.uuid4()), "from_task_id": from_task_id, "to_task_id": to_task_id,
                "from_principal_kind": principal["kind"], "from_principal_id": principal["id"],
                "to_executor_kind": to_executor_kind, "to_executor_id": to_executor_id,
                "input_manifest": json.dumps(input_manifest, separators=(",", ":")),
                "input_encrypted": self._encrypt_text(input_content), "schema_version": 1, "created_at": now(),
            }
            self.db.execute(
                """INSERT INTO task_handoffs
                   (id, from_task_id, to_task_id, from_principal_kind, from_principal_id,
                    to_executor_kind, to_executor_id, input_manifest, input_encrypted, schema_version, created_at)
                   VALUES (:id, :from_task_id, :to_task_id, :from_principal_kind, :from_principal_id,
                    :to_executor_kind, :to_executor_id, :input_manifest, :input_encrypted, :schema_version, :created_at)""",
                record,
            )
            self._append_task_context(to_task_id, "task.handoff", input_content, principal["kind"], principal["id"], input_manifest)
            self.db.commit()
        return {key: value for key, value in record.items() if key != "input_encrypted"}

    def delegate_task(self, parent_task_id: str, owner_id: int, principal: Dict[str, str], target_agent_id: str,
                      title: str, goal: str, input_package: str, budget_request: Dict[str, Any], reason: str) -> Dict[str, Any]:
        child_budget = self._normalize_task_budget(budget_request)
        if not all(isinstance(value, str) and value.strip() for value in (target_agent_id, title, goal, input_package, reason)):
            raise ValueError("委派参数不能为空")
        with self.lock:
            parent = self._task_for_principal(parent_task_id, owner_id, principal)
            if parent is None:
                raise PermissionError("当前主体无权委派此任务")
            if parent["state"] not in {"in_progress", "attention_required"}:
                raise ValueError("当前任务状态不允许委派")
            active_assignment = self.db.execute(
                """SELECT id FROM task_assignments WHERE task_id = ? AND executor_kind = ? AND executor_id = ?
                   AND state = 'accepted' ORDER BY attempt DESC LIMIT 1""",
                (parent_task_id, principal["kind"], principal["id"]),
            ).fetchone()
            if active_assignment is None:
                raise PermissionError("只有当前执行者可以委派")
            if self.get_agent(target_agent_id, owner_id) is None:
                raise ValueError("目标 Agent 未找到")

            depth, path, cursor = 0, [parent_task_id], parent
            while cursor["parent_task_id"]:
                parent_id = cursor["parent_task_id"]
                if parent_id in path:
                    raise ValueError("检测到任务委派循环")
                path.append(parent_id)
                cursor = self.db.execute("SELECT * FROM tasks WHERE id = ?", (parent_id,)).fetchone()
                if cursor is None:
                    raise ValueError("父任务链无效")
                depth += 1
            root_task_id = cursor["id"]
            parent_budget = self._json_value(parent["budget_snapshot"])
            if depth + 1 > int(parent_budget.get("max_depth", 3)):
                raise ValueError("任务委派深度已达到上限")
            child_count = int(self.db.execute(
                "SELECT COUNT(*) FROM tasks WHERE root_task_id = ? AND id != ?", (root_task_id, root_task_id)
            ).fetchone()[0])
            if child_count >= int(parent_budget.get("max_subtasks", 4)):
                raise ValueError("任务子任务数已达到上限")
            max_parallel = int(parent_budget.get("max_concurrent_runs", 0))
            if max_parallel:
                active_runs = int(self.db.execute(
                    """SELECT COUNT(*) FROM runs r JOIN tasks t ON t.id = r.task_id WHERE t.root_task_id = ?
                       AND r.state IN ('queued', 'running', 'waiting_confirmation')""", (root_task_id,)
                ).fetchone()[0])
                if active_runs >= max_parallel:
                    raise ValueError("任务树并发运行数已达到上限")
            parent_tokens = int(parent_budget.get("max_total_tokens", 0))
            requested_tokens = int(child_budget.get("max_total_tokens", 0))
            if not parent_tokens or not requested_tokens:
                raise ValueError("委派需要父任务和子任务都声明 max_total_tokens 预算")
            parent_status = self._task_budget_status(parent_task_id)
            allocated_tokens = sum(
                int(self._json_value(row["budget_snapshot"]).get("max_total_tokens", 0))
                for row in self.db.execute("SELECT budget_snapshot FROM tasks WHERE parent_task_id = ?", (parent_task_id,)).fetchall()
            )
            remaining_tokens = parent_tokens - (parent_status["tokens_used"] if parent_status else 0) - allocated_tokens
            if requested_tokens > remaining_tokens:
                raise ValueError("子任务请求预算超过父任务可分配额度")

            timestamp, task_id = now(), str(uuid.uuid4())
            record = {
                "id": task_id, "root_task_id": root_task_id, "parent_task_id": parent_task_id, "owner_user_id": owner_id,
                "proposer_kind": "agent", "proposer_id": principal["id"], "title": title.strip(), "state": "assigned",
                "goal_encrypted": self._encrypt_text(goal.strip()), "result_summary_encrypted": None,
                "assigned_agent_id": target_agent_id, "conversation_id": None, "context_scope_id": str(uuid.uuid4()),
                "budget_snapshot": json.dumps(child_budget, separators=(",", ":")), "state_version": 1,
                "context_sequence": 0, "dispatch_sequence": 0, "closed_by_kind": None, "closed_by_id": None,
                "closed_at": None, "created_at": timestamp, "updated_at": timestamp,
            }
            self.db.execute(
                """INSERT INTO tasks (id, root_task_id, parent_task_id, owner_user_id, proposer_kind, proposer_id,
                   title, state, goal_encrypted, result_summary_encrypted, assigned_agent_id, state_version,
                   conversation_id, context_scope_id, budget_snapshot, context_sequence, dispatch_sequence,
                   closed_by_kind, closed_by_id, closed_at, created_at, updated_at)
                   VALUES (:id, :root_task_id, :parent_task_id, :owner_user_id, :proposer_kind, :proposer_id,
                   :title, :state, :goal_encrypted, :result_summary_encrypted, :assigned_agent_id, :state_version,
                   :conversation_id, :context_scope_id, :budget_snapshot, :context_sequence, :dispatch_sequence,
                   :closed_by_kind, :closed_by_id, :closed_at, :created_at, :updated_at)""",
                record,
            )
            self._append_task_context(task_id, "task.goal", goal.strip(), "agent", principal["id"], {"title": record["title"]})
            manifest = {"reason": reason.strip(), "source_task_id": parent_task_id, "path": list(reversed(path))}
            handoff = {
                "id": str(uuid.uuid4()), "from_task_id": parent_task_id, "to_task_id": task_id,
                "from_principal_kind": principal["kind"], "from_principal_id": principal["id"],
                "to_executor_kind": "cloud_agent", "to_executor_id": target_agent_id,
                "input_manifest": json.dumps(manifest, separators=(",", ":")), "input_encrypted": self._encrypt_text(input_package.strip()),
                "schema_version": 1, "created_at": timestamp,
            }
            self.db.execute(
                """INSERT INTO task_handoffs (id, from_task_id, to_task_id, from_principal_kind, from_principal_id,
                   to_executor_kind, to_executor_id, input_manifest, input_encrypted, schema_version, created_at)
                   VALUES (:id, :from_task_id, :to_task_id, :from_principal_kind, :from_principal_id,
                   :to_executor_kind, :to_executor_id, :input_manifest, :input_encrypted, :schema_version, :created_at)""",
                handoff,
            )
            self._append_task_context(task_id, "task.handoff", input_package.strip(), principal["kind"], principal["id"], manifest)
            self._append_task_dispatch(task_id, "task.created", "子任务已创建", "agent", principal["id"], {"parent_task_id": parent_task_id})
            assignment = self._create_task_assignment(task_id, owner_id, "cloud_agent", target_agent_id, {"kind": "agent", "id": principal["id"]})
            self._append_task_dispatch(task_id, "task.assigned", "子任务已分配给 Agent", "agent", principal["id"], {"assignment_id": assignment["id"]})
            self._append_task_dispatch(parent_task_id, "task.assigned", "已创建并委派子任务", "agent", principal["id"], {"child_task_id": task_id})
            conversation_id = str(uuid.uuid4())
            self.db.execute(
                "INSERT INTO conversations VALUES (?, ?, ?, ?, 0, NULL, ?, ?)",
                (conversation_id, target_agent_id, owner_id, record["title"], timestamp, timestamp),
            )
            self.db.execute("UPDATE tasks SET conversation_id = ? WHERE id = ?", (conversation_id, task_id))
            self.db.commit()
        run = self.create_run(conversation_id, owner_id, goal.strip(), task_id, assignment["id"])
        task = self.get_task(task_id, owner_id)
        if task is None:
            raise RuntimeError("子任务创建后无法读取")
        task["assignment_id"] = assignment["id"]
        if run and not run.get("error"):
            task["run_id"] = run["id"]
        return task

    def collect_child_result(self, parent_task_id: str, child_task_id: str, owner_id: int,
                             principal: Dict[str, str]) -> Dict[str, Any]:
        with self.lock:
            parent = self._task_for_principal(parent_task_id, owner_id, principal)
            child = self._task_for_principal(child_task_id, owner_id, principal)
            if parent is None or child is None or child["parent_task_id"] != parent_task_id:
                raise PermissionError("只能读取当前任务的直系子任务结果")
            self._require_task_proposer(child, principal)
            if child["state"] not in {"awaiting_proposer_close", "closed"}:
                raise ValueError("子任务尚未提交可收取的结果")
            row = self.db.execute(
                "SELECT * FROM task_results WHERE task_id = ? ORDER BY created_at DESC LIMIT 1", (child_task_id,)
            ).fetchone()
            if row is None:
                raise ValueError("子任务尚未提交结构化结果")
            result = self._decrypt_text(row["result_encrypted"])
            payload = {
                "child_task_id": child_task_id, "result": result,
                "evidence_manifest": self._json_value(row["evidence_manifest"]), "risk_summary": row["risk_summary"],
            }
            self._append_task_context(parent_task_id, "task.child_result", result, "agent", principal["id"], {
                "child_task_id": child_task_id, "evidence_manifest": payload["evidence_manifest"], "risk_summary": payload["risk_summary"],
            })
            self.db.commit()
        return payload

    def close_delegated_task(self, parent_task_id: str, child_task_id: str, owner_id: int,
                             principal: Dict[str, str], result_summary: str) -> Dict[str, Any]:
        with self.lock:
            parent = self._task_for_principal(parent_task_id, owner_id, principal)
            child = self._task_for_principal(child_task_id, owner_id, principal)
            if parent is None or child is None or child["parent_task_id"] != parent_task_id:
                raise PermissionError("只能关闭当前任务的直系子任务")
            self._require_task_proposer(child, principal)
            require_task_transition(child["state"], "closed")
            timestamp, state_version = now(), int(child["state_version"]) + 1
            self.db.execute(
                """UPDATE tasks SET state = 'closed', state_version = ?, result_summary_encrypted = ?,
                   closed_by_kind = 'agent', closed_by_id = ?, closed_at = ?, updated_at = ? WHERE id = ?""",
                (state_version, self._encrypt_text(result_summary.strip()), principal["id"], timestamp, timestamp, child_task_id),
            )
            self._append_task_context(child_task_id, "task.result_accepted", result_summary.strip(), "agent", principal["id"])
            self._append_task_dispatch(child_task_id, "task.closed", "子任务已由提出 Agent 收尾", "agent", principal["id"], {"state_version": state_version})
            self.db.commit()
            updated = self._task_for_owner(child_task_id, owner_id)
        return self._task_public(updated)  # type: ignore[arg-type]

    def create_task(self, owner_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        assigned_agent_id = data.get("assigned_agent_id") or None
        if assigned_agent_id and self.get_agent(assigned_agent_id, owner_id) is None:
            raise ValueError("执行 Agent 未找到")
        principal = self._principal("user", owner_id)
        idempotency_key = data.get("idempotency_key")
        task_id, timestamp = str(uuid.uuid4()), now()
        state = "assigned" if assigned_agent_id else "queued"
        budget_snapshot = self._normalize_task_budget(data.get("budget_snapshot") or {})
        record = {
            "id": task_id, "root_task_id": task_id, "parent_task_id": None, "owner_user_id": owner_id,
            "proposer_kind": "user", "proposer_id": str(owner_id), "title": data["title"].strip(), "state": state,
            "goal_encrypted": self._encrypt_text(data["goal"].strip()), "result_summary_encrypted": None,
            "assigned_agent_id": assigned_agent_id, "conversation_id": None, "context_scope_id": str(uuid.uuid4()),
            "budget_snapshot": json.dumps(budget_snapshot, separators=(",", ":")),
            "state_version": 1, "context_sequence": 0, "dispatch_sequence": 0,
            "closed_by_kind": None, "closed_by_id": None, "closed_at": None, "created_at": timestamp, "updated_at": timestamp,
        }
        conversation_id = None
        assignment = None
        with self.lock:
            existing = self._task_command_response(owner_id, None, principal, "task.create", idempotency_key)
            if existing is not None:
                return existing
            self.db.execute(
                """INSERT INTO tasks (id, root_task_id, parent_task_id, owner_user_id, proposer_kind, proposer_id,
                   title, state, goal_encrypted, result_summary_encrypted, assigned_agent_id, state_version,
                   conversation_id, context_scope_id, budget_snapshot, context_sequence, dispatch_sequence,
                   closed_by_kind, closed_by_id, closed_at, created_at, updated_at)
                   VALUES (:id, :root_task_id, :parent_task_id, :owner_user_id, :proposer_kind, :proposer_id,
                   :title, :state, :goal_encrypted, :result_summary_encrypted, :assigned_agent_id, :state_version,
                   :conversation_id, :context_scope_id, :budget_snapshot, :context_sequence, :dispatch_sequence,
                   :closed_by_kind, :closed_by_id, :closed_at, :created_at, :updated_at)""",
                record,
            )
            self._append_task_context(task_id, "task.goal", data["goal"].strip(), "user", str(owner_id), {"title": record["title"]})
            self._append_task_dispatch(task_id, "task.created", "任务已创建", "user", str(owner_id), {"state": state})
            if assigned_agent_id:
                assignment = self._create_task_assignment(task_id, owner_id, "cloud_agent", assigned_agent_id, principal)
                self._append_task_dispatch(
                    task_id, "task.assigned", "任务已分配给 Agent", "user", str(owner_id),
                    {"agent_id": assigned_agent_id, "assignment_id": assignment["id"]},
                )
                conversation_id = str(uuid.uuid4())
                self.db.execute(
                    """INSERT INTO conversations VALUES (?, ?, ?, ?, 0, NULL, ?, ?)""",
                    (conversation_id, assigned_agent_id, owner_id, record["title"], timestamp, timestamp),
                )
                self.db.execute("UPDATE tasks SET conversation_id = ? WHERE id = ?", (conversation_id, task_id))
            self.db.commit()
        run = self.create_run(
            conversation_id, owner_id, data["goal"].strip(), task_id=task_id,
            assignment_id=assignment["id"] if assignment else None,
        ) if conversation_id else None
        task = self.get_task(task_id, owner_id)
        if task is None:
            raise RuntimeError("任务创建后无法读取")
        if assignment:
            task["assignment_id"] = assignment["id"]
        if run and not run.get("error"):
            task["run_id"] = run["id"]
        with self.lock:
            self._remember_task_command(owner_id, None, principal, "task.create", idempotency_key, task)
            self.db.commit()
        return task

    def assign_cloud_task(self, task_id: str, owner_id: int, agent_id: str,
                          idempotency_key: Optional[str] = None,
                          expected_state_version: Optional[int] = None) -> Optional[Dict[str, Any]]:
        principal = self._principal("user", owner_id)
        with self.lock:
            task = self._task_for_owner(task_id, owner_id)
            if task is None:
                return None
            self._require_task_proposer(task, principal)
            existing = self._task_command_response(owner_id, task_id, principal, "task.assign", idempotency_key)
            if existing is not None:
                return existing
            if expected_state_version is not None and int(task["state_version"]) != expected_state_version:
                raise ValueError("任务状态已更新，请刷新后重试")
            require_task_transition(task["state"], "assigned")
            self._supersede_active_assignments(task_id)
            assignment = self._create_task_assignment(task_id, owner_id, "cloud_agent", agent_id, principal)
            timestamp = now()
            conversation_id = str(uuid.uuid4())
            self.db.execute(
                "INSERT INTO conversations VALUES (?, ?, ?, ?, 0, NULL, ?, ?)",
                (conversation_id, agent_id, owner_id, task["title"], timestamp, timestamp),
            )
            state_version = int(task["state_version"]) + (0 if task["state"] == "assigned" else 1)
            self.db.execute(
                """UPDATE tasks SET state = 'assigned', assigned_agent_id = ?, conversation_id = ?,
                   state_version = ?, updated_at = ? WHERE id = ?""",
                (agent_id, conversation_id, state_version, timestamp, task_id),
            )
            self._append_task_dispatch(
                task_id, "task.assigned", "任务已分配给 Agent", "user", str(owner_id),
                {"agent_id": agent_id, "assignment_id": assignment["id"], "state_version": state_version},
            )
            self.db.commit()
        run = self.create_run(conversation_id, owner_id, self._decrypt_text(task["goal_encrypted"]), task_id, assignment["id"])
        result = self.get_task(task_id, owner_id)
        if result is None:
            raise RuntimeError("任务指派后无法读取")
        result["assignment_id"] = assignment["id"]
        if run and not run.get("error"):
            result["run_id"] = run["id"]
        with self.lock:
            self._remember_task_command(owner_id, task_id, principal, "task.assign", idempotency_key, result)
            self.db.commit()
        return result

    def get_task(self, task_id: str, owner_id: int) -> Optional[Dict[str, Any]]:
        with self.lock:
            row = self._task_for_owner(task_id, owner_id)
            if row is None:
                return None
            task = self._task_public(row)
            children = self.db.execute("SELECT COUNT(*) FROM tasks WHERE parent_task_id = ?", (task_id,)).fetchone()
            task["child_count"] = int(children[0]) if children else 0
            assignment = self.db.execute(
                "SELECT * FROM task_assignments WHERE task_id = ? ORDER BY attempt DESC LIMIT 1", (task_id,)
            ).fetchone()
            task["current_assignment"] = self._assignment_public(assignment) if assignment else None
            if assignment is not None:
                run = self.db.execute(
                    """SELECT id FROM runs WHERE task_id = ? AND assignment_id = ?
                       ORDER BY created_at DESC LIMIT 1""", (task_id, assignment["id"]),
                ).fetchone()
                if run is not None:
                    task["run_id"] = run["id"]
            task["unread_count"] = self._task_unread_count(task_id, owner_id)
        return task

    def _task_unread_count(self, task_id: str, owner_id: int) -> int:
        row = self.db.execute(
            "SELECT COUNT(*) FROM notifications WHERE owner_user_id = ? AND task_id = ? AND read_at IS NULL",
            (owner_id, task_id),
        ).fetchone()
        return int(row[0]) if row else 0

    def _task_list_projection(self, row: Row, owner_id: int) -> Dict[str, Any]:
        """Return sidebar metadata only; task work content belongs to the detail APIs."""
        task = dict(row)
        task.pop("goal_encrypted", None)
        task.pop("result_summary_encrypted", None)
        task["is_closed"] = task["state"] in {"closed", "cancelled"}
        children = self.db.execute(
            "SELECT COUNT(*) FROM tasks WHERE parent_task_id = ?", (task["id"],)
        ).fetchone()
        assignment = self.db.execute(
            "SELECT * FROM task_assignments WHERE task_id = ? ORDER BY attempt DESC LIMIT 1", (task["id"],)
        ).fetchone()
        dispatch = self.db.execute(
            """SELECT event_type, summary, created_at FROM task_dispatch_events
               WHERE task_id = ? ORDER BY sequence DESC LIMIT 1""", (task["id"],)
        ).fetchone()
        task["child_count"] = int(children[0]) if children else 0
        task["current_assignment"] = self._assignment_public(assignment) if assignment else None
        task["last_dispatch_event"] = dict(dispatch) if dispatch else None
        task["unread_count"] = self._task_unread_count(task["id"], owner_id)
        return task

    def list_tasks(self, owner_id: int, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        clauses, params = ["owner_user_id = ?"], [owner_id]
        if state:
            clauses.append("state = ?")
            params.append(state)
        params.append(limit)
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM tasks WHERE {} ORDER BY updated_at DESC LIMIT ?".format(" AND ".join(clauses)), tuple(params)
            ).fetchall()
            return [self._task_list_projection(row, owner_id) for row in rows]

    def task_context_events(self, task_id: str, owner_id: int, after_sequence: int = 0, limit: int = 200) -> Optional[List[Dict[str, Any]]]:
        with self.lock:
            if self._task_for_owner(task_id, owner_id) is None:
                return None
            rows = self.db.execute(
                """SELECT * FROM task_context_events WHERE task_id = ? AND sequence > ?
                   ORDER BY sequence ASC LIMIT ?""", (task_id, after_sequence, limit)
            ).fetchall()
        events = []
        for row in rows:
            event = dict(row)
            encrypted = event.pop("content_encrypted", None)
            event["content"] = self._decrypt_text(encrypted) if encrypted else ""
            event["redacted_payload"] = self._json_value(event["redacted_payload"])
            events.append(event)
        return events

    def task_dispatch_events(self, owner_id: int, task_id: Optional[str] = None, after_sequence: int = 0, limit: int = 200) -> List[Dict[str, Any]]:
        clauses, params = ["t.owner_user_id = ?", "e.sequence > ?"], [owner_id, after_sequence]
        if task_id:
            clauses.append("e.task_id = ?")
            params.append(task_id)
        params.append(limit)
        with self.lock:
            rows = self.db.execute(
                """SELECT e.* FROM task_dispatch_events e JOIN tasks t ON t.id = e.task_id
                   WHERE {} ORDER BY e.created_at ASC, e.sequence ASC LIMIT ?""".format(" AND ".join(clauses)), tuple(params)
            ).fetchall()
        result = []
        for row in rows:
            event = dict(row)
            event["metadata"] = self._json_value(event["metadata"])
            result.append(event)
        return result

    def _require_task_proposer(self, task: Row, principal: Dict[str, str]) -> None:
        compatible_agent_principal = task["proposer_kind"] == "agent" and principal["kind"] == "cloud_agent"
        if (task["proposer_kind"] != principal["kind"] and not compatible_agent_principal) or task["proposer_id"] != principal["id"]:
            raise PermissionError("只有任务提出者可以执行此操作")

    def transition_task(self, task_id: str, owner_id: int, target: str, summary: str,
                        result_summary: Optional[str] = None, idempotency_key: Optional[str] = None,
                        expected_state_version: Optional[int] = None) -> Optional[Dict[str, Any]]:
        principal = self._principal("user", owner_id)
        with self.lock:
            task = self._task_for_owner(task_id, owner_id)
            if task is None:
                return None
            self._require_task_proposer(task, principal)
            existing = self._task_command_response(owner_id, task_id, principal, "task." + target, idempotency_key)
            if existing is not None:
                return existing
            if expected_state_version is not None and int(task["state_version"]) != expected_state_version:
                raise ValueError("任务状态已更新，请刷新后重试")
            require_task_transition(task["state"], target)
            timestamp = now()
            state_version = int(task["state_version"]) + (0 if task["state"] == target else 1)
            closed = target in {"closed", "cancelled"}
            self.db.execute(
                """UPDATE tasks SET state = ?, state_version = ?, result_summary_encrypted = COALESCE(?, result_summary_encrypted),
                   closed_by_kind = ?, closed_by_id = ?, closed_at = ?, updated_at = ? WHERE id = ?""",
                (target, state_version, self._encrypt_text(result_summary) if result_summary is not None else None,
                 "user" if closed else None, str(owner_id) if closed else None, timestamp if closed else None, timestamp, task_id),
            )
            event_type = "task.{}".format(target)
            self._append_task_dispatch(task_id, event_type, summary, "user", str(owner_id), {"state_version": state_version})
            if result_summary is not None:
                self._append_task_context(task_id, "task.result", result_summary, "user", str(owner_id))
            if target in {"awaiting_proposer_close", "attention_required"}:
                self._create_notification(owner_id, target, task_id, {"title": task["title"], "state": target}, "{}:{}:{}".format(task_id, target, state_version))
            row = self._task_for_owner(task_id, owner_id)
            result = self._task_public(row)  # type: ignore[arg-type]
            self._remember_task_command(owner_id, task_id, principal, "task." + target, idempotency_key, result)
            self.db.commit()
        return result

    def _create_notification(self, owner_id: int, kind: str, task_id: str, payload: Dict[str, Any], dedupe_key: str) -> None:
        self.db.execute(
            """INSERT INTO notifications (id, owner_user_id, kind, task_id, payload, read_at, dedupe_key, created_at)
               VALUES (?, ?, ?, ?, ?, NULL, ?, ?) ON CONFLICT(owner_user_id, dedupe_key) DO NOTHING""",
            (str(uuid.uuid4()), owner_id, kind, task_id, json.dumps(payload, separators=(",", ":")), dedupe_key, now()),
        )

    def list_notifications(self, owner_id: int, unread_only: bool = False, limit: int = 100) -> List[Dict[str, Any]]:
        clause = "owner_user_id = ?" + (" AND read_at IS NULL" if unread_only else "")
        with self.lock:
            rows = self.db.execute("SELECT * FROM notifications WHERE {} ORDER BY created_at DESC LIMIT ?".format(clause), (owner_id, limit)).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["payload"] = self._json_value(item["payload"])
            items.append(item)
        return items

    def mark_notification_read(self, notification_id: str, owner_id: int) -> bool:
        with self.lock:
            result = self.db.execute("UPDATE notifications SET read_at = COALESCE(read_at, ?) WHERE id = ? AND owner_user_id = ?", (now(), notification_id, owner_id))
            self.db.commit()
        return result.rowcount == 1

    def sync_task_run_state(self, run_id: str, run_state: str, content: str = "", error: str = "") -> Optional[Dict[str, Any]]:
        """Project a Run lifecycle change into its Task without ever auto-closing it."""
        with self.lock:
            row = self.db.execute(
                """SELECT r.task_id, r.assignment_id, r.agent_id, t.* FROM runs r JOIN tasks t ON t.id = r.task_id
                   WHERE r.id = ? AND r.task_id IS NOT NULL""", (run_id,)
            ).fetchone()
            if row is None:
                return None
            task_id = row["task_id"]
            current = row["state"]
            if run_state == "running":
                target = "in_progress"
                summary = "执行 Agent 已开始处理任务"
            elif run_state == "waiting_confirmation":
                target = "waiting_confirmation"
                summary = "任务正在等待工具确认"
            elif run_state == "completed":
                if current in {"closed", "cancelled", "awaiting_proposer_close"}:
                    return self._task_public(row)
                if content:
                    self._append_task_context(
                        task_id, "run.completed_candidate", content, "cloud_agent", str(row["agent_id"]),
                        {"run_id": run_id, "assignment_id": row["assignment_id"]},
                    )
                    self.db.commit()
                return self._task_public(self._task_for_owner(task_id, int(row["owner_user_id"])))  # type: ignore[arg-type]
            elif run_state in {"failed", "cancelled"}:
                target = "attention_required"
                summary = "执行需要提出者处理"
            else:
                return None
            if current in {"closed", "cancelled"} or current == target:
                return self._task_public(row)
            try:
                require_task_transition(current, target)
            except ValueError:
                return self._task_public(row)
            timestamp = now()
            state_version = int(row["state_version"]) + 1
            result = content if run_state == "completed" else None
            self.db.execute(
                """UPDATE tasks SET state = ?, state_version = ?, result_summary_encrypted = COALESCE(?, result_summary_encrypted),
                   updated_at = ? WHERE id = ?""",
                (target, state_version, self._encrypt_text(result) if result is not None else None, timestamp, task_id),
            )
            if row["assignment_id"]:
                if run_state == "running":
                    self.db.execute(
                        """UPDATE task_assignments SET state = 'accepted', accepted_at = COALESCE(accepted_at, ?)
                           WHERE id = ? AND state = 'assigned'""", (timestamp, row["assignment_id"]),
                    )
                elif run_state == "failed":
                    self.db.execute(
                        "UPDATE task_assignments SET state = 'expired', completed_at = ? WHERE id = ? AND state IN ('assigned', 'accepted')",
                        (timestamp, row["assignment_id"]),
                    )
                elif run_state == "cancelled":
                    self.db.execute(
                        "UPDATE task_assignments SET state = 'cancelled', completed_at = ? WHERE id = ? AND state IN ('assigned', 'accepted')",
                        (timestamp, row["assignment_id"]),
                    )
            self._append_task_dispatch(task_id, "task.{}".format(target), summary, "agent", str(row["agent_id"]), {"run_id": run_id, "state_version": state_version})
            if run_state in {"failed", "cancelled"}:
                kind = "attention_required"
                self._create_notification(int(row["owner_user_id"]), kind, task_id, {"title": row["title"], "state": target, "run_id": run_id, "error": redact(error)[:280]}, "{}:{}:{}".format(task_id, target, state_version))
            self.db.commit()
            task = self._task_for_owner(task_id, int(row["owner_user_id"]))
        return self._task_public(task) if task is not None else None

    def task_run_messages(self, run_id: str) -> Optional[List[Dict[str, str]]]:
        snapshot = self.run_snapshot(run_id)
        if snapshot is None or "task_state" not in snapshot:
            return None
        messages = snapshot.get("task_context_messages") or []
        return [item for item in messages if isinstance(item, dict) and isinstance(item.get("content"), str)]

    def execute_task_tool(self, run_id: str, tool_call_id: str, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            row = self.db.execute(
                """SELECT r.task_id, r.assignment_id, r.agent_id, t.* FROM runs r JOIN tasks t ON t.id = r.task_id
                   WHERE r.id = ? AND r.task_id IS NOT NULL""", (run_id,),
            ).fetchone()
            if row is None or not row["assignment_id"]:
                return {"status": "denied", "error": "Task Run is not available"}
            principal = self._principal("cloud_agent", row["agent_id"])
            assignment = self.db.execute(
                """SELECT * FROM task_assignments WHERE id = ? AND task_id = ? AND executor_kind = ?
                   AND executor_id = ? AND state IN ('assigned', 'accepted')""",
                (row["assignment_id"], row["task_id"], principal["kind"], principal["id"]),
            ).fetchone()
            if assignment is None:
                return {"status": "denied", "error": "Task Assignment is no longer active"}
            task_id, owner_id = row["task_id"], int(row["owner_user_id"])
            existing = self._task_command_response(owner_id, task_id, principal, "tool." + name, tool_call_id)
            if existing is not None:
                return existing
            if name == "post_progress":
                progress = arguments.get("progress")
                if not isinstance(progress, str) or not progress.strip():
                    return {"status": "error", "error": "progress is required"}
                self._append_task_context(task_id, "task.progress", progress.strip(), principal["kind"], principal["id"], {
                    "assignment_id": assignment["id"],
                })
                response = {"status": "ok", "task_id": task_id, "state": row["state"]}
            elif name == "submit_result":
                result = arguments.get("result")
                evidence = arguments.get("evidence_manifest") or {}
                risk = arguments.get("risk_summary") or ""
                if not isinstance(result, str) or not result.strip() or not isinstance(evidence, dict) or not isinstance(risk, str):
                    return {"status": "error", "error": "result, evidence_manifest, or risk_summary is invalid"}
                task = self.submit_task_result(
                    task_id, owner_id, assignment["id"], principal, result.strip(), evidence, risk,
                )
                response = {"status": "ok", "task_id": task_id, "state": task["state"] if task else "unknown"}
            elif name == "request_proposer_decision":
                reason = arguments.get("reason")
                if not isinstance(reason, str) or not reason.strip():
                    return {"status": "error", "error": "reason is required"}
                require_task_transition(row["state"], "attention_required")
                state_version = int(row["state_version"]) + 1
                self.db.execute(
                    "UPDATE tasks SET state = 'attention_required', state_version = ?, updated_at = ? WHERE id = ?",
                    (state_version, now(), task_id),
                )
                self._append_task_context(task_id, "task.decision_requested", reason.strip(), principal["kind"], principal["id"], {
                    "assignment_id": assignment["id"],
                })
                self._append_task_dispatch(
                    task_id, "task.blocked", "执行需要提出者处理", principal["kind"], principal["id"],
                    {"assignment_id": assignment["id"], "state_version": state_version},
                )
                self._create_notification(
                    owner_id, "attention_required", task_id,
                    {"title": row["title"], "state": "attention_required"},
                    "{}:attention_required:{}".format(task_id, state_version),
                )
                response = {"status": "ok", "task_id": task_id, "state": "attention_required"}
            elif name == "accept_assignment":
                self.db.execute(
                    """UPDATE task_assignments SET state = 'accepted', accepted_at = COALESCE(accepted_at, ?)
                       WHERE id = ? AND state = 'assigned'""", (now(), assignment["id"]),
                )
                response = {"status": "ok", "task_id": task_id, "assignment_id": assignment["id"], "state": "accepted"}
            elif name == "decline_assignment":
                reason = arguments.get("reason")
                if not isinstance(reason, str) or not reason.strip():
                    return {"status": "error", "error": "reason is required"}
                self.db.execute(
                    "UPDATE task_assignments SET state = 'declined', completed_at = ? WHERE id = ? AND state IN ('assigned', 'accepted')",
                    (now(), assignment["id"]),
                )
                if row["state"] not in {"closed", "cancelled", "attention_required"}:
                    require_task_transition(row["state"], "attention_required")
                    state_version = int(row["state_version"]) + 1
                    self.db.execute(
                        "UPDATE tasks SET state = 'attention_required', state_version = ?, updated_at = ? WHERE id = ?",
                        (state_version, now(), task_id),
                    )
                    self._append_task_context(task_id, "assignment.declined", reason.strip(), principal["kind"], principal["id"], {"assignment_id": assignment["id"]})
                    self._append_task_dispatch(task_id, "assignment.declined", "执行 Agent 拒绝了任务", principal["kind"], principal["id"], {"state_version": state_version})
                    self._create_notification(owner_id, "attention_required", task_id, {"title": row["title"], "state": "attention_required"}, "{}:assignment_declined:{}".format(task_id, state_version))
                response = {"status": "ok", "task_id": task_id, "assignment_id": assignment["id"], "state": "declined", "stop_run": True}
            elif name == "delegate_task":
                required = ("target_agent_id", "title", "goal", "input_package", "reason")
                if any(not isinstance(arguments.get(key), str) for key in required) or not isinstance(arguments.get("budget_snapshot"), dict):
                    return {"status": "error", "error": "delegate_task 参数无效"}
                try:
                    child = self.delegate_task(
                        task_id, owner_id, principal, arguments["target_agent_id"], arguments["title"], arguments["goal"],
                        arguments["input_package"], arguments["budget_snapshot"], arguments["reason"],
                    )
                except ValueError as exc:
                    response = {"status": "error", "error": str(exc)}
                except PermissionError as exc:
                    response = {"status": "denied", "error": str(exc)}
                else:
                    response = {"status": "ok", "task_id": task_id, "child_task_id": child["id"], "child_run_id": child.get("run_id")}
            elif name == "collect_child_result":
                child_task_id = arguments.get("child_task_id")
                if not isinstance(child_task_id, str) or not child_task_id:
                    return {"status": "error", "error": "child_task_id is required"}
                result = self.collect_child_result(task_id, child_task_id, owner_id, principal)
                response = {"status": "ok", **result}
            elif name == "close_delegated_task":
                child_task_id, result_summary = arguments.get("child_task_id"), arguments.get("result_summary")
                if not isinstance(child_task_id, str) or not child_task_id or not isinstance(result_summary, str) or not result_summary.strip():
                    return {"status": "error", "error": "child_task_id and result_summary are required"}
                child = self.close_delegated_task(task_id, child_task_id, owner_id, principal, result_summary)
                response = {"status": "ok", "child_task_id": child["id"], "state": child["state"]}
            else:
                return {"status": "denied", "error": "Task tool is not available"}
            self._remember_task_command(owner_id, task_id, principal, "tool." + name, tool_call_id, response)
            self.db.commit()
        return response

    @staticmethod
    def _row(row: Optional[Row]) -> Optional[Dict[str, Any]]:
        return dict(row) if row is not None else None

    def create_agent(self, owner_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        agent_id = str(uuid.uuid4())
        created_at = now()
        api_key = data.get("api_key") or ""
        encrypted_key = self.cipher.encrypt(api_key.encode("utf-8"))
        record = {
            "id": agent_id,
            "owner_user_id": owner_id,
            "name": data["name"],
            "description": data.get("description", ""),
            "avatar_url": data.get("avatar_url", ""),
            "state": "active",
            "current_version": 1,
            "model_display_name": data.get("model_display_name", "Default connection"),
            "model_base_url": data["base_url"],
            "model_id": data["model_id"],
            "encrypted_api_key": encrypted_key,
            "temperature": data.get("temperature", 0.4),
            "max_tokens": data.get("max_tokens", 2048),
            "timeout_seconds": data.get("timeout_seconds", 60),
            "system_prompt": "",
            "encrypted_system_prompt": self._encrypt_text(data.get("system_prompt", "You are a helpful assistant.")),
            "run_policy": json.dumps(data.get("run_policy", {"max_tool_calls": 6, "max_concurrent_runs": 2, "daily_token_budget": 0, "monthly_token_budget": 0})),
            "memory_enabled": 1 if data.get("memory_enabled", False) else 0,
            "memory_retention_days": data.get("memory_retention_days", 30),
            "execution_target": data.get("execution_target", "cloud"),
            "default_device_id": data.get("default_device_id"),
            "default_workspace_id": data.get("default_workspace_id"),
            "model_mode": data.get("model_mode", "server_proxy"),
            "created_at": created_at,
            "updated_at": created_at,
        }
        with self.lock:
            self.db.execute(
                """INSERT INTO agents (id, owner_user_id, name, description, avatar_url, state,
                   current_version, model_display_name, model_base_url, model_id, encrypted_api_key,
                   temperature, max_tokens, timeout_seconds, system_prompt, encrypted_system_prompt,
                   run_policy, memory_enabled,
                   memory_retention_days, execution_target, default_device_id, default_workspace_id, model_mode, created_at, updated_at)
                   VALUES (:id, :owner_user_id, :name, :description, :avatar_url, :state,
                   :current_version, :model_display_name, :model_base_url, :model_id, :encrypted_api_key,
                   :temperature, :max_tokens, :timeout_seconds, :system_prompt, :encrypted_system_prompt,
                   :run_policy, :memory_enabled,
                   :memory_retention_days, :execution_target, :default_device_id, :default_workspace_id, :model_mode, :created_at, :updated_at)""",
                record,
            )
            self._ensure_builtin_tools()
            self._insert_version(agent_id, 1, owner_id, record)
            self.db.commit()
        return self.get_agent(agent_id, owner_id, include_private=True)  # type: ignore

    def _insert_version(self, agent_id: str, version: int, owner_id: int, record: Dict[str, Any]) -> None:
        snapshot = {
            key: value for key, value in record.items()
            if key not in {"encrypted_api_key", "encrypted_system_prompt", "system_prompt"}
        }
        snapshot["system_prompt"] = self._decrypt_text(record["encrypted_system_prompt"])
        snapshot["tool_ids"] = self.tool_ids(agent_id)
        self.db.execute(
            """INSERT INTO agent_versions
               (id, agent_id, version, snapshot, snapshot_encrypted, created_by_user_id, created_at)
               VALUES (?, ?, ?, '{}', ?, ?, ?)""",
            (str(uuid.uuid4()), agent_id, version, self._encrypt_json(snapshot), owner_id, now()),
        )

    def tool_ids(self, agent_id: str) -> List[str]:
        return [row[0] for row in self.db.execute("SELECT tool_id FROM agent_tools WHERE agent_id = ? ORDER BY alias", (agent_id,)).fetchall()]

    def _encrypt_json(self, value: Dict[str, Any]) -> bytes:
        return self.cipher.encrypt(json.dumps(value, separators=(",", ":")).encode("utf-8"))

    def _encrypt_text(self, value: str) -> bytes:
        return self.cipher.encrypt(str(value).encode("utf-8"))

    def _decrypt_text(self, value: bytes) -> str:
        try:
            return self.cipher.decrypt(value).decode("utf-8")
        except (InvalidToken, TypeError) as exc:
            raise RuntimeError("无法读取受保护的 Agent 数据") from exc

    def _decrypt_json(self, value: bytes) -> Dict[str, Any]:
        try:
            return json.loads(self.cipher.decrypt(value).decode("utf-8"))
        except (InvalidToken, ValueError, TypeError) as exc:
            raise RuntimeError("无法读取受保护的 Agent 配置") from exc

    def create_tool(self, owner_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        tool_id, timestamp = str(uuid.uuid4()), now()
        config = data["config"]
        record = {
            "id": tool_id, "owner_user_id": owner_id, "name": data["name"],
            "description": data.get("description", ""), "kind": data.get("kind", "http"),
            "encrypted_config": self._encrypt_json(config), "input_schema": json.dumps(data["input_schema"]),
            "confirmation_mode": data.get("confirmation_mode", "none"),
            "side_effect": data.get("side_effect", "read"),
            "provider_version": data.get("provider_version", {
                "mcp": "mcp-streamable-http-v1", "mcp_stdio": "mcp-stdio-v1", "local": "local-v1",
            }.get(data.get("kind"), "http-v1")),
            "rate_limit_per_run": int(data.get("rate_limit_per_run", 6)), "enabled": 1,
            "created_at": timestamp, "updated_at": timestamp,
        }
        with self.lock:
            try:
                self.db.execute(
                    """INSERT INTO tools (id, owner_user_id, name, description, kind, encrypted_config, input_schema,
                       confirmation_mode, side_effect, provider_version, rate_limit_per_run, enabled, created_at, updated_at)
                       VALUES (:id, :owner_user_id, :name, :description, :kind, :encrypted_config, :input_schema,
                       :confirmation_mode, :side_effect, :provider_version, :rate_limit_per_run, :enabled, :created_at, :updated_at)""",
                    record,
                )
                self.db.commit()
            except self.db.integrity_error as exc:
                self.db.rollback()
                raise HTTPException(status_code=409, detail="同名工具已存在") from exc
        return self.get_tool(tool_id, owner_id)  # type: ignore

    def get_tool(self, tool_id: str, owner_id: int, include_config: bool = False) -> Optional[Dict[str, Any]]:
        with self.lock:
            row = self.db.execute("SELECT * FROM tools WHERE id = ? AND owner_user_id = ?", (tool_id, owner_id)).fetchone()
        if row is None:
            return None
        tool = dict(row)
        tool["enabled"] = bool(tool["enabled"])
        tool["rate_limit_per_run"] = int(tool.get("rate_limit_per_run", 6))
        tool["input_schema"] = json.loads(tool["input_schema"])
        config = self._decrypt_json(tool.pop("encrypted_config"))
        tool["builtin"] = tool["name"] in {definition["name"] for definition in BUILTIN_TOOLS}
        if include_config:
            tool["config"] = redact(config)
        elif tool["kind"] in {"local", "mcp_stdio"}:
            tool["config"] = {"command": config.get("command", config.get("builtin", "")), "remote_tool_name": config.get("remote_tool_name", "")}
        else:
            tool["config"] = {"url": redact(config.get("url", "")), "method": config.get("method", "GET")}
        return tool

    def list_tools(self, owner_id: int) -> List[Dict[str, Any]]:
        with self.lock:
            self._ensure_builtin_tools_for_owner(owner_id)
            self.db.commit()
        with self.lock:
            rows = self.db.execute("SELECT id FROM tools WHERE owner_user_id = ? ORDER BY updated_at DESC", (owner_id,)).fetchall()
        return [self.get_tool(row["id"], owner_id, include_config=True) for row in rows]  # type: ignore

    def _ensure_builtin_tools_for_owner(self, owner_id: int) -> None:
        # The owner may not have an Agent yet; keep the built-in catalog visible.
        for definition in BUILTIN_TOOLS:
            existing = self.db.execute(
                "SELECT id FROM tools WHERE owner_user_id = ? AND name = ?", (owner_id, definition["name"])
            ).fetchone()
            if existing:
                tool_id = existing[0]
                self.db.execute(
                    "UPDATE tools SET encrypted_config = ?, provider_version = ?, updated_at = ? WHERE id = ?",
                    (self._encrypt_json(definition["config"]), definition["provider_version"], now(), tool_id),
                )
            else:
                timestamp = now()
                tool_id = str(uuid.uuid4())
                self.db.execute(
                    """INSERT INTO tools (id, owner_user_id, name, description, kind, encrypted_config, input_schema,
                       confirmation_mode, side_effect, provider_version, rate_limit_per_run, enabled, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 6, 1, ?, ?)""",
                    (tool_id, owner_id, definition["name"], definition["description"], definition["kind"],
                     self._encrypt_json(definition["config"]), json.dumps(definition["input_schema"]),
                     definition["confirmation_mode"], definition["side_effect"], definition["provider_version"], timestamp, timestamp),
                )

    def _arguments_hash(self, arguments: Dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    def count_tool_invocations(self, run_id: str, tool_id: str) -> int:
        with self.lock:
            return int(self.db.execute(
                "SELECT COUNT(*) FROM tool_invocations WHERE run_id = ? AND tool_id = ?", (run_id, tool_id)
            ).fetchone()[0])

    def record_tool_invocation(self, run_id: str, tool_id: str, tool_call_id: str, status: str) -> bool:
        with self.lock:
            if not self.task_budget_allows_tool_call(run_id):
                return False
            self.db.execute(
                "INSERT INTO tool_invocations VALUES (?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), run_id, tool_id, tool_call_id, status[:16], now()),
            )
            self.db.commit()
        return True

    def create_confirmation(self, run_id: str, tool_call_id: str, tool_name: str, arguments: Dict[str, Any], checkpoint: Dict[str, Any]) -> Dict[str, Any]:
        digest = self._arguments_hash(arguments)
        with self.lock:
            task_link = self.db.execute("SELECT task_id FROM runs WHERE id = ?", (run_id,)).fetchone()
        record = {
            "id": str(uuid.uuid4()), "run_id": run_id, "tool_call_id": tool_call_id,
            "tool_name": tool_name, "arguments": "{}", "arguments_hash": digest,
            "arguments_encrypted": self._encrypt_json(arguments), "checkpoint_encrypted": self._encrypt_json(checkpoint),
            "task_id": task_link["task_id"] if task_link else None,
            "state": "pending", "rejection_reason": "", "created_at": now(),
        }
        with self.lock:
            try:
                self.db.execute(
                    """INSERT INTO tool_confirmations (id, run_id, tool_call_id, tool_name, arguments, arguments_hash,
                       arguments_encrypted, checkpoint_encrypted, task_id, state, rejection_reason, created_at)
                       VALUES (:id, :run_id, :tool_call_id, :tool_name, :arguments, :arguments_hash, :arguments_encrypted,
                       :checkpoint_encrypted, :task_id, :state, :rejection_reason, :created_at)""", record,
                )
                self.db.commit()
            except self.db.integrity_error as exc:
                self.db.rollback()
                raise ValueError("同一工具调用已经请求确认") from exc
        return self.get_confirmation(record["id"], run_id)  # type: ignore

    def get_confirmation(self, confirmation_id: str, run_id: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            row = self.db.execute("SELECT * FROM tool_confirmations WHERE id = ? AND run_id = ?", (confirmation_id, run_id)).fetchone()
        if row is None:
            return None
        value = dict(row)
        encrypted = value.pop("arguments_encrypted", None)
        value.pop("checkpoint_encrypted", None)
        value["arguments"] = self._decrypt_json(encrypted) if encrypted else json.loads(value.get("arguments") or "{}")
        return value

    def pending_confirmation(self, run_id: str, state: str = "approved") -> Optional[Dict[str, Any]]:
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM tool_confirmations WHERE run_id = ? AND state = ? ORDER BY created_at DESC LIMIT 1", (run_id, state)
            ).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["arguments"] = self._decrypt_json(value["arguments_encrypted"])
        value["checkpoint"] = self._decrypt_json(value["checkpoint_encrypted"])
        return value

    def decide_confirmation(self, confirmation_id: str, run_id: str, owner_id: int, arguments_hash: str, approve: bool, reason: str = "") -> Optional[Dict[str, Any]]:
        run = self.get_run(run_id, owner_id)
        if run is None:
            return None
        with self.lock:
            row = self.db.execute("SELECT * FROM tool_confirmations WHERE id = ? AND run_id = ?", (confirmation_id, run_id)).fetchone()
            if row is None:
                return None
            if row["state"] != "pending" or row["arguments_hash"] != arguments_hash:
                raise ValueError("确认已失效，工具参数或状态已改变")
            state = "approved" if approve else "rejected"
            self.db.execute(
                "UPDATE tool_confirmations SET state = ?, decided_by_user_id = ?, decided_at = ?, rejection_reason = ? WHERE id = ?",
                (state, owner_id, now(), reason[:500], confirmation_id),
            )
            self.db.commit()
        return self.get_confirmation(confirmation_id, run_id)

    def mark_confirmation_executed(self, confirmation_id: str) -> None:
        with self.lock:
            self.db.execute("UPDATE tool_confirmations SET state = 'executed' WHERE id = ? AND state = 'approved'", (confirmation_id,))
            self.db.commit()

    def list_confirmations(self, run_id: str, owner_id: int) -> Optional[List[Dict[str, Any]]]:
        if self.get_run(run_id, owner_id) is None:
            return None
        with self.lock:
            rows = self.db.execute("SELECT id FROM tool_confirmations WHERE run_id = ? ORDER BY created_at", (run_id,)).fetchall()
        return [self.get_confirmation(row["id"], run_id) for row in rows]  # type: ignore

    def set_agent_tools(self, agent_id: str, owner_id: int, tool_ids: List[str]) -> Optional[Dict[str, Any]]:
        if len(tool_ids) > 32 or len(set(tool_ids)) != len(tool_ids):
            raise HTTPException(status_code=400, detail="工具列表无效")
        with self.lock:
            self._ensure_builtin_tools_for_owner(owner_id)
            agent = self.db.execute("SELECT * FROM agents WHERE id = ? AND owner_user_id = ?", (agent_id, owner_id)).fetchone()
            if agent is None:
                return None
            available = {row[0] for row in self.db.execute("SELECT id FROM tools WHERE owner_user_id = ? AND enabled = 1", (owner_id,)).fetchall()}
            if not set(tool_ids).issubset(available):
                raise HTTPException(status_code=400, detail="工具未找到或不可用")
            self.db.execute("DELETE FROM agent_tools WHERE agent_id = ?", (agent_id,))
            for tool_id in tool_ids:
                name = self.db.execute("SELECT name FROM tools WHERE id = ?", (tool_id,)).fetchone()[0]
                self.db.execute("INSERT INTO agent_tools VALUES (?, ?, ?)", (agent_id, tool_id, name))
            record = dict(agent)
            record["current_version"] += 1
            record["updated_at"] = now()
            self.db.execute("UPDATE agents SET current_version = ?, updated_at = ? WHERE id = ?", (record["current_version"], record["updated_at"], agent_id))
            self._insert_version(agent_id, record["current_version"], owner_id, record)
            self.db.commit()
        return self.get_agent(agent_id, owner_id, include_private=True)

    def assigned_tools(self, agent_id: str, owner_id: int) -> List[Dict[str, Any]]:
        with self.lock:
            rows = self.db.execute("SELECT tool_id FROM agent_tools WHERE agent_id = ?", (agent_id,)).fetchall()
        return [self.get_tool(row["tool_id"], owner_id, include_config=True) for row in rows]  # type: ignore

    def _freeze_run(self, run_id: str, agent: Row, owner_id: int) -> None:
        snapshot = dict(agent)
        if snapshot["model_mode"] == "local_direct":
            snapshot.pop("encrypted_api_key", None)
        else:
            snapshot["encrypted_api_key"] = base64.b64encode(snapshot["encrypted_api_key"]).decode("ascii")
        snapshot["system_prompt"] = self._decrypt_text(snapshot.pop("encrypted_system_prompt"))
        task_link = self.db.execute(
            """SELECT t.* FROM tasks t JOIN runs r ON r.task_id = t.id WHERE r.id = ?""", (run_id,)
        ).fetchone()
        if task_link is not None:
            task = dict(task_link)
            task_goal = self._decrypt_text(task["goal_encrypted"])
            budget_status = self._task_budget_status(task["id"])
            if budget_status and budget_status["max_total_tokens"]:
                remaining_tokens = max(1, budget_status["max_total_tokens"] - budget_status["tokens_used"])
                snapshot["max_tokens"] = min(int(snapshot["max_tokens"]), remaining_tokens)
            run_link = self.db.execute("SELECT assignment_id FROM runs WHERE id = ?", (run_id,)).fetchone()
            assignment = self.db.execute(
                "SELECT * FROM task_assignments WHERE id = ?", (run_link["assignment_id"] if run_link else None,)
            ).fetchone()
            if assignment is None:
                raise RuntimeError("Task Run 缺少有效 Assignment")
            context_events = self.db.execute(
                "SELECT * FROM task_context_events WHERE task_id = ? ORDER BY sequence ASC", (task["id"],)
            ).fetchall()
            context_messages = []
            context_manifest = []
            for event in context_events:
                content = self._decrypt_text(event["content_encrypted"]) if event["content_encrypted"] else ""
                if not content:
                    continue
                context_messages.append({
                    "role": "user", "content": "[{}] {}".format(event["kind"], content),
                })
                context_manifest.append({"id": event["id"], "sequence": event["sequence"], "kind": event["kind"]})
            task_state = {
                "task_id": task["id"], "root_task_id": task["root_task_id"], "parent_task_id": task["parent_task_id"],
                "title": task["title"], "goal": task_goal, "state": task["state"],
                "proposer": {"kind": task["proposer_kind"], "id": task["proposer_id"]},
                "assignment": {"id": assignment["id"], "executor_kind": assignment["executor_kind"], "executor_id": assignment["executor_id"], "attempt": assignment["attempt"]},
                "budget_snapshot": self._json_value(task["budget_snapshot"]),
                "budget_remaining": {
                    "total_tokens": max(0, budget_status["max_total_tokens"] - budget_status["tokens_used"]) if budget_status and budget_status["max_total_tokens"] else None,
                    "tool_calls": max(0, budget_status["max_tool_calls"] - budget_status["tool_calls_used"]) if budget_status and budget_status["max_tool_calls"] else None,
                },
                "context_boundary": "Only this task's authorized context is available.",
            }
            snapshot["task_prompt_version"] = 1
            snapshot["task_state"] = task_state
            snapshot["task_context_manifest"] = context_manifest
            snapshot["task_context_messages"] = context_messages
            snapshot["system_prompt"] += "\n\n" + self._task_system_prompt(task_state)
        tools = self.assigned_tools(snapshot["id"], owner_id)
        if task_link is not None:
            task_tool_names = {item["name"] for item in TASK_TOOL_DEFINITIONS}
            tools = [item for item in tools if item["name"] not in task_tool_names]
            tools.extend(TASK_TOOL_DEFINITIONS)
        for tool in tools:
            if tool["kind"] == "task":
                continue
            row = self.db.execute("SELECT encrypted_config FROM tools WHERE id = ?", (tool["id"],)).fetchone()
            tool["config"] = self._decrypt_json(row[0])
        snapshot["tools"] = tools
        self.db.execute("INSERT INTO run_snapshots VALUES (?, ?, ?)", (run_id, self._encrypt_json(snapshot), now()))

    @staticmethod
    def _task_system_prompt(task: Dict[str, Any]) -> str:
        return """You are an executor in a multi-agent task cluster. You may act only for the current Task below.

Current Task: {task_id}
Title: {title}
Goal: {goal}
State: {state}
Proposer: {proposer_kind}:{proposer_id}

The provided context is the complete authorized context for this Task only. Never assume access to any other task, conversation, device, workspace, or parent/child task unless an explicit, authorized input is supplied. Different tasks are strictly isolated.

Work only with authorized tools and budget. When an explicitly bounded subtask is necessary, use delegate_task with the smallest authorized input package; never create work from plain @ text. Do not claim to close, cancel, or reopen this Task: only its proposer may do so. When work is complete, call submit_result with verifiable evidence and remaining risks; it does not close the Task. Use post_progress for durable progress and request_proposer_decision when requirements, permissions, confirmations, budget, or execution conditions are unclear. Cluster dispatch messages may contain only short scheduling summaries; never publish work dialogue, code, raw tool output, credentials, absolute paths, or hidden reasoning there. Do not expand authority or guess.

All state-changing operations require structured, authorized tools and are checked by the server; this instruction never grants extra permissions.""".format(
            task_id=task["task_id"], title=task["title"], goal=task["goal"], state=task["state"],
            proposer_kind=task["proposer"]["kind"], proposer_id=task["proposer"]["id"],
        )

    def run_snapshot(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            row = self.db.execute("SELECT encrypted_snapshot FROM run_snapshots WHERE run_id = ?", (run_id,)).fetchone()
        return self._decrypt_json(row[0]) if row else None

    def get_agent(self, agent_id: str, viewer_id: int, include_private: bool = False) -> Optional[Dict[str, Any]]:
        with self.lock:
            row = self.db.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        if row is None or row["owner_user_id"] != viewer_id:
            return None
        agent = dict(row)
        encrypted_api_key = agent.pop("encrypted_api_key", None)
        encrypted_prompt = agent.pop("encrypted_system_prompt", None)
        if encrypted_prompt:
            agent["system_prompt"] = self._decrypt_text(encrypted_prompt)
        agent["run_policy"] = json.loads(agent["run_policy"] or "{}")
        agent["memory_enabled"] = bool(agent["memory_enabled"])
        agent["tool_ids"] = self.tool_ids(agent_id)
        agent["is_owner"] = True
        if include_private:
            has_server_key = bool(encrypted_api_key and self.decrypt_api_key(encrypted_api_key))
            local_model = self.get_local_model(agent_id, viewer_id, agent.get("default_device_id")) if agent["model_mode"] == "local_direct" else None
            agent["model"] = {
                "display_name": agent.pop("model_display_name"),
                "base_url": agent.pop("model_base_url"),
                "model_id": agent.pop("model_id"),
                "temperature": agent.pop("temperature"),
                "max_tokens": agent.pop("max_tokens"),
                "timeout_seconds": agent.pop("timeout_seconds"),
                "api_key_configured": bool(local_model) if agent["model_mode"] == "local_direct" else has_server_key,
            }
        else:
            agent.pop("model_display_name", None)
            agent.pop("model_base_url", None)
            agent.pop("model_id", None)
            agent.pop("temperature", None)
            agent.pop("max_tokens", None)
            agent.pop("timeout_seconds", None)
            agent.pop("system_prompt", None)
        return agent

    def create_local_device(self, owner_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        record = {
            "id": str(uuid.uuid4()), "owner_user_id": owner_id, "display_name": data["display_name"],
            "platform": data.get("platform", ""), "cli_version": data.get("cli_version", ""),
            "status": "offline", "capabilities": json.dumps(data.get("capabilities", []), separators=(",", ":")),
            "last_heartbeat_at": None, "created_at": now(),
        }
        with self.lock:
            self.db.execute("""INSERT INTO local_agent_devices
                (id, owner_user_id, display_name, platform, cli_version, status, capabilities, last_heartbeat_at, created_at)
                VALUES (:id, :owner_user_id, :display_name, :platform, :cli_version, :status, :capabilities, :last_heartbeat_at, :created_at)""", record)
            self.db.commit()
        return self.get_local_device(record["id"], owner_id)  # type: ignore

    def start_pairing(self, data: Dict[str, Any]) -> Dict[str, Any]:
        pairing_secret = opaque_token("pair")
        code = "LA-{:06d}".format(secrets.randbelow(1000000))
        record = {
            "id": str(uuid.uuid4()), "pairing_secret_hash": credential_hash(pairing_secret),
            "code_hash": credential_hash(code), "display_name": data["display_name"],
            "platform": data.get("platform", ""), "cli_version": data.get("cli_version", ""),
            "capabilities": json.dumps(data.get("capabilities", []), separators=(",", ":")),
            "state": "pending", "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
            "created_at": now(),
        }
        with self.lock:
            self.db.execute("""INSERT INTO pairing_sessions
                (id, pairing_secret_hash, code_hash, display_name, platform, cli_version, capabilities, state, expires_at, created_at)
                VALUES (:id, :pairing_secret_hash, :code_hash, :display_name, :platform, :cli_version, :capabilities, :state, :expires_at, :created_at)""", record)
            self.db.commit()
        return {"pairing_id": record["id"], "code": code, "pairing_secret": pairing_secret, "expires_at": record["expires_at"]}

    def approve_pairing(self, pairing_id: str, code: str, owner_id: int) -> Optional[Dict[str, Any]]:
        with self.lock:
            session = self.db.execute("SELECT * FROM pairing_sessions WHERE id = ?", (pairing_id,)).fetchone()
            if session is None or session["state"] != "pending" or session["expires_at"] <= now() or not hmac.compare_digest(session["code_hash"], credential_hash(code)):
                return None
            device_id = str(uuid.uuid4())
            timestamp = now()
            self.db.execute("""INSERT INTO local_agent_devices
                (id, owner_user_id, display_name, platform, cli_version, status, capabilities, last_heartbeat_at, created_at, credential_hash)
                VALUES (?, ?, ?, ?, ?, 'offline', ?, NULL, ?, ?)""",
                (device_id, owner_id, session["display_name"], session["platform"], session["cli_version"], session["capabilities"], timestamp, session["pairing_secret_hash"]),
            )
            self.db.execute("""UPDATE pairing_sessions SET state = 'approved', owner_user_id = ?, device_id = ?,
                approved_at = ? WHERE id = ? AND state = 'pending'""",
                (owner_id, device_id, timestamp, pairing_id),
            )
            self.db.commit()
        return self.get_local_device(device_id, owner_id)

    def claim_pairing(self, pairing_id: str, pairing_secret: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            session = self.db.execute("SELECT * FROM pairing_sessions WHERE id = ?", (pairing_id,)).fetchone()
            if session is None or session["expires_at"] <= now() or not hmac.compare_digest(session["pairing_secret_hash"], credential_hash(pairing_secret)):
                return None
            if session["state"] == "pending":
                return {"state": "pending", "expires_at": session["expires_at"]}
            if session["state"] != "approved" or session["consumed_at"]:
                return None
            self.db.execute("UPDATE pairing_sessions SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL", (now(), pairing_id))
            self.db.commit()
        return {"state": "approved", "device_id": session["device_id"]}

    def issue_device_access_token(self, refresh_token: str) -> Optional[Dict[str, Any]]:
        digest = credential_hash(refresh_token)
        with self.lock:
            device = self.db.execute("SELECT * FROM local_agent_devices WHERE credential_hash = ? AND status != 'revoked'", (digest,)).fetchone()
        if device is None:
            return None
        payload = {"device_id": device["id"], "exp": int(time.time()) + 600}
        encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii").rstrip("=")
        signature = hmac.new(settings.service_secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).hexdigest()
        return {"access_token": "{}.{}".format(encoded, signature), "token_type": "Bearer", "expires_in": 600, "device_id": device["id"]}

    def register_device_workspace(self, device_id: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        with self.lock:
            device = self.db.execute("SELECT owner_user_id FROM local_agent_devices WHERE id = ? AND status != 'revoked'", (device_id,)).fetchone()
        if device is None:
            return None
        return self.add_local_workspace(int(device["owner_user_id"]), device_id, data)

    def authenticate_device_access_token(self, token: str) -> Optional[str]:
        try:
            encoded, signature = token.split(".", 1)
            expected = hmac.new(settings.service_secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, signature): return None
            payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8"))
            if int(payload["exp"]) < int(time.time()): return None
            device_id = str(payload["device_id"])
            with self.lock:
                device = self.db.execute("SELECT id FROM local_agent_devices WHERE id = ? AND status != 'revoked'", (device_id,)).fetchone()
            return device_id if device is not None else None
        except (ValueError, KeyError, TypeError, UnicodeDecodeError):
            return None

    def get_local_device(self, device_id: str, owner_id: int) -> Optional[Dict[str, Any]]:
        with self.lock:
            row = self.db.execute("SELECT * FROM local_agent_devices WHERE id = ? AND owner_user_id = ?", (device_id, owner_id)).fetchone()
        if row is None:
            return None
        device = dict(row); device.pop("credential_hash", None); device["capabilities"] = json.loads(device["capabilities"] or "[]")
        return device

    def list_local_devices(self, owner_id: int) -> List[Dict[str, Any]]:
        with self.lock:
            rows = self.db.execute("SELECT id FROM local_agent_devices WHERE owner_user_id = ? ORDER BY created_at DESC", (owner_id,)).fetchall()
        return [self.get_local_device(row["id"], owner_id) for row in rows]  # type: ignore

    def revoke_local_device(self, device_id: str, owner_id: int) -> bool:
        with self.lock:
            changed = self.db.execute("UPDATE local_agent_devices SET status = 'revoked' WHERE id = ? AND owner_user_id = ? AND status != 'revoked'", (device_id, owner_id))
            self.db.commit()
        return changed.rowcount == 1

    def set_local_device_status(self, device_id: str, status: str) -> None:
        if status not in {"online", "offline"}:
            raise ValueError("invalid local device status")
        with self.lock:
            self.db.execute("UPDATE local_agent_devices SET status = ?, last_heartbeat_at = ? WHERE id = ? AND status != 'revoked'", (status, now(), device_id))
            self.db.commit()

    def add_local_workspace(self, owner_id: int, device_id: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        device = self.get_local_device(device_id, owner_id)
        if device is None or device["status"] == "revoked":
            return None
        record = {"id": str(uuid.uuid4()), "device_id": device_id, "display_name": data["display_name"], "policy_version": data.get("policy_version", 1), "capabilities": json.dumps(data.get("capabilities", []), separators=(",", ":")), "created_at": now()}
        with self.lock:
            self.db.execute("INSERT INTO local_workspaces (id, device_id, display_name, policy_version, capabilities, created_at) VALUES (:id, :device_id, :display_name, :policy_version, :capabilities, :created_at)", record)
            self.db.commit()
        return self.get_local_workspace(record["id"], owner_id)

    def get_local_workspace(self, workspace_id: str, owner_id: int) -> Optional[Dict[str, Any]]:
        with self.lock:
            row = self.db.execute("""SELECT w.* FROM local_workspaces w JOIN local_agent_devices d ON d.id = w.device_id
                WHERE w.id = ? AND d.owner_user_id = ? AND d.status != 'revoked'""", (workspace_id, owner_id)).fetchone()
        if row is None:
            return None
        workspace = dict(row); workspace["capabilities"] = json.loads(workspace["capabilities"] or "[]")
        return workspace

    def list_local_workspaces(self, device_id: str, owner_id: int) -> Optional[List[Dict[str, Any]]]:
        if self.get_local_device(device_id, owner_id) is None:
            return None
        with self.lock:
            rows = self.db.execute("SELECT id FROM local_workspaces WHERE device_id = ? ORDER BY created_at DESC", (device_id,)).fetchall()
        return [self.get_local_workspace(row["id"], owner_id) for row in rows]  # type: ignore

    def get_local_model(self, agent_id: str, owner_id: int, device_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        query = """SELECT m.* FROM local_agent_models m JOIN local_agent_devices d ON d.id = m.device_id
            JOIN agents a ON a.id = m.agent_id WHERE m.agent_id = ? AND d.owner_user_id = ? AND a.owner_user_id = ?"""
        params: List[Any] = [agent_id, owner_id, owner_id]
        if device_id:
            query += " AND m.device_id = ?"; params.append(device_id)
        with self.lock:
            row = self.db.execute(query, tuple(params)).fetchone()
        return dict(row) if row is not None else None

    def register_local_model(self, device_id: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        with self.lock:
            device = self.db.execute("SELECT owner_user_id FROM local_agent_devices WHERE id = ? AND status != 'revoked'", (device_id,)).fetchone()
            if device is None:
                return None
            owner_id = int(device["owner_user_id"])
            agent = self.db.execute("SELECT id FROM agents WHERE id = ? AND owner_user_id = ?", (data["agent_id"], owner_id)).fetchone()
            if agent is None:
                return None
            record = {"agent_id": data["agent_id"], "device_id": device_id, "model_base_url": data["base_url"], "model_id": data["model_id"], "configured_at": now()}
            self.db.execute("""INSERT INTO local_agent_models (agent_id, device_id, model_base_url, model_id, configured_at)
                VALUES (:agent_id, :device_id, :model_base_url, :model_id, :configured_at)
                ON CONFLICT(agent_id) DO UPDATE SET device_id = excluded.device_id, model_base_url = excluded.model_base_url,
                model_id = excluded.model_id, configured_at = excluded.configured_at""", record)
            self.db.commit()
        return self.get_local_model(record["agent_id"], owner_id, device_id)

    def remove_local_model(self, agent_id: str, device_id: str) -> bool:
        with self.lock:
            result = self.db.execute("DELETE FROM local_agent_models WHERE agent_id = ? AND device_id = ?", (agent_id, device_id))
            self.db.commit()
        return result.rowcount == 1

    def offer_local_run(self, device_id: str) -> Optional[Dict[str, Any]]:
        lease_id = str(uuid.uuid4())
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=90)).isoformat().replace("+00:00", "Z")
        with self.lock:
            row = self.db.execute("""SELECT d.run_id FROM local_run_dispatches d JOIN runs r ON r.id = d.run_id
                JOIN agents a ON a.id = r.agent_id WHERE d.device_id = ?
                AND (d.executor_state = 'pending' OR (d.executor_state = 'offered' AND d.lease_expires_at <= ?))
                AND a.model_mode = 'local_direct' ORDER BY d.run_id ASC LIMIT 1""", (device_id, now())).fetchone()
            if row is None:
                return None
            changed = self.db.execute("""UPDATE local_run_dispatches SET executor_state = 'offered', lease_id = ?, lease_expires_at = ?
                WHERE run_id = ? AND device_id = ?
                AND (executor_state = 'pending' OR (executor_state = 'offered' AND lease_expires_at <= ?))""",
                (lease_id, expires_at, row["run_id"], device_id, now())).rowcount
            self.db.commit()
        if changed != 1:
            return None
        context = self.get_run_context(row["run_id"])
        if context is None or context.get("model_mode") != "local_direct":
            return None
        history = self.model_messages(context["conversation_id"], context["context_epoch"])
        return {
            "run_id": row["run_id"], "lease_id": lease_id, "lease_expires_at": expires_at,
            "workspace_id": context["default_workspace_id"], "agent_id": context["agent_id"],
            "system_prompt": context["system_prompt"], "messages": history,
            "max_tokens": context["max_tokens"], "temperature": context["temperature"],
        }

    def claim_local_run(self, run_id: str, device_id: str, lease_id: str, local_session_id: str) -> bool:
        with self.lock:
            result = self.db.execute("""UPDATE local_run_dispatches SET executor_state = 'claimed', local_session_id = ?
                WHERE run_id = ? AND device_id = ? AND lease_id = ? AND executor_state = 'offered' AND lease_expires_at > ?""",
                (local_session_id, run_id, device_id, lease_id, now()))
            if result.rowcount != 1:
                self.db.commit()
                return False
            started = self.db.execute(
                "UPDATE runs SET state = 'running', started_at = COALESCE(started_at, ?), attempt = attempt + 1 WHERE id = ? AND state = 'queued'",
                (now(), run_id),
            )
            if started.rowcount != 1:
                self.db.rollback()
                return False
            self.db.commit()
        return True

    def renew_local_lease(self, run_id: str, device_id: str, lease_id: str) -> Optional[bool]:
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=90)).isoformat().replace("+00:00", "Z")
        with self.lock:
            result = self.db.execute("""UPDATE local_run_dispatches SET lease_expires_at = ? WHERE run_id = ? AND device_id = ?
                AND lease_id = ? AND executor_state = 'claimed' AND lease_expires_at > ?""", (expires_at, run_id, device_id, lease_id, now()))
            cancelled = self.db.execute("SELECT state FROM runs WHERE id = ?", (run_id,)).fetchone()
            self.db.commit()
        if result.rowcount != 1:
            return None
        return cancelled is not None and cancelled["state"] == "cancelled"

    def append_local_run_event(self, run_id: str, device_id: str, lease_id: str, sequence: int, event_type: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        with self.lock:
            result = self.db.execute("""UPDATE local_run_dispatches SET last_acked_sequence = ? WHERE run_id = ? AND device_id = ?
                AND lease_id = ? AND executor_state = 'claimed' AND lease_expires_at > ? AND last_acked_sequence < ?""",
                (sequence, run_id, device_id, lease_id, now(), sequence))
            self.db.commit()
        if result.rowcount != 1:
            return None
        return self.add_event(run_id, event_type, payload)

    def finish_local_run(self, run_id: str, device_id: str, lease_id: str, state: str, content: str = "", error: str = "") -> bool:
        if state not in {"completed", "failed", "cancelled"}:
            raise ValueError("invalid local terminal state")
        with self.lock:
            dispatch = self.db.execute("""SELECT executor_state FROM local_run_dispatches WHERE run_id = ? AND device_id = ? AND lease_id = ?
                AND executor_state = 'claimed' AND lease_expires_at > ?""", (run_id, device_id, lease_id, now())).fetchone()
            conversation = self.db.execute("SELECT conversation_id, context_epoch FROM runs JOIN conversations ON conversations.id = runs.conversation_id WHERE runs.id = ?", (run_id,)).fetchone()
        if dispatch is None:
            return False
        try:
            self.update_run(run_id, state, final_content=content, error_message=error)
        except ValueError:
            return False
        self.sync_task_run_state(run_id, state, content=content, error=error)
        with self.lock:
            self.db.execute("UPDATE local_run_dispatches SET executor_state = ? WHERE run_id = ?", (state, run_id))
            self.db.commit()
        if state == "completed" and content and conversation is not None:
            self.add_message(conversation["conversation_id"], run_id, "assistant", content, int(conversation["context_epoch"]))
        return True

    def bind_local_agent(self, agent_id: str, owner_id: int, device_id: str, workspace_id: str, model_mode: str) -> Optional[Dict[str, Any]]:
        device = self.get_local_device(device_id, owner_id)
        workspace = self.get_local_workspace(workspace_id, owner_id)
        if device is None or device.get("status") == "revoked" or workspace is None or workspace["device_id"] != device_id:
            raise ValueError("本地设备或工作区未找到")
        with self.lock:
            agent = self.db.execute("SELECT encrypted_api_key FROM agents WHERE id = ? AND owner_user_id = ?", (agent_id, owner_id)).fetchone()
            if agent is None:
                return None
            if model_mode == "local_direct":
                model = self.get_local_model(agent_id, owner_id, device_id)
                if model is None:
                    raise ValueError("该设备尚未登记本地模型凭据")
                if self.decrypt_api_key(agent["encrypted_api_key"]):
                    raise ValueError("local_direct 只能绑定从未向服务端提交模型密钥的 Agent")
                result = self.db.execute("""UPDATE agents SET execution_target = 'local', default_device_id = ?, default_workspace_id = ?,
                    model_mode = 'local_direct', model_base_url = ?, model_id = ?, encrypted_api_key = ?, current_version = current_version + 1,
                    updated_at = ? WHERE id = ? AND owner_user_id = ?""", (device_id, workspace_id, model["model_base_url"], model["model_id"], self.cipher.encrypt(b""), now(), agent_id, owner_id))
            else:
                if not self.decrypt_api_key(agent["encrypted_api_key"]):
                    raise ValueError("server_proxy 需要服务端模型密钥")
                result = self.db.execute("""UPDATE agents SET execution_target = 'local', default_device_id = ?, default_workspace_id = ?,
                    model_mode = 'server_proxy', current_version = current_version + 1, updated_at = ? WHERE id = ? AND owner_user_id = ?""", (device_id, workspace_id, now(), agent_id, owner_id))
            self.db.commit()
        return self.get_agent(agent_id, owner_id, include_private=True) if result.rowcount else None

    def list_agents(self, owner_id: int) -> List[Dict[str, Any]]:
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM agents WHERE owner_user_id = ? AND state != 'archived' ORDER BY updated_at DESC",
                (owner_id,),
            ).fetchall()
        return [self.get_agent(row["id"], owner_id) for row in rows]  # type: ignore

    def update_agent(self, agent_id: str, owner_id: int, changes: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        with self.lock:
            row = self.db.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
            if row is None or row["owner_user_id"] != owner_id:
                return None
            if changes.get("model_mode") == "local_direct":
                raise ValueError("local_direct 必须通过 local-bind 绑定")
            if row["model_mode"] == "local_direct" and any(key in changes for key in ("api_key", "base_url", "model_id")):
                raise ValueError("local_direct 的模型配置只能在本机 daemon 中更新")
            record = dict(row)
            mapping = {
                "name": "name", "description": "description", "avatar_url": "avatar_url",
                "model_display_name": "model_display_name", "base_url": "model_base_url",
                "model_id": "model_id", "temperature": "temperature", "max_tokens": "max_tokens",
                "timeout_seconds": "timeout_seconds", "system_prompt": "system_prompt",
                "execution_target": "execution_target", "default_device_id": "default_device_id",
                "default_workspace_id": "default_workspace_id", "model_mode": "model_mode",
            }
            for source, target in mapping.items():
                if source in changes and changes[source] is not None:
                    record[target] = changes[source]
            if changes.get("api_key"):
                record["encrypted_api_key"] = self.cipher.encrypt(changes["api_key"].encode("utf-8"))
            if "system_prompt" in changes:
                record["system_prompt"] = ""
                record["encrypted_system_prompt"] = self._encrypt_text(changes["system_prompt"])
            if "run_policy" in changes:
                record["run_policy"] = json.dumps(changes["run_policy"], separators=(",", ":"))
            if "memory_enabled" in changes:
                record["memory_enabled"] = 1 if changes["memory_enabled"] else 0
            if "memory_retention_days" in changes:
                record["memory_retention_days"] = changes["memory_retention_days"]
            record["current_version"] += 1
            record["updated_at"] = now()
            self.db.execute(
                """UPDATE agents SET name=:name, description=:description, avatar_url=:avatar_url,
                current_version=:current_version, model_display_name=:model_display_name, model_base_url=:model_base_url,
                model_id=:model_id, encrypted_api_key=:encrypted_api_key, temperature=:temperature,
                max_tokens=:max_tokens, timeout_seconds=:timeout_seconds, system_prompt=:system_prompt,
                encrypted_system_prompt=:encrypted_system_prompt, run_policy=:run_policy,
                memory_enabled=:memory_enabled, memory_retention_days=:memory_retention_days,
                execution_target=:execution_target, default_device_id=:default_device_id,
                default_workspace_id=:default_workspace_id, model_mode=:model_mode,
                updated_at=:updated_at WHERE id=:id""", record,
            )
            self._insert_version(agent_id, record["current_version"], owner_id, record)
            self.db.commit()
        return self.get_agent(agent_id, owner_id, include_private=True)

    def set_agent_state(self, agent_id: str, owner_id: int, state: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            row = self.db.execute("SELECT owner_user_id FROM agents WHERE id = ?", (agent_id,)).fetchone()
            if row is None or row["owner_user_id"] != owner_id:
                return None
            self.db.execute("UPDATE agents SET state = ?, updated_at = ? WHERE id = ?", (state, now(), agent_id))
            self.db.commit()
        return self.get_agent(agent_id, owner_id)

    def create_conversation(self, agent_id: str, owner_id: int, title: str = "新会话") -> Optional[Dict[str, Any]]:
        if self.get_agent(agent_id, owner_id) is None:
            return None
        conversation_id = str(uuid.uuid4())
        timestamp = now()
        conversation = {
            "id": conversation_id, "agent_id": agent_id, "owner_user_id": owner_id,
            "title": title[:120] or "新会话", "context_epoch": 0,
            "deleted_at": None, "created_at": timestamp, "updated_at": timestamp,
        }
        with self.lock:
            self.db.execute(
                "INSERT INTO conversations VALUES (:id, :agent_id, :owner_user_id, :title, :context_epoch, :deleted_at, :created_at, :updated_at)",
                conversation,
            )
            self.db.commit()
        return conversation

    def list_conversations(self, agent_id: str, owner_id: int) -> List[Dict[str, Any]]:
        with self.lock:
            return [dict(row) for row in self.db.execute(
                "SELECT * FROM conversations WHERE agent_id = ? AND owner_user_id = ? AND deleted_at IS NULL ORDER BY updated_at DESC",
                (agent_id, owner_id),
            ).fetchall()]

    def get_conversation(self, conversation_id: str, owner_id: int) -> Optional[Dict[str, Any]]:
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM conversations WHERE id = ? AND owner_user_id = ? AND deleted_at IS NULL",
                (conversation_id, owner_id),
            ).fetchone()
        return self._row(row)

    def channel_event_result(self, provider: str, bot_id: str, event_id: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            row = self.db.execute(
                """SELECT conversation_id, run_id FROM channel_event_deduplications
                   WHERE provider = ? AND bot_id = ? AND event_id = ?""",
                (provider, bot_id, event_id),
            ).fetchone()
        return self._row(row)

    def remember_channel_event(self, provider: str, bot_id: str, event_id: str,
                               conversation_id: str, run_id: str, owner_user_id: int) -> None:
        with self.lock:
            self.db.execute(
                """INSERT INTO channel_event_deduplications
                   (provider, bot_id, event_id, conversation_id, run_id, owner_user_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (provider, bot_id, event_id, conversation_id, run_id, owner_user_id, now()),
            )
            self.db.commit()

    def conversation_messages(self, conversation_id: str, owner_id: int) -> Optional[Dict[str, Any]]:
        conversation = self.get_conversation(conversation_id, owner_id)
        if conversation is None:
            return None
        with self.lock:
            # Tool responses are durable model context, not chat messages. Returning
            # them here made a page refresh render raw JSON responses as assistant
            # replies. Their reviewable presentation belongs to the run trace.
            messages = [dict(row) for row in self.db.execute(
                "SELECT * FROM messages WHERE conversation_id = ? AND role IN ('user', 'assistant') ORDER BY created_at ASC",
                (conversation_id,),
            ).fetchall()]
        for message in messages:
            encrypted = message.pop("content_encrypted", None)
            message["content"] = self._decrypt_text(encrypted) if encrypted else message.get("content", "")
        return {"conversation": conversation, "messages": messages}

    def create_run(self, conversation_id: str, owner_id: int, content: str, task_id: Optional[str] = None,
                   assignment_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        conversation = self.get_conversation(conversation_id, owner_id)
        if conversation is None:
            return None
        with self.lock:
            if task_id:
                task = self._task_for_owner(task_id, owner_id)
                if task is None or task["conversation_id"] != conversation_id:
                    return {"error": "任务上下文不可用"}
                if assignment_id is None:
                    return {"error": "任务执行分配不可用"}
                assignment = self.db.execute(
                    """SELECT * FROM task_assignments WHERE id = ? AND task_id = ?
                       AND executor_kind = 'cloud_agent' AND state = 'assigned'""", (assignment_id, task_id),
                ).fetchone()
                if assignment is None or assignment["executor_id"] != conversation["agent_id"]:
                    return {"error": "任务执行分配不可用"}
                budget_error = self._task_budget_error(task_id, include_next_run=True)
                if budget_error:
                    return {"error": budget_error}
            agent = self.db.execute("SELECT * FROM agents WHERE id = ?", (conversation["agent_id"],)).fetchone()
            if agent is None or agent["state"] != "active":
                return {"error": "Agent 当前不可运行"}
            policy = json.loads(agent["run_policy"] or "{}")
            active = self.db.execute(
                "SELECT COUNT(*) FROM runs WHERE agent_id = ? AND state IN ('queued', 'running', 'waiting_confirmation')",
                (conversation["agent_id"],),
            ).fetchone()[0]
            if active >= max(1, int(policy.get("max_concurrent_runs", 2))):
                return {"error": "Agent 并发运行数已达到上限"}
            daily_limit = max(0, int(policy.get("daily_token_budget", 0)))
            monthly_limit = max(0, int(policy.get("monthly_token_budget", 0)))
            if daily_limit and self._token_usage_since(conversation["agent_id"], now_offset(days=-1)) >= daily_limit:
                return {"error": "Agent 今日 token 预算已用尽"}
            if monthly_limit and self._token_usage_since(conversation["agent_id"], now_offset(days=-30)) >= monthly_limit:
                return {"error": "Agent 本月 token 预算已用尽"}
            run_id = str(uuid.uuid4())
            timestamp = now()
            self.db.execute(
                """INSERT INTO runs (id, conversation_id, agent_id, agent_version, initiated_by_user_id, task_id, assignment_id,
                   state, final_content, error_message, created_at, started_at, completed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', '', '', ?, NULL, NULL)""",
                (run_id, conversation_id, conversation["agent_id"], agent["current_version"], owner_id, task_id,
                 assignment_id, timestamp),
            )
            self.db.execute(
                """INSERT INTO messages
                   (id, conversation_id, run_id, role, content, content_encrypted, context_epoch, created_at)
                   VALUES (?, ?, ?, 'user', '', ?, ?, ?)""",
                (str(uuid.uuid4()), conversation_id, run_id, self._encrypt_text(content), conversation["context_epoch"], timestamp),
            )
            self._freeze_run(run_id, agent, owner_id)
            if agent["execution_target"] == "local":
                device_id, workspace_id = agent["default_device_id"], agent["default_workspace_id"]
                if not device_id or not workspace_id:
                    self.db.rollback()
                    return {"error": "Local Agent 尚未绑定设备和工作区"}
                workspace = self.get_local_workspace(workspace_id, owner_id)
                if workspace is None or workspace["device_id"] != device_id:
                    self.db.rollback()
                    return {"error": "Local Agent 绑定的设备或工作区已不可用"}
                self.db.execute("""INSERT INTO local_run_dispatches
                    (run_id, device_id, workspace_id, executor_state, last_acked_sequence) VALUES (?, ?, ?, 'pending', 0)""",
                    (run_id, device_id, workspace_id))
            else:
                self.db.execute(
                    "INSERT INTO outbox_events VALUES (?, 'agent_run', ?, 'agent.run.queued', ?, NULL, ?)",
                    (str(uuid.uuid4()), run_id, json.dumps({"run_id": run_id}), timestamp),
                )
            self.db.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (timestamp, conversation_id))
            self.db.commit()
        return self.get_run(run_id, owner_id)

    def _token_usage_since(self, agent_id: str, since: str) -> int:
        rows = self.db.execute("SELECT usage FROM runs WHERE agent_id = ? AND created_at >= ?", (agent_id, since)).fetchall()
        total = 0
        for row in rows:
            usage = json.loads(row["usage"] or "{}")
            total += int(usage.get("total_tokens") or usage.get("input_tokens", 0) + usage.get("output_tokens", 0))
        return total

    def pending_outbox_events(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM outbox_events WHERE published_at IS NULL ORDER BY created_at ASC LIMIT ?", (limit,)
            ).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def mark_outbox_published(self, event_id: str) -> None:
        with self.lock:
            self.db.execute("UPDATE outbox_events SET published_at = ? WHERE id = ? AND published_at IS NULL", (now(), event_id))
            self.db.commit()

    def enqueue_confirmation_resume(self, run_id: str) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO outbox_events VALUES (?, 'agent_run', ?, 'agent.run.confirmed', ?, NULL, ?)",
                (str(uuid.uuid4()), run_id, json.dumps({"run_id": run_id, "resume_confirmation": True}), now()),
            )
            self.db.commit()

    def get_run(self, run_id: str, owner_id: int) -> Optional[Dict[str, Any]]:
        with self.lock:
            row = self.db.execute("SELECT * FROM runs WHERE id = ? AND initiated_by_user_id = ?", (run_id, owner_id)).fetchone()
        run = self._row(row)
        if run is not None:
            encrypted = run.pop("final_content_encrypted", None)
            run["final_content"] = self._decrypt_text(encrypted) if encrypted else run.get("final_content", "")
            run["usage"] = json.loads(run.get("usage") or "{}")
            with self.lock:
                dispatch = self.db.execute("SELECT device_id, workspace_id, executor_state FROM local_run_dispatches WHERE run_id = ?", (run_id,)).fetchone()
            if dispatch is not None:
                run["local_dispatch"] = dict(dispatch)
        return run

    def get_run_context(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            row = self.db.execute(
                """SELECT runs.*, agents.*, conversations.context_epoch FROM runs
                   JOIN agents ON agents.id = runs.agent_id
                   JOIN conversations ON conversations.id = runs.conversation_id WHERE runs.id = ?""",
                (run_id,),
            ).fetchone()
        context = self._row(row)
        if context is None:
            return None
        snapshot = self.run_snapshot(run_id)
        if snapshot is not None:
            context.update(snapshot)
            if context.get("model_mode") == "local_direct":
                context.pop("encrypted_api_key", None)
            elif context.get("encrypted_api_key"):
                context["encrypted_api_key"] = base64.b64decode(context["encrypted_api_key"])
        return context

    def update_run(self, run_id: str, state: str, final_content: str = "", error_message: str = "") -> None:
        completed_at = now() if state in ("completed", "failed", "cancelled") else None
        with self.lock:
            current = self.db.execute("SELECT state FROM runs WHERE id = ?", (run_id,)).fetchone()
            if current is None:
                raise ValueError("运行不存在")
            require_transition(current["state"], state)
            self.db.execute(
                """UPDATE runs SET state = ?, final_content = '', final_content_encrypted = ?,
                   error_message = ?, completed_at = COALESCE(?, completed_at),
                   started_at = CASE WHEN ? = 'running' THEN COALESCE(started_at, ?) ELSE started_at END
                   WHERE id = ? AND state = ?""",
                (state, self._encrypt_text(final_content), redact(error_message)[:500], completed_at,
                 state, now(), run_id, current["state"]),
            )
            if self.db.execute("SELECT state FROM runs WHERE id = ?", (run_id,)).fetchone()["state"] != state:
                self.db.rollback()
                raise ValueError("运行状态已被并发更新")
            self.db.commit()

    def try_start_run(self, run_id: str, recover: bool = False, resume_confirmation: bool = False) -> bool:
        allowed = ("queued", "running") if recover else (("waiting_confirmation",) if resume_confirmation else ("queued",))
        placeholders = ",".join("?" for _ in allowed)
        with self.lock:
            result = self.db.execute(
                "UPDATE runs SET state = 'running', started_at = COALESCE(started_at, ?), attempt = attempt + 1 "
                "WHERE id = ? AND state IN ({})".format(placeholders),
                (now(), run_id) + allowed,
            )
            self.db.commit()
        return result.rowcount == 1

    def update_usage(self, run_id: str, usage: Dict[str, Any], context_manifest: Dict[str, Any]) -> None:
        with self.lock:
            self.db.execute(
                "UPDATE runs SET usage = ?, context_manifest_encrypted = ? WHERE id = ?",
                (json.dumps(usage, separators=(",", ":")), self._encrypt_json(context_manifest), run_id),
            )
            self._mark_task_budget_exhausted(run_id)
            self.db.commit()

    def task_budget_allows_tool_call(self, run_id: str) -> bool:
        with self.lock:
            row = self.db.execute("SELECT task_id FROM runs WHERE id = ?", (run_id,)).fetchone()
            return row is None or row["task_id"] is None or self._task_budget_error(row["task_id"]) is None

    def is_cancelled(self, run_id: str) -> bool:
        with self.lock:
            row = self.db.execute("SELECT state FROM runs WHERE id = ?", (run_id,)).fetchone()
        return row is None or row["state"] == "cancelled"

    def add_message(self, conversation_id: str, run_id: str, role: str, content: str, epoch: int) -> None:
        with self.lock:
            self.db.execute(
                """INSERT INTO messages
                   (id, conversation_id, run_id, role, content, content_encrypted, context_epoch, created_at)
                   VALUES (?, ?, ?, ?, '', ?, ?, ?)""",
                (str(uuid.uuid4()), conversation_id, run_id, role, self._encrypt_text(content), epoch, now()),
            )
            self.db.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now(), conversation_id))
            self.db.commit()

    def model_messages(self, conversation_id: str, epoch: int) -> List[Dict[str, str]]:
        with self.lock:
            rows = self.db.execute(
                "SELECT role, content, content_encrypted FROM messages WHERE conversation_id = ? AND context_epoch = ? AND role IN ('user', 'assistant') ORDER BY created_at ASC",
                (conversation_id, epoch),
            ).fetchall()
        return [
            {"role": row["role"], "content": self._decrypt_text(row["content_encrypted"]) if row["content_encrypted"] else row["content"]}
            for row in rows
        ]

    def create_memory(self, agent_id: str, owner_id: int, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        agent = self.get_agent(agent_id, owner_id)
        if agent is None:
            return None
        if not agent["memory_enabled"]:
            raise ValueError("长期记忆尚未为此 Agent 授权")
        content = str(data["content"]).strip()
        kind = str(data.get("kind", "fact"))
        if kind not in {"preference", "profile", "constraint", "fact", "experience"}:
            raise ValueError("记忆类型无效")
        record = {
            "id": str(uuid.uuid4()), "owner_user_id": owner_id, "agent_id": agent_id,
            "content_encrypted": self._encrypt_text(content), "embedding": "[]",
            "expires_at": data.get("expires_at") or now_offset(int(agent.get("memory_retention_days", 30))),
            "source_message_id": data.get("source_message_id"), "scope": "agent", "kind": kind,
            "source_confidence": "user", "importance": int(data.get("importance", 50)),
            "access_count": 0, "conflict_state": "active", "last_accessed_at": None, "created_at": now(),
        }
        if not 0 <= record["importance"] <= 100:
            raise ValueError("记忆重要度必须在 0 到 100 之间")
        with self.lock:
            # Do not silently overwrite conflicting facts. A same-kind exact duplicate is rejected;
            # a same-kind different value remains visible as conflicted until the user removes one.
            active = self.db.execute(
                "SELECT id, content_encrypted FROM memory_items WHERE agent_id = ? AND kind = ? AND conflict_state = 'active'", (agent_id, kind)
            ).fetchall()
            for item in active:
                if self._decrypt_text(item["content_encrypted"]).casefold() == content.casefold():
                    raise ValueError("相同记忆已存在")
            if kind in {"preference", "profile", "constraint"} and active:
                self.db.execute("UPDATE memory_items SET conflict_state = 'conflicted' WHERE agent_id = ? AND kind = ? AND conflict_state = 'active'", (agent_id, kind))
                record["conflict_state"] = "conflicted"
            self.db.execute(
                """INSERT INTO memory_items (id, owner_user_id, agent_id, content_encrypted, embedding, expires_at,
                   source_message_id, scope, kind, source_confidence, importance, access_count, conflict_state,
                   last_accessed_at, created_at) VALUES (:id, :owner_user_id, :agent_id, :content_encrypted, :embedding,
                   :expires_at, :source_message_id, :scope, :kind, :source_confidence, :importance, :access_count,
                   :conflict_state, :last_accessed_at, :created_at)""", record,
            )
            self.db.commit()
        return self.get_memory(record["id"], agent_id, owner_id)

    def get_memory(self, memory_id: str, agent_id: str, owner_id: int) -> Optional[Dict[str, Any]]:
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM memory_items WHERE id = ? AND agent_id = ? AND owner_user_id = ?", (memory_id, agent_id, owner_id)
            ).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["content"] = self._decrypt_text(value.pop("content_encrypted"))
        return value

    def list_memories(self, agent_id: str, owner_id: int) -> Optional[List[Dict[str, Any]]]:
        if self.get_agent(agent_id, owner_id) is None:
            return None
        with self.lock:
            rows = self.db.execute(
                "SELECT id FROM memory_items WHERE agent_id = ? AND owner_user_id = ? AND conflict_state != 'deleted' ORDER BY importance DESC, created_at DESC",
                (agent_id, owner_id),
            ).fetchall()
        return [self.get_memory(row["id"], agent_id, owner_id) for row in rows]  # type: ignore

    def retrieve_memories(self, agent_id: str, owner_id: int, query: str, limit: int = 6) -> List[Dict[str, Any]]:
        words = {word for word in re.findall(r"[\w\u4e00-\u9fff]+", query.casefold()) if len(word) > 1}
        with self.lock:
            rows = self.db.execute(
                """SELECT * FROM memory_items WHERE agent_id = ? AND owner_user_id = ? AND conflict_state = 'active'
                   AND (expires_at IS NULL OR expires_at > ?) ORDER BY importance DESC, created_at DESC""", (agent_id, owner_id, now())
            ).fetchall()
            scored = []
            for row in rows:
                value = dict(row)
                content = self._decrypt_text(value["content_encrypted"])
                overlap = sum(word in content.casefold() for word in words)
                if overlap or not words:
                    value["content"] = content
                    scored.append((overlap * 100 + int(value["importance"]), value))
            selected = [item for _, item in sorted(scored, key=lambda pair: pair[0], reverse=True)[:limit]]
            for item in selected:
                self.db.execute("UPDATE memory_items SET access_count = access_count + 1, last_accessed_at = ? WHERE id = ?", (now(), item["id"]))
            self.db.commit()
        return selected

    def delete_memory(self, memory_id: str, agent_id: str, owner_id: int) -> bool:
        with self.lock:
            result = self.db.execute(
                "UPDATE memory_items SET conflict_state = 'deleted' WHERE id = ? AND agent_id = ? AND owner_user_id = ? AND conflict_state != 'deleted'",
                (memory_id, agent_id, owner_id),
            )
            self.db.commit()
        return result.rowcount > 0

    def clear_context(self, conversation_id: str, owner_id: int) -> Optional[Dict[str, Any]]:
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM conversations WHERE id = ? AND owner_user_id = ? AND deleted_at IS NULL",
                (conversation_id, owner_id),
            ).fetchone()
            if row is None:
                return None
            epoch = row["context_epoch"] + 1
            timestamp = now()
            self.db.execute("UPDATE conversations SET context_epoch = ?, updated_at = ? WHERE id = ?", (epoch, timestamp, conversation_id))
            self.db.commit()
        updated = dict(row)
        updated["context_epoch"] = epoch
        updated["updated_at"] = timestamp
        return updated

    def delete_conversation(self, conversation_id: str, owner_id: int) -> bool:
        with self.lock:
            result = self.db.execute(
                "UPDATE conversations SET deleted_at = ?, updated_at = ? WHERE id = ? AND owner_user_id = ? AND deleted_at IS NULL",
                (now(), now(), conversation_id, owner_id),
            )
            self.db.commit()
        return result.rowcount > 0

    def add_event(self, run_id: str, event_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            if self.db.postgres:
                sequence = self.db.execute(
                    "UPDATE runs SET event_sequence = event_sequence + 1 WHERE id = ? RETURNING event_sequence", (run_id,)
                ).fetchone()[0]
            else:
                sequence = self.db.execute("SELECT event_sequence + 1 FROM runs WHERE id = ?", (run_id,)).fetchone()[0]
                self.db.execute("UPDATE runs SET event_sequence = ? WHERE id = ?", (sequence, run_id))
            event = {
                "type": event_type, "run_id": run_id, "sequence": sequence,
                "timestamp": now(), "payload": payload,
            }
            self.db.execute(
                """INSERT INTO trace_events
                   (id, run_id, sequence, event_type, payload, payload_encrypted, redacted_payload, created_at)
                   VALUES (?, ?, ?, ?, '{}', ?, ?, ?)""",
                (str(uuid.uuid4()), run_id, sequence, event_type, self._encrypt_json(payload),
                 json.dumps(audit_payload(event_type, payload), separators=(",", ":")), event["timestamp"]),
            )
            self.db.commit()
        return event

    def events_after(self, run_id: str, owner_id: int, after_sequence: int) -> Optional[List[Dict[str, Any]]]:
        if self.get_run(run_id, owner_id) is None:
            return None
        with self.lock:
            rows = self.db.execute(
                "SELECT sequence, event_type, payload, payload_encrypted, created_at FROM trace_events WHERE run_id = ? AND sequence > ? ORDER BY sequence ASC",
                (run_id, after_sequence),
            ).fetchall()
        return [
            {"type": row["event_type"], "run_id": run_id, "sequence": row["sequence"], "timestamp": row["created_at"],
             "payload": self._decrypt_json(row["payload_encrypted"]) if row["payload_encrypted"] else json.loads(row["payload"])}
            for row in rows
        ]

    def decrypt_api_key(self, encrypted: bytes) -> str:
        try:
            return self.cipher.decrypt(encrypted).decode("utf-8")
        except InvalidToken as exc:
            raise RuntimeError("无法解密模型密钥，请联系 Agent 创建者") from exc


class EventHub:
    def __init__(self) -> None:
        self.subscribers: Dict[str, List[asyncio.Queue]] = defaultdict(list)
        self.lock = asyncio.Lock()

    async def subscribe(self, run_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        async with self.lock:
            self.subscribers[run_id].append(queue)
        return queue

    async def unsubscribe(self, run_id: str, queue: asyncio.Queue) -> None:
        async with self.lock:
            subscribers = self.subscribers.get(run_id, [])
            if queue in subscribers:
                subscribers.remove(queue)
            if not subscribers:
                self.subscribers.pop(run_id, None)

    async def publish(self, event: Dict[str, Any]) -> None:
        async with self.lock:
            targets = list(self.subscribers.get(event["run_id"], []))
        for queue in targets:
            queue.put_nowait(event)


class TaskEventHub:
    def __init__(self) -> None:
        self.subscribers: Dict[str, List[asyncio.Queue]] = defaultdict(list)
        self.all_subscribers: Dict[int, List[asyncio.Queue]] = defaultdict(list)
        self.lock = asyncio.Lock()

    async def subscribe(self, task_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        async with self.lock:
            self.subscribers[task_id].append(queue)
        return queue

    async def unsubscribe(self, task_id: str, queue: asyncio.Queue) -> None:
        async with self.lock:
            subscribers = self.subscribers.get(task_id, [])
            if queue in subscribers:
                subscribers.remove(queue)
            if not subscribers:
                self.subscribers.pop(task_id, None)

    async def subscribe_all(self, owner_user_id: int) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        async with self.lock:
            self.all_subscribers[owner_user_id].append(queue)
        return queue

    async def unsubscribe_all(self, owner_user_id: int, queue: asyncio.Queue) -> None:
        async with self.lock:
            subscribers = self.all_subscribers.get(owner_user_id, [])
            if queue in subscribers:
                subscribers.remove(queue)
            if not subscribers:
                self.all_subscribers.pop(owner_user_id, None)

    async def publish(self, event: Dict[str, Any]) -> None:
        async with self.lock:
            targets = list(self.subscribers.get(event["task_id"], []))
            owner_user_id = event.get("owner_user_id")
            if owner_user_id is not None:
                targets.extend(self.all_subscribers.get(int(owner_user_id), []))
        for queue in targets:
            queue.put_nowait(event)


store: Optional[AgentStore] = None
hub = EventHub()
task_hub = TaskEventHub()
app = FastAPI(title="Chat Agent API", version="0.1.0")
background_tasks: List[asyncio.Task] = []


@app.on_event("startup")
async def startup() -> None:
    global store
    settings.validate()
    store = AgentStore(settings.database_url or settings.database_path, settings.master_key)
    if settings.redis_url:
        background_tasks.append(asyncio.create_task(outbox_publisher_loop()))
        background_tasks.append(asyncio.create_task(redis_event_relay()))


@app.on_event("shutdown")
async def shutdown() -> None:
    for task in background_tasks:
        task.cancel()
    if background_tasks:
        await asyncio.gather(*background_tasks, return_exceptions=True)
    background_tasks.clear()
    if store is not None:
        store.db.close()


@app.exception_handler(HTTPException)
async def http_exception_handler(_, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})


def database() -> AgentStore:
    if store is None:
        raise RuntimeError("Agent service has not started")
    return store


async def authenticate(token: str) -> Dict[str, Any]:
    if not token:
        raise HTTPException(status_code=401, detail="缺少认证 token")
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                settings.auth_url,
                headers={"Authorization": "Service " + settings.service_secret},
                json={"user_token": token},
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail="聊天认证服务不可用") from exc
    if response.status_code != 200:
        raise HTTPException(status_code=401, detail="认证 token 无效或已过期")
    identity = response.json()
    if not identity.get("active"):
        raise HTTPException(status_code=401, detail="账户不可用")
    return identity


async def authenticated_user(authorization: Optional[str]) -> Dict[str, Any]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少认证 token")
    return await authenticate(authorization[7:].strip())


async def authenticated_service(authorization: Optional[str]) -> None:
    if not authorization or not authorization.startswith("Service "):
        raise HTTPException(status_code=401, detail="缺少服务认证")
    supplied = authorization[8:].strip()
    if not supplied or not secrets.compare_digest(supplied, settings.service_secret):
        raise HTTPException(status_code=401, detail="服务认证无效")


async def qq_gateway_request(method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Proxy a user-authorized QQ connection command to the private gateway."""
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.request(
                method, settings.qq_gateway_url + path,
                json=payload,
                headers={"Authorization": "Service " + settings.service_secret},
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail="QQ Gateway 当前不可用") from exc
    if response.status_code >= 400:
        detail = "QQ Gateway 请求失败"
        try:
            body = response.json()
            detail = body.get("detail", body.get("error", detail)) if isinstance(body, dict) else detail
        except ValueError:
            pass
        raise HTTPException(status_code=response.status_code, detail=detail)
    try:
        body = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="QQ Gateway 返回了无效响应") from exc
    return body if isinstance(body, dict) else {"data": body}


async def authenticated_device(authorization: Optional[str]) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少设备 access token")
    device_id = database().authenticate_device_access_token(authorization[7:].strip())
    if not device_id:
        raise HTTPException(status_code=401, detail="设备 access token 无效或已过期")
    return device_id


class AgentPayload(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=280)
    avatar_url: str = Field(default="", max_length=2048)
    model_display_name: str = Field(default="Default connection", min_length=1, max_length=80)
    base_url: str = Field(min_length=8, max_length=1024)
    api_key: Optional[str] = Field(default=None, max_length=4096)
    model_id: str = Field(min_length=1, max_length=160)
    temperature: float = Field(default=0.4, ge=0, le=2)
    max_tokens: int = Field(default=2048, ge=1, le=32768)
    timeout_seconds: int = Field(default=60, ge=5, le=300)
    system_prompt: str = Field(default="You are a helpful assistant.", min_length=1, max_length=32000)
    run_policy: Dict[str, int] = Field(default_factory=lambda: {"max_tool_calls": 6, "max_concurrent_runs": 2, "daily_token_budget": 0, "monthly_token_budget": 0})
    memory_enabled: bool = False
    memory_retention_days: int = Field(default=30, ge=1, le=3650)
    execution_target: str = Field(default="cloud", pattern="^(cloud|local)$")
    default_device_id: Optional[str] = Field(default=None, max_length=64)
    default_workspace_id: Optional[str] = Field(default=None, max_length=64)
    model_mode: str = Field(default="server_proxy", pattern="^(server_proxy|local_direct)$")


class AgentUpdatePayload(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    description: Optional[str] = Field(default=None, max_length=280)
    avatar_url: Optional[str] = Field(default=None, max_length=2048)
    model_display_name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    base_url: Optional[str] = Field(default=None, min_length=8, max_length=1024)
    api_key: Optional[str] = Field(default=None, max_length=4096)
    model_id: Optional[str] = Field(default=None, min_length=1, max_length=160)
    temperature: Optional[float] = Field(default=None, ge=0, le=2)
    max_tokens: Optional[int] = Field(default=None, ge=1, le=32768)
    timeout_seconds: Optional[int] = Field(default=None, ge=5, le=300)
    system_prompt: Optional[str] = Field(default=None, min_length=1, max_length=32000)
    run_policy: Optional[Dict[str, int]] = None
    memory_enabled: Optional[bool] = None
    memory_retention_days: Optional[int] = Field(default=None, ge=1, le=3650)
    execution_target: Optional[str] = Field(default=None, pattern="^(cloud|local)$")
    default_device_id: Optional[str] = Field(default=None, max_length=64)
    default_workspace_id: Optional[str] = Field(default=None, max_length=64)
    model_mode: Optional[str] = Field(default=None, pattern="^(server_proxy|local_direct)$")


class QQConnectPayload(BaseModel):
    app_id: str = Field(min_length=1, max_length=128)
    client_secret: str = Field(min_length=1, max_length=256)
    bot_id: Optional[str] = Field(default=None, max_length=128)
    intents: int = Field(default=513, ge=1, le=4095)


class LocalDevicePayload(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)
    platform: str = Field(default="", max_length=80)
    cli_version: str = Field(default="", max_length=40)
    capabilities: List[Dict[str, Any]] = Field(default_factory=list, max_length=64)


class PairingApprovalPayload(BaseModel):
    code: str = Field(min_length=9, max_length=9, pattern=r"^LA-[0-9]{6}$")


class PairingClaimPayload(BaseModel):
    pairing_secret: str = Field(min_length=16, max_length=256)


class DeviceTokenRefreshPayload(BaseModel):
    refresh_token: str = Field(min_length=16, max_length=256)


class LocalWorkspacePayload(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)
    policy_version: int = Field(default=1, ge=1, le=1000000)
    capabilities: List[Dict[str, Any]] = Field(default_factory=list, max_length=64)


class LocalModelPayload(BaseModel):
    agent_id: str = Field(min_length=1, max_length=64)
    base_url: str = Field(min_length=8, max_length=1024)
    model_id: str = Field(min_length=1, max_length=160)


class LocalBindPayload(BaseModel):
    device_id: str = Field(min_length=1, max_length=64)
    workspace_id: str = Field(min_length=1, max_length=64)
    model_mode: str = Field(default="server_proxy", pattern="^(server_proxy|local_direct)$")


class ConversationPayload(BaseModel):
    title: str = Field(default="新会话", max_length=120)


class RunPayload(BaseModel):
    content: str = Field(min_length=1, max_length=50000)


class ChannelEventPayload(BaseModel):
    provider: str = Field(min_length=1, max_length=32)
    bot_id: str = Field(min_length=1, max_length=128)
    event_id: str = Field(min_length=1, max_length=256)
    event_type: str = Field(min_length=1, max_length=80)
    scope_type: str = Field(min_length=1, max_length=32)
    scope_id: str = Field(min_length=1, max_length=256)
    sender_id: str = Field(default="", max_length=256)
    content: str = Field(min_length=1, max_length=50000)
    agent_id: str = Field(min_length=1, max_length=128)
    owner_user_id: int = Field(ge=1)
    conversation_id: Optional[str] = Field(default=None, max_length=128)
    title: str = Field(default="Channel conversation", max_length=120)


class TaskPayload(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    goal: str = Field(min_length=1, max_length=50000)
    assigned_agent_id: Optional[str] = Field(default=None, max_length=64)
    budget_snapshot: Dict[str, int] = Field(default_factory=dict)
    idempotency_key: Optional[str] = Field(default=None, min_length=1, max_length=128)


class TaskResultPayload(BaseModel):
    result_summary: str = Field(min_length=1, max_length=50000)
    idempotency_key: Optional[str] = Field(default=None, min_length=1, max_length=128)


class TaskAssignmentPayload(BaseModel):
    agent_id: str = Field(min_length=1, max_length=64)
    idempotency_key: Optional[str] = Field(default=None, min_length=1, max_length=128)


class ToolPayload(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    description: str = Field(default="", max_length=2000)
    kind: str = Field(default="http", pattern="^(http|openapi|mcp|mcp_stdio|local)$")
    config: Dict[str, Any]
    input_schema: Dict[str, Any]
    confirmation_mode: str = Field(default="none", pattern="^(none|per_run|per_call)$")
    side_effect: str = Field(default="read", pattern="^(read|write|destructive)$")
    rate_limit_per_run: int = Field(default=6, ge=1, le=100)


class ToolAssignmentPayload(BaseModel):
    tool_ids: List[str] = Field(default_factory=list, max_length=32)


class OpenAPIImportPayload(BaseModel):
    document: Optional[Dict[str, Any]] = None
    document_url: Optional[str] = Field(default=None, max_length=2048)
    base_url: Optional[str] = Field(default=None, max_length=2048)


class MCPDiscoverPayload(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
    headers: Dict[str, str] = Field(default_factory=dict)


class MCPStdioDiscoverPayload(BaseModel):
    command: str = Field(min_length=1, max_length=256)
    args: List[str] = Field(default_factory=list, max_length=32)
    env: Dict[str, str] = Field(default_factory=dict)


class ConfirmationDecisionPayload(BaseModel):
    approve: bool
    arguments_hash: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")
    reason: str = Field(default="", max_length=500)


class MemoryPayload(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    kind: str = Field(default="fact", pattern="^(preference|profile|constraint|fact|experience)$")
    importance: int = Field(default=50, ge=0, le=100)
    expires_at: Optional[str] = Field(default=None, max_length=64)


def validate_base_url(value: str) -> None:
    if not settings.allow_http and not value.lower().startswith("https://"):
        raise HTTPException(status_code=400, detail="模型 Base URL 必须使用 HTTPS")
    if settings.allow_http and not value.lower().startswith(("https://", "http://")):
        raise HTTPException(status_code=400, detail="模型 Base URL 格式无效")


async def validate_tool_payload(payload: ToolPayload) -> None:
    schema = require_object_schema(payload.input_schema)
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="工具输入 Schema 无效") from exc
    method = str(payload.config.get("method", "GET")).upper()
    url = str(payload.config.get("url", ""))
    if payload.kind == "mcp_stdio":
        if not isinstance(payload.config.get("command"), str) or not payload.config.get("command", "").strip():
            raise HTTPException(status_code=400, detail="MCP STDIO 缺少 command")
        if not str(payload.config.get("remote_tool_name", "")).strip():
            raise HTTPException(status_code=400, detail="MCP STDIO 工具缺少 remote_tool_name")
        if not isinstance(payload.config.get("args", []), list) or any(not isinstance(item, str) for item in payload.config.get("args", [])):
            raise HTTPException(status_code=400, detail="MCP STDIO args 无效")
    elif payload.kind == "local":
        if not payload.config.get("builtin") and (not isinstance(payload.config.get("command"), str) or not payload.config.get("command", "").strip()):
            raise HTTPException(status_code=400, detail="本地工具缺少 command")
    elif payload.kind == "mcp":
        if not str(payload.config.get("remote_tool_name", "")).strip():
            raise HTTPException(status_code=400, detail="MCP 工具缺少 remote_tool_name")
    elif method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}:
        raise HTTPException(status_code=400, detail="HTTP 工具方法不受支持")
    expected = "read" if (method in {"GET", "HEAD"} and payload.kind not in {"mcp", "mcp_stdio", "local"}) or payload.side_effect == "read" else "write"
    if payload.side_effect == "read" and expected != "read":
        raise HTTPException(status_code=400, detail="非只读 HTTP 操作不能标记为 read")
    if payload.side_effect == "destructive" and payload.confirmation_mode != "per_call":
        raise HTTPException(status_code=400, detail="破坏性工具必须逐次确认")
    if payload.side_effect == "write" and payload.confirmation_mode == "none":
        raise HTTPException(status_code=400, detail="写工具必须要求确认")
    if payload.kind in {"http", "openapi", "mcp"}:
        if not url or len(url) > 2048:
            raise HTTPException(status_code=400, detail="工具 URL 无效")
        await assert_safe_public_url(url, settings.allow_http)


def validate_run_policy(policy: Dict[str, int]) -> Dict[str, int]:
    defaults = {
        "max_tool_calls": 6, "max_concurrent_runs": 2,
        "daily_token_budget": 0, "monthly_token_budget": 0, "context_window": 32768,
    }
    merged = {**defaults, **policy}
    bounds = {
        "max_tool_calls": (0, 20), "max_concurrent_runs": (1, 10),
        "daily_token_budget": (0, 100_000_000), "monthly_token_budget": (0, 1_000_000_000),
        "context_window": (2048, 2_000_000),
    }
    if set(merged) - set(bounds):
        raise HTTPException(status_code=400, detail="运行策略包含未知字段")
    for key, (minimum, maximum) in bounds.items():
        if isinstance(merged[key], bool) or not isinstance(merged[key], int) or not minimum <= merged[key] <= maximum:
            raise HTTPException(status_code=400, detail="运行策略字段无效：" + key)
    return merged


@app.get("/healthz")
async def healthz() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/internal/v1/channel-events")
async def create_channel_event(raw_payload: Dict[str, Any] = Body(...), authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    """Create a Run for a trusted channel gateway without exposing user auth."""
    await authenticated_service(authorization)
    try:
        payload = ChannelEventPayload.model_validate(raw_payload)
    except Exception as exc:
        raise HTTPException(status_code=422, detail="Channel Event payload 无效") from exc
    if payload.provider != "qq":
        raise HTTPException(status_code=400, detail="暂不支持该 Channel Provider")
    existing = database().channel_event_result(payload.provider, payload.bot_id, payload.event_id)
    if existing is not None:
        return {"provider": payload.provider, "event_id": payload.event_id, "conversation_id": existing["conversation_id"], "run_id": existing["run_id"], "duplicate": True}
    agent = database().get_agent(payload.agent_id, payload.owner_user_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Channel 绑定的 Agent 未找到")
    conversation = None
    if payload.conversation_id:
        conversation = database().get_conversation(payload.conversation_id, payload.owner_user_id)
        if conversation is None or conversation["agent_id"] != payload.agent_id:
            raise HTTPException(status_code=409, detail="Channel 会话映射无效")
    else:
        conversation = database().create_conversation(payload.agent_id, payload.owner_user_id, payload.title)
    if conversation is None:
        raise HTTPException(status_code=404, detail="无法创建 Channel 会话")
    run = database().create_run(conversation["id"], payload.owner_user_id, payload.content.strip())
    if run is None:
        raise HTTPException(status_code=404, detail="Channel 会话未找到")
    if run.get("error"):
        raise HTTPException(status_code=409, detail=run["error"])
    try:
        database().remember_channel_event(
            payload.provider, payload.bot_id, payload.event_id, conversation["id"], run["id"], payload.owner_user_id,
        )
    except database().db.integrity_error:
        existing = database().channel_event_result(payload.provider, payload.bot_id, payload.event_id)
        if existing is None:
            raise
        return {"provider": payload.provider, "event_id": payload.event_id, "conversation_id": existing["conversation_id"], "run_id": existing["run_id"], "duplicate": True}
    if not run.get("local_dispatch"):
        await enqueue_run(run["id"])
    return {"provider": payload.provider, "event_id": payload.event_id, "conversation_id": conversation["id"], "run_id": run["id"]}


@app.get("/internal/v1/channel-runs/{run_id}")
async def get_channel_run(run_id: str, owner_user_id: int, authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    await authenticated_service(authorization)
    run = database().get_run(run_id, owner_user_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Channel Run 未找到")
    return {"id": run["id"], "state": run["state"], "final_content": run.get("final_content", ""), "error_message": run.get("error_message", "")}


@app.post("/api/v1/tools")
async def create_tool(payload: ToolPayload, authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    await validate_tool_payload(payload)
    user = await authenticated_user(authorization)
    return database().create_tool(user["user_id"], payload.model_dump())


@app.get("/api/v1/tools")
async def list_tools(authorization: Optional[str] = Header(None)) -> List[Dict[str, Any]]:
    user = await authenticated_user(authorization)
    return database().list_tools(user["user_id"])


@app.post("/api/v1/tools/openapi/import")
async def import_openapi(payload: OpenAPIImportPayload, authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    await authenticated_user(authorization)
    document = payload.document
    if document is None:
        if not payload.document_url:
            raise HTTPException(status_code=400, detail="请提供 OpenAPI 文档或 URL")
        await assert_safe_public_url(payload.document_url, settings.allow_http)
        try:
            async with httpx.AsyncClient(
                timeout=10, follow_redirects=False, transport=safe_http_transport(), trust_env=False,
            ) as client:
                response = await client.get(payload.document_url, headers={"Accept": "application/json, application/yaml"})
                response.raise_for_status()
                if len(response.content) > 1024 * 1024:
                    raise HTTPException(status_code=400, detail="OpenAPI 文档超过 1 MiB")
                document = response.json()
        except HTTPException:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="无法读取 OpenAPI 文档") from exc
    candidates = openapi_operations(document)
    for candidate in candidates:
        candidate["side_effect"] = "write" if candidate.pop("has_side_effect") else "read"
        candidate["confirmation_mode"] = "per_call" if candidate["side_effect"] != "read" else "none"
    return {"candidates": candidates, "base_url": payload.base_url or str(document.get("servers", [{}])[0].get("url", ""))}


@app.post("/api/v1/tools/mcp/discover")
async def discover_mcp(payload: MCPDiscoverPayload, authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    """Discover remote MCP tools as candidates. Discovery never authorizes a tool."""
    await authenticated_user(authorization)
    await assert_safe_public_url(payload.url, settings.allow_http)
    headers = {str(key): str(value) for key, value in payload.headers.items()}
    headers.setdefault("Content-Type", "application/json")
    headers.setdefault("Accept", "application/json, text/event-stream")
    request = {"jsonrpc": "2.0", "id": "agent-discovery", "method": "tools/list", "params": {}}
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False, transport=safe_http_transport(), trust_env=False) as client:
            response = await client.post(payload.url, headers=headers, json=request)
            assert_public_peer(response)
            if 300 <= response.status_code < 400:
                raise HTTPException(status_code=400, detail="MCP 不允许重定向")
            response.raise_for_status()
            data = response.json()
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="无法发现远程 MCP 工具") from exc
    items = (data.get("result") or {}).get("tools")
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail="MCP tools/list 响应无效")
    candidates = []
    for item in items[:100]:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        schema = item.get("inputSchema") or {"type": "object", "properties": {}}
        try:
            require_object_schema(schema)
            Draft202012Validator.check_schema(schema)
        except Exception:
            continue
        candidates.append({
            "name": item["name"][:64], "description": str(item.get("description", ""))[:2000],
            "input_schema": schema, "kind": "mcp", "side_effect": "read", "confirmation_mode": "none",
            "config": {"url": payload.url, "headers": redact(headers), "remote_tool_name": item["name"]},
        })
    return {"candidates": candidates, "provider": "mcp-streamable-http-v1"}


@app.post("/api/v1/tools/mcp/discover-stdio")
async def discover_mcp_stdio_endpoint(payload: MCPStdioDiscoverPayload, authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    await authenticated_user(authorization)
    try:
        items = await discover_mcp_stdio(payload.model_dump(), timeout=15.0)
    except (OSError, RuntimeError, ValueError, asyncio.TimeoutError) as exc:
        raise HTTPException(status_code=400, detail="无法发现本地 MCP 工具：" + str(exc)[:300]) from exc
    candidates = []
    for item in items[:100]:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        schema = item.get("inputSchema") or {"type": "object", "properties": {}}
        try:
            require_object_schema(schema)
            Draft202012Validator.check_schema(schema)
        except Exception:
            continue
        candidates.append({
            "name": item["name"][:64], "description": str(item.get("description", ""))[:2000],
            "input_schema": schema, "kind": "mcp_stdio", "side_effect": "read", "confirmation_mode": "none",
            "config": {"command": payload.command, "args": payload.args, "env": payload.env, "remote_tool_name": item["name"]},
        })
    return {"candidates": candidates, "provider": "mcp-stdio-v1"}


@app.post("/api/v1/tools/{tool_id}/validate")
async def validate_tool(tool_id: str, authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    user = await authenticated_user(authorization)
    tool = database().get_tool(tool_id, user["user_id"], include_config=True)
    if tool is None:
        raise HTTPException(status_code=404, detail="工具未找到")
    if tool["kind"] in {"http", "openapi", "mcp"}:
        await assert_safe_public_url(str(tool["config"].get("url", "")), settings.allow_http)
    return {"valid": True, "summary": "URL、Schema 和确认策略校验通过；未执行可能有副作用的请求。"}


@app.post("/api/v1/agents")
async def create_agent(payload: AgentPayload, authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    validate_base_url(payload.base_url)
    user = await authenticated_user(authorization)
    data = payload.model_dump()
    direct = data["execution_target"] == "local" and data["model_mode"] == "local_direct"
    if direct and data.get("api_key"):
        raise HTTPException(status_code=400, detail="local_direct 的模型密钥只能由本机 daemon 保存")
    if not direct and not data.get("api_key"):
        raise HTTPException(status_code=400, detail="server_proxy 需要模型密钥")
    data["run_policy"] = validate_run_policy(data["run_policy"])
    return database().create_agent(user["user_id"], data)


@app.get("/api/v1/agents")
async def list_agents(authorization: Optional[str] = Header(None)) -> List[Dict[str, Any]]:
    user = await authenticated_user(authorization)
    return database().list_agents(user["user_id"])


@app.post("/api/v1/local-agent/devices")
async def create_local_device(payload: LocalDevicePayload, authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    user = await authenticated_user(authorization)
    return database().create_local_device(user["user_id"], payload.model_dump())


@app.post("/api/v1/local-agent/pairings")
async def start_local_pairing(payload: LocalDevicePayload) -> Dict[str, Any]:
    return database().start_pairing(payload.model_dump())


@app.post("/api/v1/local-agent/pairings/{pairing_id}/approve")
async def approve_local_pairing(pairing_id: str, payload: PairingApprovalPayload, authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    user = await authenticated_user(authorization)
    device = database().approve_pairing(pairing_id, payload.code, user["user_id"])
    if device is None: raise HTTPException(status_code=404, detail="配对码无效、已过期或已处理")
    return device


@app.post("/api/v1/local-agent/pairings/{pairing_id}/claim")
async def claim_local_pairing(pairing_id: str, payload: PairingClaimPayload) -> Dict[str, Any]:
    result = database().claim_pairing(pairing_id, payload.pairing_secret)
    if result is None: raise HTTPException(status_code=404, detail="配对会话无效、已过期或已领取")
    return result


@app.post("/api/v1/local-agent/token/refresh")
async def refresh_local_device_token(payload: DeviceTokenRefreshPayload) -> Dict[str, Any]:
    result = database().issue_device_access_token(payload.refresh_token)
    if result is None: raise HTTPException(status_code=401, detail="设备凭据无效或已撤销")
    return result


@app.get("/api/v1/local-agent/devices")
async def list_local_devices(authorization: Optional[str] = Header(None)) -> List[Dict[str, Any]]:
    user = await authenticated_user(authorization)
    return database().list_local_devices(user["user_id"])


@app.delete("/api/v1/local-agent/devices/{device_id}")
async def revoke_local_device(device_id: str, authorization: Optional[str] = Header(None)) -> Dict[str, bool]:
    user = await authenticated_user(authorization)
    if not database().revoke_local_device(device_id, user["user_id"]):
        raise HTTPException(status_code=404, detail="本地设备未找到")
    return {"revoked": True}


@app.post("/api/v1/local-agent/devices/{device_id}/workspaces")
async def add_local_workspace(device_id: str, payload: LocalWorkspacePayload, authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    user = await authenticated_user(authorization)
    workspace = database().add_local_workspace(user["user_id"], device_id, payload.model_dump())
    if workspace is None:
        raise HTTPException(status_code=404, detail="本地设备未找到")
    return workspace


@app.post("/api/v1/local-agent/workspaces")
async def register_local_workspace(payload: LocalWorkspacePayload, authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    device_id = await authenticated_device(authorization)
    workspace = database().register_device_workspace(device_id, payload.model_dump())
    if workspace is None:
        raise HTTPException(status_code=401, detail="设备已撤销")
    return workspace


@app.post("/api/v1/local-agent/models")
async def register_local_model(payload: LocalModelPayload, authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    validate_base_url(payload.base_url)
    device_id = await authenticated_device(authorization)
    model = database().register_local_model(device_id, payload.model_dump())
    if model is None:
        raise HTTPException(status_code=404, detail="Agent 未找到或设备已撤销")
    return model


@app.delete("/api/v1/local-agent/models/{agent_id}")
async def remove_local_model(agent_id: str, authorization: Optional[str] = Header(None)) -> Dict[str, bool]:
    device_id = await authenticated_device(authorization)
    return {"removed": database().remove_local_model(agent_id, device_id)}


@app.get("/api/v1/local-agent/devices/{device_id}/workspaces")
async def list_local_workspaces(device_id: str, authorization: Optional[str] = Header(None)) -> List[Dict[str, Any]]:
    user = await authenticated_user(authorization)
    workspaces = database().list_local_workspaces(device_id, user["user_id"])
    if workspaces is None:
        raise HTTPException(status_code=404, detail="本地设备未找到")
    return workspaces


@app.get("/api/v1/agents/{agent_id}")
async def get_agent(agent_id: str, authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    user = await authenticated_user(authorization)
    agent = database().get_agent(agent_id, user["user_id"], include_private=True)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent 未找到")
    return agent


@app.get("/api/v1/agents/{agent_id}/qq")
async def get_agent_qq_connection(agent_id: str, authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    user = await authenticated_user(authorization)
    if database().get_agent(agent_id, user["user_id"]) is None:
        raise HTTPException(status_code=404, detail="Agent 未找到")
    try:
        return await qq_gateway_request("GET", "/internal/v1/qq/connections/" + agent_id)
    except HTTPException as exc:
        if exc.status_code in {502, 503}:
            return {"agent_id": agent_id, "status": "gateway_unavailable", "configured": False}
        raise


@app.post("/api/v1/agents/{agent_id}/qq")
async def connect_agent_qq(agent_id: str, payload: QQConnectPayload, authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    user = await authenticated_user(authorization)
    if database().get_agent(agent_id, user["user_id"]) is None:
        raise HTTPException(status_code=404, detail="Agent 未找到")
    command = {
        "agent_id": agent_id,
        "owner_user_id": int(user["user_id"]),
        "app_id": payload.app_id.strip(),
        "client_secret": payload.client_secret,
        "bot_id": (payload.bot_id or "").strip(),
        "intents": payload.intents,
    }
    return await qq_gateway_request("POST", "/internal/v1/qq/connections", command)


@app.delete("/api/v1/agents/{agent_id}/qq")
async def disconnect_agent_qq(agent_id: str, authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    user = await authenticated_user(authorization)
    if database().get_agent(agent_id, user["user_id"]) is None:
        raise HTTPException(status_code=404, detail="Agent 未找到")
    return await qq_gateway_request("DELETE", "/internal/v1/qq/connections/" + agent_id)


@app.post("/api/v1/agents/{agent_id}/local-bind")
async def bind_local_agent(agent_id: str, payload: LocalBindPayload, authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    user = await authenticated_user(authorization)
    try:
        agent = database().bind_local_agent(agent_id, user["user_id"], payload.device_id, payload.workspace_id, payload.model_mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent 未找到")
    return agent


@app.get("/api/v1/agents/{agent_id}/tools")
async def list_agent_tools(agent_id: str, authorization: Optional[str] = Header(None)) -> List[Dict[str, Any]]:
    user = await authenticated_user(authorization)
    if database().get_agent(agent_id, user["user_id"]) is None:
        raise HTTPException(status_code=404, detail="Agent 未找到")
    return database().assigned_tools(agent_id, user["user_id"])


@app.put("/api/v1/agents/{agent_id}/tools")
async def assign_agent_tools(agent_id: str, payload: ToolAssignmentPayload, authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    user = await authenticated_user(authorization)
    agent = database().set_agent_tools(agent_id, user["user_id"], payload.tool_ids)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent 未找到")
    return agent


@app.put("/api/v1/agents/{agent_id}")
async def update_agent(agent_id: str, payload: AgentUpdatePayload, authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    changes = payload.model_dump(exclude_none=True)
    if "base_url" in changes:
        validate_base_url(changes["base_url"])
    if "run_policy" in changes:
        changes["run_policy"] = validate_run_policy(changes["run_policy"])
    user = await authenticated_user(authorization)
    try:
        agent = database().update_agent(agent_id, user["user_id"], changes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent 未找到")
    return agent


@app.post("/api/v1/agents/{agent_id}/pause")
async def pause_agent(agent_id: str, authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    user = await authenticated_user(authorization)
    agent = database().set_agent_state(agent_id, user["user_id"], "paused")
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent 未找到")
    return agent


@app.post("/api/v1/agents/{agent_id}/resume")
async def resume_agent(agent_id: str, authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    user = await authenticated_user(authorization)
    agent = database().set_agent_state(agent_id, user["user_id"], "active")
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent 未找到")
    return agent


@app.delete("/api/v1/agents/{agent_id}")
async def archive_agent(agent_id: str, authorization: Optional[str] = Header(None)) -> Dict[str, bool]:
    user = await authenticated_user(authorization)
    if database().set_agent_state(agent_id, user["user_id"], "archived") is None:
        raise HTTPException(status_code=404, detail="Agent 未找到")
    return {"archived": True}


@app.get("/api/v1/agents/{agent_id}/conversations")
async def list_conversations(agent_id: str, authorization: Optional[str] = Header(None)) -> List[Dict[str, Any]]:
    user = await authenticated_user(authorization)
    return database().list_conversations(agent_id, user["user_id"])


@app.post("/api/v1/agents/{agent_id}/conversations")
async def create_conversation(agent_id: str, payload: ConversationPayload, authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    user = await authenticated_user(authorization)
    conversation = database().create_conversation(agent_id, user["user_id"], payload.title)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Agent 未找到")
    return conversation


@app.get("/api/v1/agent-conversations/{conversation_id}")
async def get_conversation(conversation_id: str, authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    user = await authenticated_user(authorization)
    data = database().conversation_messages(conversation_id, user["user_id"])
    if data is None:
        raise HTTPException(status_code=404, detail="会话未找到")
    return data


@app.post("/api/v1/agent-conversations/{conversation_id}/runs")
async def create_run(conversation_id: str, payload: RunPayload, authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    user = await authenticated_user(authorization)
    run = database().create_run(conversation_id, user["user_id"], payload.content.strip())
    if run is None:
        raise HTTPException(status_code=404, detail="会话未找到")
    if run.get("error"):
        raise HTTPException(status_code=409, detail=run["error"])
    if not run.get("local_dispatch"):
        await enqueue_run(run["id"])
    return run


@app.post("/api/v1/tasks")
async def create_task(payload: TaskPayload, authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    user = await authenticated_user(authorization)
    try:
        task = database().create_task(user["user_id"], payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    run_id = task.get("run_id")
    if run_id:
        run = database().get_run(run_id, user["user_id"])
        if run is not None and not run.get("local_dispatch"):
            await enqueue_run(run_id)
    return task


@app.get("/api/v1/tasks")
async def list_tasks(state: Optional[str] = Query(None), limit: int = Query(100, ge=1, le=200),
                     authorization: Optional[str] = Header(None)) -> List[Dict[str, Any]]:
    user = await authenticated_user(authorization)
    return database().list_tasks(user["user_id"], state=state, limit=limit)


@app.get("/api/v1/tasks/{task_id}")
async def get_task(task_id: str, authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    user = await authenticated_user(authorization)
    task = database().get_task(task_id, user["user_id"])
    if task is None:
        raise HTTPException(status_code=404, detail="任务未找到")
    return task


@app.get("/api/v1/tasks/{task_id}/context")
async def get_task_context(task_id: str, after_sequence: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=500),
                           authorization: Optional[str] = Header(None)) -> List[Dict[str, Any]]:
    user = await authenticated_user(authorization)
    events = database().task_context_events(task_id, user["user_id"], after_sequence=after_sequence, limit=limit)
    if events is None:
        raise HTTPException(status_code=404, detail="任务未找到")
    return events


@app.get("/api/v1/tasks/{task_id}/assignments")
async def get_task_assignments(task_id: str, authorization: Optional[str] = Header(None)) -> List[Dict[str, Any]]:
    user = await authenticated_user(authorization)
    assignments = database().task_assignments(task_id, user["user_id"])
    if assignments is None:
        raise HTTPException(status_code=404, detail="任务未找到")
    return assignments


@app.get("/api/v1/tasks/{task_id}/results")
async def get_task_results(task_id: str, authorization: Optional[str] = Header(None)) -> List[Dict[str, Any]]:
    user = await authenticated_user(authorization)
    results = database().task_results(task_id, user["user_id"])
    if results is None:
        raise HTTPException(status_code=404, detail="任务未找到")
    return results


@app.get("/api/v1/tasks/{task_id}/runs")
async def get_task_runs(task_id: str, limit: int = Query(100, ge=1, le=200),
                        authorization: Optional[str] = Header(None)) -> List[Dict[str, Any]]:
    user = await authenticated_user(authorization)
    runs = database().task_runs(task_id, user["user_id"], limit=limit)
    if runs is None:
        raise HTTPException(status_code=404, detail="任务未找到")
    return runs


@app.get("/api/v1/tasks/{task_id}/confirmations")
async def get_task_confirmations(task_id: str, authorization: Optional[str] = Header(None)) -> List[Dict[str, Any]]:
    user = await authenticated_user(authorization)
    confirmations = database().task_confirmations(task_id, user["user_id"])
    if confirmations is None:
        raise HTTPException(status_code=404, detail="任务未找到")
    return confirmations


@app.get("/api/v1/task-dispatch-events")
async def get_task_dispatch_events(task_id: Optional[str] = Query(None), after_sequence: int = Query(0, ge=0),
                                   limit: int = Query(200, ge=1, le=500), authorization: Optional[str] = Header(None)) -> List[Dict[str, Any]]:
    user = await authenticated_user(authorization)
    return database().task_dispatch_events(user["user_id"], task_id=task_id, after_sequence=after_sequence, limit=limit)


@app.post("/api/v1/tasks/{task_id}/assignments")
async def assign_task(task_id: str, payload: TaskAssignmentPayload,
                      expected_state_version: Optional[int] = Header(None, alias="X-Task-State-Version"),
                      authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    user = await authenticated_user(authorization)
    try:
        task = database().assign_cloud_task(
            task_id, user["user_id"], payload.agent_id, payload.idempotency_key, expected_state_version,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if task is None:
        raise HTTPException(status_code=404, detail="任务未找到")
    if task.get("run_id"):
        await enqueue_run(task["run_id"])
    return task


@app.post("/api/v1/tasks/{task_id}/start")
async def start_task(task_id: str, idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
                     expected_state_version: Optional[int] = Header(None, alias="X-Task-State-Version"),
                     authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    user = await authenticated_user(authorization)
    try:
        task = database().transition_task(
            task_id, user["user_id"], "in_progress", "任务开始执行", idempotency_key=idempotency_key,
            expected_state_version=expected_state_version,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if task is None:
        raise HTTPException(status_code=404, detail="任务未找到")
    return task


@app.post("/api/v1/tasks/{task_id}/submit-result")
async def submit_task_result(task_id: str, payload: TaskResultPayload,
                             expected_state_version: Optional[int] = Header(None, alias="X-Task-State-Version"),
                             authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    user = await authenticated_user(authorization)
    if database().get_task(task_id, user["user_id"]) is None:
        raise HTTPException(status_code=404, detail="任务未找到")
    raise HTTPException(status_code=403, detail="结果只能由当前执行者通过 submit_result 工具提交")


@app.post("/api/v1/tasks/{task_id}/close")
async def close_task(task_id: str, payload: TaskResultPayload,
                     expected_state_version: Optional[int] = Header(None, alias="X-Task-State-Version"),
                     authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    user = await authenticated_user(authorization)
    try:
        task = database().transition_task(
            task_id, user["user_id"], "closed", "任务已由提出者收尾", payload.result_summary.strip(),
            payload.idempotency_key, expected_state_version,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if task is None:
        raise HTTPException(status_code=404, detail="任务未找到")
    return task


@app.post("/api/v1/tasks/{task_id}/reopen")
async def reopen_task(task_id: str, idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
                      expected_state_version: Optional[int] = Header(None, alias="X-Task-State-Version"),
                      authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    user = await authenticated_user(authorization)
    task = database().get_task(task_id, user["user_id"])
    if task is None:
        raise HTTPException(status_code=404, detail="任务未找到")
    target = "in_progress" if task["state"] == "awaiting_proposer_close" else "queued"
    try:
        result = database().transition_task(
            task_id, user["user_id"], target, "提出者要求继续处理任务", idempotency_key=idempotency_key,
            expected_state_version=expected_state_version,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return result  # type: ignore[return-value]


@app.post("/api/v1/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
                      expected_state_version: Optional[int] = Header(None, alias="X-Task-State-Version"),
                      authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    user = await authenticated_user(authorization)
    try:
        task = database().transition_task(
            task_id, user["user_id"], "cancelled", idempotency_key=idempotency_key,
            summary="任务已由提出者取消", expected_state_version=expected_state_version,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if task is None:
        raise HTTPException(status_code=404, detail="任务未找到")
    return task


@app.get("/api/v1/notifications")
async def list_notifications(unread_only: bool = Query(False), limit: int = Query(100, ge=1, le=200),
                             authorization: Optional[str] = Header(None)) -> List[Dict[str, Any]]:
    user = await authenticated_user(authorization)
    return database().list_notifications(user["user_id"], unread_only=unread_only, limit=limit)


@app.post("/api/v1/notifications/{notification_id}/read")
async def mark_notification_read(notification_id: str, authorization: Optional[str] = Header(None)) -> Dict[str, bool]:
    user = await authenticated_user(authorization)
    if not database().mark_notification_read(notification_id, user["user_id"]):
        raise HTTPException(status_code=404, detail="通知未找到")
    return {"read": True}


@app.post("/api/v1/agent-runs/{run_id}/cancel")
async def cancel_run(run_id: str, authorization: Optional[str] = Header(None)) -> Dict[str, bool]:
    user = await authenticated_user(authorization)
    run = database().get_run(run_id, user["user_id"])
    if run is None:
        raise HTTPException(status_code=404, detail="运行未找到")
    if run["state"] in {"completed", "failed", "cancelled"}:
        if run["state"] == "cancelled":
            return {"cancelled": True}
        raise HTTPException(status_code=409, detail="运行已经结束")
    database().update_run(run_id, "cancelled")
    database().sync_task_run_state(run_id, "cancelled", error="运行已由用户取消")
    await emit(run_id, "agent.run.cancelled", {"summary": "运行已取消"})
    return {"cancelled": True}


@app.get("/api/v1/agent-runs/{run_id}")
async def get_run(run_id: str, authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    user = await authenticated_user(authorization)
    run = database().get_run(run_id, user["user_id"])
    if run is None:
        raise HTTPException(status_code=404, detail="运行未找到")
    return run


@app.get("/api/v1/agent-runs")
async def list_runs(agent_id: Optional[str] = Query(None), state: Optional[str] = Query(None), limit: int = Query(100, ge=1, le=200),
                    authorization: Optional[str] = Header(None)) -> List[Dict[str, Any]]:
    user = await authenticated_user(authorization)
    return database().list_runs(user["user_id"], agent_id=agent_id, state=state, limit=limit)


@app.get("/api/v1/evaluations/runs")
async def list_evaluation_runs(authorization: Optional[str] = Header(None)) -> List[Dict[str, Any]]:
    await authenticated_user(authorization)
    return database().list_evaluation_runs()


@app.get("/api/v1/evaluations/runs/{evaluation_run_id}")
async def get_evaluation_run(evaluation_run_id: str, authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    await authenticated_user(authorization)
    result = database().evaluation_run(evaluation_run_id)
    if result is None:
        raise HTTPException(status_code=404, detail="评估运行未找到")
    return result


@app.get("/api/v1/evaluations/compare")
async def compare_evaluation_runs(baseline_id: str = Query(...), candidate_id: str = Query(...),
                                  authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    await authenticated_user(authorization)
    result = database().compare_evaluation_runs(baseline_id, candidate_id)
    if result is None:
        raise HTTPException(status_code=404, detail="评估运行未找到")
    return result


@app.get("/api/v1/agent-runs/{run_id}/confirmations")
async def run_confirmations(run_id: str, authorization: Optional[str] = Header(None)) -> List[Dict[str, Any]]:
    user = await authenticated_user(authorization)
    items = database().list_confirmations(run_id, user["user_id"])
    if items is None:
        raise HTTPException(status_code=404, detail="运行未找到")
    return items


@app.post("/api/v1/agent-runs/{run_id}/confirmations/{confirmation_id}")
async def decide_tool_confirmation(run_id: str, confirmation_id: str, payload: ConfirmationDecisionPayload,
                                   authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    user = await authenticated_user(authorization)
    try:
        confirmation = database().decide_confirmation(
            confirmation_id, run_id, user["user_id"], payload.arguments_hash, payload.approve, payload.reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if confirmation is None:
        raise HTTPException(status_code=404, detail="确认请求未找到")
    if payload.approve:
        await enqueue_confirmation(run_id)
    else:
        database().update_run(run_id, "cancelled", error_message="用户拒绝工具确认")
        await emit(run_id, "agent.tool.rejected", {"tool": confirmation["tool_name"], "summary": "用户拒绝了工具操作"})
    return confirmation


@app.get("/api/v1/agents/{agent_id}/memories")
async def list_memories(agent_id: str, authorization: Optional[str] = Header(None)) -> List[Dict[str, Any]]:
    user = await authenticated_user(authorization)
    memories = database().list_memories(agent_id, user["user_id"])
    if memories is None:
        raise HTTPException(status_code=404, detail="Agent 未找到")
    return memories


@app.post("/api/v1/agents/{agent_id}/memories")
async def create_memory(agent_id: str, payload: MemoryPayload, authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    user = await authenticated_user(authorization)
    try:
        memory = database().create_memory(agent_id, user["user_id"], payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if memory is None:
        raise HTTPException(status_code=404, detail="Agent 未找到")
    return memory


@app.delete("/api/v1/agents/{agent_id}/memories/{memory_id}")
async def delete_memory(agent_id: str, memory_id: str, authorization: Optional[str] = Header(None)) -> Dict[str, bool]:
    user = await authenticated_user(authorization)
    if not database().delete_memory(memory_id, agent_id, user["user_id"]):
        raise HTTPException(status_code=404, detail="记忆未找到")
    return {"deleted": True}


@app.post("/api/v1/agent-conversations/{conversation_id}/clear-context")
async def clear_context(conversation_id: str, authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    user = await authenticated_user(authorization)
    conversation = database().clear_context(conversation_id, user["user_id"])
    if conversation is None:
        raise HTTPException(status_code=404, detail="会话未找到")
    return conversation


@app.delete("/api/v1/agent-conversations/{conversation_id}")
async def delete_conversation(conversation_id: str, authorization: Optional[str] = Header(None)) -> Dict[str, bool]:
    user = await authenticated_user(authorization)
    if not database().delete_conversation(conversation_id, user["user_id"]):
        raise HTTPException(status_code=404, detail="会话未找到")
    return {"deleted": True}


@app.get("/api/v1/agent-runs/{run_id}/trace")
async def run_trace(run_id: str, after_sequence: int = Query(0, ge=0), authorization: Optional[str] = Header(None)) -> List[Dict[str, Any]]:
    user = await authenticated_user(authorization)
    events = database().events_after(run_id, user["user_id"], after_sequence)
    if events is None:
        raise HTTPException(status_code=404, detail="运行未找到")
    return events


async def emit(run_id: str, event_type: str, payload: Dict[str, Any]) -> None:
    event = database().add_event(run_id, event_type, payload)
    await publish_event(event)


async def publish_event(event: Dict[str, Any]) -> None:
    if not settings.redis_url:
        await hub.publish(event)
        return
    from redis.asyncio import Redis
    client = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        try:
            await client.publish("agent-events", json.dumps(event, separators=(",", ":")))
        except Exception:
            # Replay comes from PostgreSQL; Pub/Sub is only the low-latency path.
            pass
    finally:
        await client.aclose()


async def execute_tool(tool: Dict[str, Any], arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Dispatch every provider through one policy-controlled runtime boundary."""
    kind = tool.get("kind", "http")
    if kind == "mcp":
        return await execute_mcp_tool(tool, arguments, settings.allow_http, settings.tool_response_limit)
    if kind == "mcp_stdio":
        return await execute_stdio_mcp_tool(tool, arguments, settings.tool_response_limit)
    if kind == "local":
        return await execute_local_tool(tool, arguments, settings.tool_response_limit)
    return await execute_http_tool(tool, arguments, settings.allow_http, settings.tool_response_limit)


async def redis_event_relay() -> None:
    from redis.asyncio import Redis
    while True:
        client = Redis.from_url(settings.redis_url, decode_responses=True)
        pubsub = client.pubsub()
        try:
            await pubsub.subscribe("agent-events", "task-events")
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    event = json.loads(message["data"])
                    if message.get("channel") == "task-events":
                        await task_hub.publish(event)
                    else:
                        await hub.publish(event)
                except (TypeError, ValueError, KeyError):
                    continue
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(1)
        finally:
            await pubsub.aclose()
            await client.aclose()


async def publish_pending_outbox() -> int:
    if not settings.redis_url:
        return 0
    from redis.asyncio import Redis
    client = Redis.from_url(settings.redis_url, decode_responses=True)
    published = 0
    try:
        for event in database().pending_outbox_events():
            if event["aggregate_type"] == "agent_run":
                fields = {"run_id": event["aggregate_id"], "outbox_id": event["id"]}
                if event["payload"].get("resume_confirmation"):
                    fields["resume_confirmation"] = "True"
                await client.xadd("agent-runs", fields)
            elif event["aggregate_type"] == "task":
                fields = {
                    "task_id": event["aggregate_id"], "outbox_id": event["id"],
                    "sequence": str(event["payload"].get("sequence", 0)),
                    "event_type": str(event["payload"].get("event_type", "task.dispatch")),
                }
                await client.xadd("task-events", fields)
                await client.publish("task-events", json.dumps({
                    "type": fields["event_type"], "task_id": fields["task_id"],
                    "sequence": int(fields["sequence"]), "timestamp": event["created_at"],
                    "owner_user_id": event["payload"].get("owner_user_id"),
                    "payload": {
                        "summary": event["payload"].get("summary", ""),
                        "metadata": event["payload"].get("metadata", {}), "outbox_id": event["id"],
                    },
                }, separators=(",", ":")))
            else:
                database().mark_outbox_published(event["id"])
                continue
            database().mark_outbox_published(event["id"])
            published += 1
    finally:
        await client.aclose()
    return published


async def outbox_publisher_loop() -> None:
    while True:
        try:
            published = await publish_pending_outbox()
            await asyncio.sleep(0.1 if published else 1.0)
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(1.0)


async def enqueue_run(run_id: str) -> None:
    if not settings.redis_url:
        asyncio.create_task(orchestrate_run(run_id))
        return
    try:
        await publish_pending_outbox()
    except Exception:
        # The durable outbox loop will retry. A transient Redis outage must not
        # turn a recoverable queued run into a false terminal failure.
        return


async def enqueue_confirmation(run_id: str) -> None:
    database().enqueue_confirmation_resume(run_id)
    if not settings.redis_url:
        asyncio.create_task(orchestrate_run(run_id, resume_confirmation=True))
        return
    try:
        await publish_pending_outbox()
    except Exception:
        return


async def orchestrate_run(run_id: str, recover: bool = False, resume_confirmation: bool = False) -> None:
    context = database().get_run_context(run_id)
    if context is None or database().is_cancelled(run_id):
        return
    if not database().try_start_run(run_id, recover=recover, resume_confirmation=resume_confirmation):
        return
    database().sync_task_run_state(run_id, "running")
    await emit(run_id, "agent.run.queued", {"summary": "任务已进入执行队列"})
    await emit(run_id, "agent.run.started", {"summary": "恢复运行" if recover else "开始调用模型"})
    task_history = database().task_run_messages(run_id)
    history = task_history if task_history is not None else database().model_messages(context["conversation_id"], context["context_epoch"])
    policy = context.get("run_policy") or {}
    if isinstance(policy, str):
        policy = json.loads(policy)
    tools = [tool for tool in context.get("tools", []) if tool.get("enabled", True)]
    declarations = tool_declarations(tools, max(256, int(policy.get("context_window", 32768)) // 4))
    declaration_tokens = sum(len(json.dumps(item, ensure_ascii=False).encode("utf-8")) // 3 + 1 for item in declarations)
    memories = []
    if context.get("memory_enabled"):
        memories = database().retrieve_memories(context["agent_id"], context["initiated_by_user_id"], history[-1]["content"] if history else "")
    messages, manifest = prepare_context(
        context["system_prompt"], history, policy, int(context["max_tokens"]), len(declarations), declaration_tokens, memories
    )
    await emit(run_id, "agent.context.prepared", {
        "message_count": len(messages), "estimated_input_tokens": manifest["estimated_input_tokens"],
        "history_dropped": manifest["history_dropped"], "memory_count": manifest["memory_count"], "summary": "已按预算准备当前上下文",
    })
    if memories:
        await emit(run_id, "agent.memory.retrieved", {
            "count": len(memories), "memory_ids": [item["id"] for item in memories], "summary": "已检索授权长期记忆",
        })

    endpoint = context["model_base_url"].rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint += "/chat/completions"
    allowed_names = {item["function"]["name"] for item in declarations}
    tools_by_name = {tool["name"]: tool for tool in tools if tool["name"] in allowed_names}
    total_usage: Dict[str, int] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    final_content = ""
    final_reasoning = ""
    tool_calls_used = 0
    try:
        approved = database().pending_confirmation(run_id) if resume_confirmation else None
        if resume_confirmation and approved is None:
            raise RuntimeError("没有可恢复的已批准工具确认")
        if approved is not None:
            checkpoint = approved["checkpoint"]
            messages = checkpoint["messages"]
            call = checkpoint["call"]
            tool_calls_used = int(checkpoint.get("tool_calls_used", 1))
            tool = checkpoint["tool"]
            call_id = call["id"]
            arguments = approved["arguments"]
            messages.append({"role": "assistant", "content": checkpoint.get("assistant_content"), "tool_calls": [call]})
            await emit(run_id, "agent.tool.started", {
                "tool": tool["name"], "tool_type": tool.get("kind", "tool"),
                "arguments": redact(arguments), "summary": "已确认，正在执行工具",
            })
            if not database().task_budget_allows_tool_call(run_id):
                result = {"status": "denied", "error": "Task tool-call budget exhausted"}
            elif database().count_tool_invocations(run_id, tool["id"]) >= int(tool.get("rate_limit_per_run", 6)):
                result = {"status": "denied", "error": "Tool rate limit exceeded for this run"}
            elif database().record_tool_invocation(run_id, tool["id"], call_id, "started"):
                result = await execute_tool(tool, arguments)
            else:
                result = {"status": "denied", "error": "Task tool-call budget exhausted"}
            tool_content = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
            messages.append({"role": "tool", "tool_call_id": call_id, "content": tool_content})
            if task_history is None:
                database().add_message(context["conversation_id"], run_id, "tool", tool_content, context["context_epoch"])
            database().mark_confirmation_executed(approved["id"])
            await emit(run_id, "agent.tool.completed", {
                "tool": tool["name"], "tool_type": tool.get("kind", "tool"),
                "arguments": redact(arguments), "result": redact(result), "summary": "已执行确认的工具",
            })
        api_key = database().decrypt_api_key(context["encrypted_api_key"])
        timeout = httpx.Timeout(float(context["timeout_seconds"]), connect=10.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False) as client:
            for _ in range(int(policy.get("max_tool_calls", 6)) + 1):
                request_body: Dict[str, Any] = {
                    "model": context["model_id"], "stream": True, "temperature": context["temperature"],
                    "max_tokens": context["max_tokens"], "messages": messages,
                }
                if declarations:
                    request_body["tools"] = declarations
                    request_body["tool_choice"] = "auto"
                turn = ModelTurn()
                async with client.stream(
                    "POST", endpoint,
                    headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
                    json=request_body,
                ) as response:
                    response.raise_for_status()
                    async for parsed, text in stream_chat(response):
                        turn = parsed
                        if database().is_cancelled(run_id):
                            return
                        if parsed.reasoning_delta:
                            final_reasoning += parsed.reasoning_delta
                            await emit(run_id, "agent.message.reasoning.delta", {"content": parsed.reasoning_delta})
                        if text:
                            await emit(run_id, "agent.message.delta", {"content": text})
                for key in total_usage:
                    total_usage[key] += int(turn.usage.get(key, 0))
                if not turn.tool_calls:
                    final_content = turn.text.strip()
                    break
                assistant_calls = []
                for call in turn.tool_calls.values():
                    tool_calls_used += 1
                    arguments: Dict[str, Any] = {}
                    if tool_calls_used > int(policy.get("max_tool_calls", 6)):
                        raise RuntimeError("工具调用次数超过运行预算")
                    tool = tools_by_name.get(call["name"])
                    if tool is None:
                        result = {"status": "denied", "error": "Tool is not authorized for this Agent"}
                    else:
                        try:
                            arguments = json.loads(call["arguments"] or "{}")
                            if not isinstance(arguments, dict):
                                raise ValueError("Tool arguments must be an object")
                            call_id = call["id"] or "call-{}".format(tool_calls_used)
                            assistant_call = {
                                "id": call_id, "type": "function",
                                "function": {"name": call["name"], "arguments": call["arguments"] or "{}"},
                            }
                            if not database().task_budget_allows_tool_call(run_id):
                                result = {"status": "denied", "error": "Task tool-call budget exhausted"}
                            # All write/destructive calls require a durable, argument-bound approval.
                            # Treating per-run as per-call is intentionally stricter than the configured minimum.
                            elif tool.get("side_effect") in {"write", "destructive"} or tool.get("confirmation_mode") != "none":
                                confirmation = database().create_confirmation(run_id, call_id, call["name"], arguments, {
                                    "messages": messages, "call": assistant_call, "tool": tool,
                                    "assistant_content": turn.text or None, "tool_calls_used": tool_calls_used,
                                })
                                database().update_run(run_id, "waiting_confirmation")
                                database().sync_task_run_state(run_id, "waiting_confirmation")
                                await emit(run_id, "agent.tool.confirmation_required", {
                                    "confirmation_id": confirmation["id"], "tool": call["name"],
                                    "tool_type": tool.get("kind", "tool"), "arguments": redact(arguments),
                                    "arguments_hash": confirmation["arguments_hash"],
                                    "side_effect": tool.get("side_effect"), "summary": "等待用户确认工具操作",
                                })
                                return
                            elif tool.get("kind") == "task":
                                if database().record_tool_invocation(run_id, tool["id"], call_id, "started"):
                                    result = database().execute_task_tool(run_id, call_id, call["name"], arguments)
                                else:
                                    result = {"status": "denied", "error": "Task tool-call budget exhausted"}
                            else:
                                if database().count_tool_invocations(run_id, tool["id"]) >= int(tool.get("rate_limit_per_run", 6)):
                                    raise RuntimeError("工具在本次运行中已达到频率上限")
                                await emit(run_id, "agent.tool.started", {
                                    "tool": call["name"], "tool_type": tool.get("kind", "tool"),
                                    "arguments": redact(arguments), "summary": "正在执行工具",
                                })
                                if database().record_tool_invocation(run_id, tool["id"], call_id, "started"):
                                    result = await execute_tool(tool, arguments)
                                else:
                                    result = {"status": "denied", "error": "Task tool-call budget exhausted"}
                        except (ValueError, RuntimeError, httpx.HTTPError, HTTPException) as exc:
                            result = {"status": "error", "error": redact(str(exc))[:500]}
                    await emit(run_id, "agent.tool.completed", {
                        "tool": call["name"], "tool_type": tool.get("kind", "tool") if tool else "tool",
                        "arguments": redact(arguments),
                        "result": redact(result), "summary": "只读工具执行完成",
                    })
                    if result.get("stop_run"):
                        database().update_run(run_id, "cancelled", error_message="执行 Agent 拒绝 Assignment")
                        database().sync_task_run_state(run_id, "cancelled", error="执行 Agent 拒绝 Assignment")
                        await emit(run_id, "agent.run.cancelled", {"summary": "执行 Agent 拒绝 Assignment"})
                        return
                    call_id = call["id"] or "call-{}".format(tool_calls_used)
                    assistant_calls.append({
                        "id": call_id, "type": "function",
                        "function": {"name": call["name"], "arguments": call["arguments"] or "{}"},
                    })
                    tool_content = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
                    messages.append({"role": "tool", "tool_call_id": call_id, "content": tool_content})
                    if task_history is None:
                        database().add_message(
                            context["conversation_id"], run_id, "tool", tool_content, context["context_epoch"]
                        )
                messages.insert(len(messages) - len(assistant_calls), {
                    "role": "assistant", "content": turn.text or None, "tool_calls": assistant_calls,
                })
            else:
                raise RuntimeError("模型循环超过运行预算")
    except httpx.HTTPStatusError as exc:
        message = "模型服务返回 HTTP {}".format(exc.response.status_code)
        database().update_run(run_id, "failed", error_message=message)
        database().sync_task_run_state(run_id, "failed", error=message)
        await emit(run_id, "agent.run.failed", {"error": message})
        return
    except (httpx.HTTPError, RuntimeError) as exc:
        message = "模型调用失败：{}".format(str(exc)[:300])
        database().update_run(run_id, "failed", error_message=message)
        database().sync_task_run_state(run_id, "failed", error=message)
        await emit(run_id, "agent.run.failed", {"error": message})
        return

    if not final_content:
        final_content = "模型没有返回可显示的内容。"
    if not total_usage["total_tokens"]:
        total_usage["input_tokens"] = manifest["estimated_input_tokens"]
        total_usage["output_tokens"] = max(1, len(final_content.encode("utf-8")) // 3)
        total_usage["total_tokens"] = total_usage["input_tokens"] + total_usage["output_tokens"]
        total_usage["estimated"] = 1
    manifest["tool_calls_used"] = tool_calls_used
    database().update_usage(run_id, total_usage, manifest)
    if task_history is None:
        database().add_message(context["conversation_id"], run_id, "assistant", final_content, context["context_epoch"])
    database().update_run(run_id, "completed", final_content=final_content)
    database().sync_task_run_state(run_id, "completed", content=final_content)
    await emit(run_id, "agent.run.completed", {
        "content": final_content, "reasoning": final_reasoning, "usage": total_usage, "summary": "回复已保存",
    })


@app.websocket("/agent/ws")
async def agent_websocket(websocket: WebSocket, token: str = Query("")) -> None:
    try:
        user = await authenticate(token)
    except HTTPException:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    subscribed_run: Optional[str] = None
    queue: Optional[asyncio.Queue] = None
    try:
        while True:
            message = await websocket.receive_json()
            if message.get("type") != "agent.subscribe":
                await websocket.send_json({"type": "agent.error", "payload": {"error": "不支持的事件类型"}})
                continue
            run_id = str(message.get("run_id", ""))
            after_sequence = max(0, int(message.get("after_sequence", 0)))
            previous = database().events_after(run_id, user["user_id"], after_sequence)
            if previous is None:
                await websocket.send_json({"type": "agent.error", "payload": {"error": "运行未找到"}})
                continue
            if queue is not None and subscribed_run is not None:
                await hub.unsubscribe(subscribed_run, queue)
            subscribed_run = run_id
            queue = await hub.subscribe(run_id)
            last_sequence = after_sequence
            for event in previous:
                await websocket.send_json(event)
                last_sequence = max(last_sequence, int(event["sequence"]))
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=1.0)
                    candidates = [event]
                except asyncio.TimeoutError:
                    candidates = database().events_after(run_id, user["user_id"], last_sequence) or []
                for event in candidates:
                    sequence = int(event.get("sequence", 0))
                    if sequence <= last_sequence:
                        continue
                    await websocket.send_json(event)
                    last_sequence = sequence
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        if queue is not None and subscribed_run is not None:
            await hub.unsubscribe(subscribed_run, queue)


@app.websocket("/task/ws")
async def task_websocket(websocket: WebSocket, token: str = Query("")) -> None:
    try:
        user = await authenticate(token)
    except HTTPException:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    subscribed_task: Optional[str] = None
    subscribed_all = False
    queue: Optional[asyncio.Queue] = None

    def public_event(event: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "type": event.get("type", event.get("event_type", "task.dispatch")), "task_id": event["task_id"],
            "sequence": event["sequence"], "timestamp": event.get("timestamp", event.get("created_at")),
            "payload": event.get("payload", {
                "summary": event.get("summary", ""), "metadata": event.get("metadata", {}),
            }),
        }

    async def unsubscribe_current() -> None:
        nonlocal queue, subscribed_task, subscribed_all
        if queue is None:
            return
        if subscribed_all:
            await task_hub.unsubscribe_all(user["user_id"], queue)
        elif subscribed_task is not None:
            await task_hub.unsubscribe(subscribed_task, queue)
        queue = None
        subscribed_task = None
        subscribed_all = False

    try:
        while True:
            message = await websocket.receive_json()
            subscription_type = message.get("type")
            if subscription_type not in {"task.subscribe", "task.subscribe_all"}:
                await websocket.send_json({"type": "task.error", "payload": {"error": "不支持的事件类型"}})
                continue
            await unsubscribe_current()
            if subscription_type == "task.subscribe_all":
                subscribed_all = True
                queue = await task_hub.subscribe_all(user["user_id"])
                # Per-task sequence values cannot form a global cursor. The client
                # reloads the durable task and notification projections on this signal.
                await websocket.send_json({"type": "task.resync_required"})
                last_sequence = 0
            else:
                task_id = str(message.get("task_id", ""))
                after_sequence = max(0, int(message.get("after_sequence", 0)))
                previous = database().task_dispatch_events(user["user_id"], task_id=task_id, after_sequence=after_sequence)
                if database().get_task(task_id, user["user_id"]) is None:
                    await websocket.send_json({"type": "task.error", "payload": {"error": "任务未找到"}})
                    continue
                subscribed_task = task_id
                queue = await task_hub.subscribe(task_id)
                last_sequence = after_sequence
                for event in previous:
                    message_event = public_event(event)
                    await websocket.send_json(message_event)
                    last_sequence = max(last_sequence, int(message_event["sequence"]))
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=1.0)  # type: ignore[union-attr]
                    candidates = [event]
                except asyncio.TimeoutError:
                    if subscribed_all:
                        continue
                    candidates = database().task_dispatch_events(
                        user["user_id"], task_id=subscribed_task, after_sequence=last_sequence,
                    )
                for event in candidates:
                    if event.get("owner_user_id") not in {None, user["user_id"]}:
                        continue
                    sequence = int(event.get("sequence", 0))
                    if not subscribed_all and sequence <= last_sequence:
                        continue
                    message_event = public_event(event)
                    await websocket.send_json(message_event)
                    last_sequence = sequence
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        await unsubscribe_current()


@app.websocket("/local-agent/ws")
async def local_agent_websocket(websocket: WebSocket) -> None:
    try:
        device_id = await authenticated_device(websocket.headers.get("authorization"))
    except HTTPException:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    database().set_local_device_status(device_id, "online")
    try:
        while True:
            offer = database().offer_local_run(device_id)
            if offer is not None:
                await websocket.send_json({"protocol_version": 1, "type": "run.offer", "message_id": str(uuid.uuid4()), "payload": offer})
            try:
                message = await asyncio.wait_for(websocket.receive_json(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if not isinstance(message, dict) or message.get("protocol_version") != 1 or not isinstance(message.get("message_id"), str):
                await websocket.send_json({"protocol_version": 1, "type": "error", "message_id": str(uuid.uuid4()), "payload": {"error": "invalid local-agent envelope"}})
                continue
            message_type = message.get("type")
            payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
            if message_type == "hello":
                await websocket.send_json({"protocol_version": 1, "type": "hello.ack", "message_id": str(uuid.uuid4()), "payload": {"device_id": device_id}})
            elif message_type == "run.claim":
                claimed = database().claim_local_run(str(payload.get("run_id", "")), device_id, str(payload.get("lease_id", "")), str(payload.get("local_session_id", "")))
                await websocket.send_json({"protocol_version": 1, "type": "run.claimed", "message_id": str(uuid.uuid4()), "payload": {"run_id": payload.get("run_id"), "claimed": claimed}})
                if claimed:
                    await emit(str(payload["run_id"]), "agent.run.started", {"summary": "本机 daemon 已取得租约"})
            elif message_type == "lease.renew":
                cancelled = database().renew_local_lease(str(payload.get("run_id", "")), device_id, str(payload.get("lease_id", "")))
                await websocket.send_json({"protocol_version": 1, "type": "lease.ack", "message_id": str(uuid.uuid4()), "payload": {"run_id": payload.get("run_id"), "valid": cancelled is not None, "cancelled": bool(cancelled)}})
            elif message_type == "run.event":
                event = database().append_local_run_event(str(payload.get("run_id", "")), device_id, str(payload.get("lease_id", "")), int(payload.get("sequence", 0)), str(payload.get("event_type", "")), payload.get("payload") or {})
                await websocket.send_json({"protocol_version": 1, "type": "event.ack", "message_id": str(uuid.uuid4()), "payload": {"run_id": payload.get("run_id"), "accepted": event is not None}})
                if event is not None:
                    await publish_event(event)
            elif message_type == "run.finish":
                finished = database().finish_local_run(str(payload.get("run_id", "")), device_id, str(payload.get("lease_id", "")), str(payload.get("state", "")), str(payload.get("content", "")), str(payload.get("error", "")))
                await websocket.send_json({"protocol_version": 1, "type": "run.finish.ack", "message_id": str(uuid.uuid4()), "payload": {"run_id": payload.get("run_id"), "accepted": finished}})
                if finished:
                    await emit(str(payload["run_id"]), "agent.run.completed" if payload["state"] == "completed" else "agent.run.failed", {"summary": "本机 run 已结束"})
            else:
                await websocket.send_json({"protocol_version": 1, "type": "error", "message_id": str(uuid.uuid4()), "payload": {"error": "unsupported local-agent message"}})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        database().set_local_device_status(device_id, "offline")
