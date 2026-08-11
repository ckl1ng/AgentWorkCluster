"""Phase B tool governance, confirmation checkpoints, and explainable memory.

Revision ID: 20260723_0002
Revises: 20260723_0001
Create Date: 2026-07-23
"""

from alembic import op


revision = "20260723_0002"
down_revision = "20260723_0001"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    ALTER TABLE tools ADD COLUMN side_effect VARCHAR(16) NOT NULL DEFAULT 'read'
      CHECK (side_effect IN ('read','write','destructive'));
    ALTER TABLE tools ADD COLUMN provider_version VARCHAR(80) NOT NULL DEFAULT 'http-v1';
    ALTER TABLE tools ADD COLUMN rate_limit_per_run INTEGER NOT NULL DEFAULT 6;
    ALTER TABLE tool_confirmations ADD COLUMN arguments_hash VARCHAR(64) NOT NULL DEFAULT '';
    ALTER TABLE tool_confirmations ADD COLUMN arguments_encrypted BYTEA;
    ALTER TABLE tool_confirmations ADD COLUMN checkpoint_encrypted BYTEA;
    ALTER TABLE tool_confirmations ADD COLUMN rejection_reason VARCHAR(500) NOT NULL DEFAULT '';
    ALTER TABLE memory_items ADD COLUMN scope VARCHAR(16) NOT NULL DEFAULT 'agent'
      CHECK (scope IN ('user','agent'));
    ALTER TABLE memory_items ADD COLUMN kind VARCHAR(24) NOT NULL DEFAULT 'fact'
      CHECK (kind IN ('preference','profile','constraint','fact','experience'));
    ALTER TABLE memory_items ADD COLUMN source_confidence VARCHAR(16) NOT NULL DEFAULT 'user'
      CHECK (source_confidence IN ('user','tool','inferred','imported'));
    ALTER TABLE memory_items ADD COLUMN importance SMALLINT NOT NULL DEFAULT 50;
    ALTER TABLE memory_items ADD COLUMN access_count BIGINT NOT NULL DEFAULT 0;
    ALTER TABLE memory_items ADD COLUMN conflict_state VARCHAR(16) NOT NULL DEFAULT 'active'
      CHECK (conflict_state IN ('active','superseded','conflicted','deleted'));
    ALTER TABLE memory_items ADD COLUMN last_accessed_at TIMESTAMPTZ;
    CREATE INDEX memory_items_lookup_idx ON memory_items (owner_user_id, agent_id, conflict_state, expires_at);
    CREATE TABLE tool_invocations (
      id UUID PRIMARY KEY, run_id UUID NOT NULL REFERENCES runs(id), tool_id UUID NOT NULL REFERENCES tools(id),
      tool_call_id TEXT NOT NULL, status VARCHAR(16) NOT NULL, created_at TIMESTAMPTZ NOT NULL
    );
    CREATE INDEX tool_invocations_run_tool_idx ON tool_invocations (run_id, tool_id, created_at);
    """)


def downgrade():
    op.execute("DROP TABLE IF EXISTS tool_invocations;")
    # Existing Phase A data makes reversing the added columns lossy; downgrade is intentionally unsupported.
    raise RuntimeError("Phase B downgrade is not supported")
