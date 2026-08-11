"""Harden Task ownership, attempts, results, handoffs, and commands.

Revision ID: 20260727_0005
Revises: 20260727_0004
Create Date: 2026-07-27
"""

from alembic import op


revision = "20260727_0005"
down_revision = "20260727_0004"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    ALTER TABLE tasks ADD COLUMN context_scope_id UUID;
    ALTER TABLE tasks ADD COLUMN budget_snapshot TEXT NOT NULL DEFAULT '{}';
    CREATE TABLE task_assignments (
      id UUID PRIMARY KEY, task_id UUID NOT NULL REFERENCES tasks(id),
      executor_kind VARCHAR(16) NOT NULL CHECK (executor_kind IN ('cloud_agent','local_device')),
      executor_id TEXT NOT NULL, device_id UUID, workspace_id UUID,
      state VARCHAR(24) NOT NULL CHECK (state IN ('assigned','accepted','declined','superseded','expired','completed','cancelled')),
      lease_id UUID, attempt INTEGER NOT NULL, assigned_by_kind VARCHAR(16) NOT NULL,
      assigned_by_id TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL, accepted_at TIMESTAMPTZ,
      completed_at TIMESTAMPTZ, UNIQUE(task_id, attempt)
    );
    CREATE INDEX task_assignments_task_state_idx ON task_assignments (task_id, state, created_at DESC);
    CREATE INDEX task_assignments_executor_idx ON task_assignments (executor_kind, executor_id, state, created_at DESC);

    CREATE TABLE task_handoffs (
      id UUID PRIMARY KEY, from_task_id UUID NOT NULL REFERENCES tasks(id), to_task_id UUID NOT NULL REFERENCES tasks(id),
      from_principal_kind VARCHAR(16) NOT NULL, from_principal_id TEXT NOT NULL,
      to_executor_kind VARCHAR(16) NOT NULL, to_executor_id TEXT NOT NULL,
      input_manifest TEXT NOT NULL DEFAULT '{}', input_encrypted BYTEA, schema_version INTEGER NOT NULL DEFAULT 1,
      created_at TIMESTAMPTZ NOT NULL
    );
    CREATE INDEX task_handoffs_to_task_idx ON task_handoffs (to_task_id, created_at DESC);

    CREATE TABLE task_results (
      id UUID PRIMARY KEY, task_id UUID NOT NULL REFERENCES tasks(id), assignment_id UUID NOT NULL REFERENCES task_assignments(id),
      submitted_by_kind VARCHAR(16) NOT NULL, submitted_by_id TEXT NOT NULL,
      result_encrypted BYTEA NOT NULL, evidence_manifest TEXT NOT NULL DEFAULT '{}', risk_summary TEXT NOT NULL DEFAULT '',
      created_at TIMESTAMPTZ NOT NULL
    );
    CREATE INDEX task_results_task_idx ON task_results (task_id, created_at DESC);

    CREATE TABLE task_command_deduplications (
      id UUID PRIMARY KEY, owner_user_id BIGINT NOT NULL, task_scope TEXT NOT NULL,
      actor_kind VARCHAR(16) NOT NULL, actor_id TEXT NOT NULL, operation VARCHAR(64) NOT NULL,
      idempotency_key VARCHAR(128) NOT NULL, response_encrypted BYTEA NOT NULL,
      created_at TIMESTAMPTZ NOT NULL,
      UNIQUE(owner_user_id, task_scope, actor_kind, actor_id, operation, idempotency_key)
    );
    CREATE INDEX task_command_deduplications_task_idx ON task_command_deduplications (task_scope, created_at DESC);

    ALTER TABLE runs ADD COLUMN assignment_id UUID REFERENCES task_assignments(id);
    CREATE INDEX runs_assignment_idx ON runs (assignment_id, created_at DESC);
    ALTER TABLE tool_confirmations ADD COLUMN task_id UUID REFERENCES tasks(id);
    CREATE INDEX tool_confirmations_task_idx ON tool_confirmations (task_id, created_at DESC);
    """)


def downgrade():
    op.execute("""
    ALTER TABLE tasks DROP COLUMN IF EXISTS budget_snapshot;
    ALTER TABLE tasks DROP COLUMN IF EXISTS context_scope_id;
    ALTER TABLE tool_confirmations DROP COLUMN IF EXISTS task_id;
    ALTER TABLE runs DROP COLUMN IF EXISTS assignment_id;
    DROP TABLE IF EXISTS task_command_deduplications;
    DROP TABLE IF EXISTS task_results;
    DROP TABLE IF EXISTS task_handoffs;
    DROP TABLE IF EXISTS task_assignments;
    """)
