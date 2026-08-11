"""Initial production schema for the Phase A Agent Harness.

Revision ID: 20260723_0001
Revises:
Create Date: 2026-07-23
"""

from alembic import op


revision = "20260723_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE TABLE agents (
      id UUID PRIMARY KEY, owner_user_id BIGINT NOT NULL, name VARCHAR(80) NOT NULL,
      description VARCHAR(280) NOT NULL DEFAULT '', avatar_url TEXT NOT NULL DEFAULT '',
      state VARCHAR(16) NOT NULL DEFAULT 'active' CHECK (state IN ('active','paused','archived')),
      current_version INTEGER NOT NULL, model_display_name VARCHAR(80) NOT NULL,
      model_base_url TEXT NOT NULL, model_id VARCHAR(160) NOT NULL, encrypted_api_key BYTEA NOT NULL,
      temperature DOUBLE PRECISION NOT NULL, max_tokens INTEGER NOT NULL, timeout_seconds INTEGER NOT NULL,
      system_prompt TEXT NOT NULL DEFAULT '' CHECK (system_prompt = ''), encrypted_system_prompt BYTEA NOT NULL,
      run_policy TEXT NOT NULL DEFAULT '{}', memory_enabled INTEGER NOT NULL DEFAULT 0,
      memory_retention_days INTEGER NOT NULL DEFAULT 30,
      created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL
    );
    CREATE INDEX agents_owner_idx ON agents (owner_user_id, updated_at DESC);

    CREATE TABLE agent_versions (
      id UUID PRIMARY KEY, agent_id UUID NOT NULL REFERENCES agents(id), version INTEGER NOT NULL,
      snapshot TEXT NOT NULL DEFAULT '{}' CHECK (snapshot = '{}'), snapshot_encrypted BYTEA NOT NULL,
      created_by_user_id BIGINT NOT NULL, created_at TIMESTAMPTZ NOT NULL, UNIQUE(agent_id, version)
    );
    CREATE TABLE conversations (
      id UUID PRIMARY KEY, agent_id UUID NOT NULL REFERENCES agents(id), owner_user_id BIGINT NOT NULL,
      title VARCHAR(120) NOT NULL, context_epoch INTEGER NOT NULL DEFAULT 0, deleted_at TIMESTAMPTZ,
      created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL
    );
    CREATE INDEX conversations_owner_idx ON conversations (owner_user_id, updated_at DESC);
    CREATE TABLE runs (
      id UUID PRIMARY KEY, conversation_id UUID NOT NULL REFERENCES conversations(id),
      agent_id UUID NOT NULL REFERENCES agents(id), agent_version INTEGER NOT NULL,
      initiated_by_user_id BIGINT NOT NULL,
      state VARCHAR(32) NOT NULL DEFAULT 'queued' CHECK (state IN ('queued','running','waiting_confirmation','completed','failed','cancelled')),
      final_content TEXT NOT NULL DEFAULT '' CHECK (final_content = ''), final_content_encrypted BYTEA,
      error_message TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL,
      started_at TIMESTAMPTZ, completed_at TIMESTAMPTZ, usage TEXT NOT NULL DEFAULT '{}',
      attempt INTEGER NOT NULL DEFAULT 0, context_manifest_encrypted BYTEA,
      event_sequence BIGINT NOT NULL DEFAULT 0
    );
    CREATE INDEX runs_agent_state_idx ON runs (agent_id, state, created_at DESC);
    CREATE TABLE messages (
      id UUID PRIMARY KEY, conversation_id UUID NOT NULL REFERENCES conversations(id), run_id UUID REFERENCES runs(id),
      role VARCHAR(24) NOT NULL CHECK (role IN ('user','assistant','tool','system_summary')),
      content TEXT NOT NULL DEFAULT '' CHECK (content = ''), content_encrypted BYTEA NOT NULL,
      context_epoch INTEGER NOT NULL, created_at TIMESTAMPTZ NOT NULL
    );
    CREATE INDEX messages_conversation_idx ON messages (conversation_id, created_at);
    CREATE TABLE trace_events (
      id UUID PRIMARY KEY, run_id UUID NOT NULL REFERENCES runs(id), sequence BIGINT NOT NULL,
      event_type VARCHAR(80) NOT NULL, payload TEXT NOT NULL DEFAULT '{}' CHECK (payload = '{}'),
      payload_encrypted BYTEA NOT NULL, redacted_payload TEXT NOT NULL DEFAULT '{}',
      created_at TIMESTAMPTZ NOT NULL, UNIQUE(run_id, sequence)
    );
    CREATE INDEX trace_events_replay_idx ON trace_events (run_id, sequence);
    CREATE TABLE tools (
      id UUID PRIMARY KEY, owner_user_id BIGINT NOT NULL, name VARCHAR(64) NOT NULL,
      description TEXT NOT NULL, kind VARCHAR(16) NOT NULL, encrypted_config BYTEA NOT NULL,
      input_schema TEXT NOT NULL, confirmation_mode VARCHAR(16) NOT NULL,
      enabled INTEGER NOT NULL DEFAULT 1, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
      UNIQUE(owner_user_id, name)
    );
    CREATE TABLE agent_tools (
      agent_id UUID NOT NULL REFERENCES agents(id), tool_id UUID NOT NULL REFERENCES tools(id), alias VARCHAR(64) NOT NULL,
      PRIMARY KEY(agent_id, tool_id)
    );
    CREATE TABLE run_snapshots (
      run_id UUID PRIMARY KEY REFERENCES runs(id), encrypted_snapshot BYTEA NOT NULL, created_at TIMESTAMPTZ NOT NULL
    );
    CREATE TABLE tool_confirmations (
      id UUID PRIMARY KEY, run_id UUID NOT NULL REFERENCES runs(id), tool_call_id TEXT NOT NULL,
      tool_name VARCHAR(64) NOT NULL, arguments TEXT NOT NULL, state VARCHAR(16) NOT NULL,
      decided_by_user_id BIGINT, created_at TIMESTAMPTZ NOT NULL, decided_at TIMESTAMPTZ,
      UNIQUE(run_id, tool_call_id)
    );
    CREATE TABLE memory_items (
      id UUID PRIMARY KEY, owner_user_id BIGINT NOT NULL, agent_id UUID NOT NULL REFERENCES agents(id),
      content_encrypted BYTEA NOT NULL, embedding TEXT NOT NULL, expires_at TIMESTAMPTZ,
      source_message_id UUID, created_at TIMESTAMPTZ NOT NULL
    );
    CREATE TABLE evaluation_cases (
      id TEXT PRIMARY KEY, agent_id UUID, version_range TEXT NOT NULL DEFAULT '', input_encrypted BYTEA NOT NULL,
      selected_context_encrypted BYTEA, expected_assertions TEXT NOT NULL, tags TEXT NOT NULL,
      created_at TIMESTAMPTZ NOT NULL
    );
    CREATE TABLE evaluation_runs (
      id UUID PRIMARY KEY, harness_version TEXT NOT NULL, agent_version_id TEXT,
      model_connection_id TEXT, aggregate_metrics TEXT NOT NULL DEFAULT '{}',
      started_at TIMESTAMPTZ NOT NULL, completed_at TIMESTAMPTZ
    );
    CREATE TABLE evaluation_results (
      id UUID PRIMARY KEY, evaluation_run_id UUID NOT NULL REFERENCES evaluation_runs(id), case_id TEXT NOT NULL,
      passed INTEGER NOT NULL, score DOUBLE PRECISION NOT NULL, latency_ms INTEGER NOT NULL,
      usage TEXT NOT NULL DEFAULT '{}', tool_trace_ref TEXT, failure_category TEXT,
      failure_detail TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL,
      UNIQUE(evaluation_run_id, case_id)
    );
    CREATE TABLE outbox_events (
      id UUID PRIMARY KEY, aggregate_type VARCHAR(64) NOT NULL, aggregate_id UUID NOT NULL,
      event_type VARCHAR(80) NOT NULL, payload TEXT NOT NULL, published_at TIMESTAMPTZ,
      created_at TIMESTAMPTZ NOT NULL
    );
    CREATE INDEX outbox_events_pending_idx ON outbox_events (created_at) WHERE published_at IS NULL;
    """)


def downgrade():
    op.execute("""
    DROP TABLE IF EXISTS outbox_events, evaluation_results, evaluation_runs, evaluation_cases,
      memory_items, tool_confirmations, run_snapshots, agent_tools, tools, trace_events,
      messages, runs, conversations, agent_versions, agents CASCADE;
    """)
