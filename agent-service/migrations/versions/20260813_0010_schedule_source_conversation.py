"""Bind one-shot schedules to the conversation that created them."""

from alembic import op


revision = "20260813_0010"
down_revision = "20260813_0009"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE agent_schedules ADD COLUMN source_conversation_id UUID;")


def downgrade():
    op.execute("ALTER TABLE agent_schedules DROP COLUMN IF EXISTS source_conversation_id;")
