"""Persist Channel gateway event idempotency keys.

Revision ID: 20260811_0006
Revises: 20260727_0005
Create Date: 2026-08-11
"""

from alembic import op


revision = "20260811_0006"
down_revision = "20260727_0005"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE TABLE channel_event_deduplications (
      provider VARCHAR(32) NOT NULL,
      bot_id VARCHAR(128) NOT NULL,
      event_id VARCHAR(256) NOT NULL,
      conversation_id UUID NOT NULL REFERENCES conversations(id),
      run_id UUID NOT NULL REFERENCES runs(id),
      owner_user_id BIGINT NOT NULL,
      created_at TIMESTAMPTZ NOT NULL,
      PRIMARY KEY(provider, bot_id, event_id)
    );
    """)


def downgrade():
    op.execute("DROP TABLE IF EXISTS channel_event_deduplications;")
