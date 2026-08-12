"""Add conversation-private status and autonomous schedule storage."""

from alembic import op


revision = "20260812_0008"
down_revision = "20260812_0007"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE TABLE agent_status (
      conversation_id UUID PRIMARY KEY REFERENCES conversations(id), source VARCHAR(16) NOT NULL DEFAULT 'user',
      status_encrypted BYTEA NOT NULL, updated_at TIMESTAMPTZ NOT NULL
    );
    CREATE TABLE agent_schedules (
      id UUID PRIMARY KEY, agent_id UUID NOT NULL REFERENCES agents(id), owner_user_id BIGINT NOT NULL,
      run_at TIMESTAMPTZ NOT NULL, prompt_encrypted BYTEA NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
      idempotency_key VARCHAR(128) NOT NULL, last_triggered_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL,
      updated_at TIMESTAMPTZ NOT NULL, UNIQUE(agent_id, owner_user_id, idempotency_key)
    );
    CREATE INDEX agent_schedules_due_idx ON agent_schedules (enabled, run_at);
    """)


def downgrade():
    op.execute("DROP TABLE IF EXISTS agent_schedules;")
    op.execute("DROP TABLE IF EXISTS agent_status;")
