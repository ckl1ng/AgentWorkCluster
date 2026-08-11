"""Add the owned, isolated Task domain used by Agent cluster orchestration.

Revision ID: 20260727_0004
Revises: 20260724_0003
Create Date: 2026-07-27
"""

from alembic import op


revision = "20260727_0004"
down_revision = "20260724_0003"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE TABLE tasks (
      id UUID PRIMARY KEY, root_task_id UUID NOT NULL, parent_task_id UUID,
      owner_user_id BIGINT NOT NULL, proposer_kind VARCHAR(16) NOT NULL CHECK (proposer_kind IN ('user','agent')),
      proposer_id TEXT NOT NULL, title VARCHAR(160) NOT NULL,
      state VARCHAR(32) NOT NULL CHECK (state IN ('draft','queued','assigned','in_progress','waiting_confirmation',
        'awaiting_proposer_close','attention_required','closed','cancelled')),
      goal_encrypted BYTEA NOT NULL, result_summary_encrypted BYTEA,
      assigned_agent_id UUID REFERENCES agents(id), conversation_id UUID REFERENCES conversations(id), state_version BIGINT NOT NULL DEFAULT 1,
      context_sequence BIGINT NOT NULL DEFAULT 0, dispatch_sequence BIGINT NOT NULL DEFAULT 0,
      closed_by_kind VARCHAR(16), closed_by_id TEXT, closed_at TIMESTAMPTZ,
      created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL
    );
    CREATE INDEX tasks_owner_state_idx ON tasks (owner_user_id, state, updated_at DESC);
    CREATE INDEX tasks_root_idx ON tasks (root_task_id, created_at);
    CREATE TABLE task_context_events (
      id UUID PRIMARY KEY, task_id UUID NOT NULL REFERENCES tasks(id), sequence BIGINT NOT NULL,
      kind VARCHAR(64) NOT NULL, content_encrypted BYTEA, redacted_payload TEXT NOT NULL DEFAULT '{}',
      actor_kind VARCHAR(16) NOT NULL, actor_id TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL,
      UNIQUE(task_id, sequence)
    );
    CREATE TABLE task_dispatch_events (
      id UUID PRIMARY KEY, task_id UUID NOT NULL REFERENCES tasks(id), sequence BIGINT NOT NULL,
      event_type VARCHAR(64) NOT NULL, summary VARCHAR(280) NOT NULL,
      metadata TEXT NOT NULL DEFAULT '{}', actor_kind VARCHAR(16) NOT NULL,
      actor_id TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL, UNIQUE(task_id, sequence)
    );
    CREATE TABLE notifications (
      id UUID PRIMARY KEY, owner_user_id BIGINT NOT NULL, kind VARCHAR(64) NOT NULL,
      task_id UUID NOT NULL REFERENCES tasks(id), payload TEXT NOT NULL DEFAULT '{}',
      read_at TIMESTAMPTZ, dedupe_key VARCHAR(255) NOT NULL, created_at TIMESTAMPTZ NOT NULL,
      UNIQUE(owner_user_id, dedupe_key)
    );
    CREATE INDEX notifications_owner_idx ON notifications (owner_user_id, read_at, created_at DESC);
    ALTER TABLE runs ADD COLUMN task_id UUID REFERENCES tasks(id);
    CREATE INDEX runs_task_idx ON runs (task_id, created_at DESC);
    """)


def downgrade():
    op.execute("""
    ALTER TABLE runs DROP COLUMN IF EXISTS task_id;
    DROP TABLE IF EXISTS notifications;
    DROP TABLE IF EXISTS task_dispatch_events;
    DROP TABLE IF EXISTS task_context_events;
    DROP TABLE IF EXISTS tasks;
    """)
