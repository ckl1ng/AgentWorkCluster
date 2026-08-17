"""Add execadapter executor_kind to agents for external local executors (Codex).

Revision ID: 20260815_0011
Revises: 20260813_0010
Create Date: 2026-08-15
"""

from alembic import op


revision = "20260815_0011"
down_revision = "20260813_0010"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    ALTER TABLE agents ADD COLUMN executor_kind VARCHAR(16) NOT NULL DEFAULT 'model'
      CHECK (executor_kind IN ('model', 'codex'));
    """)


def downgrade():
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS executor_kind;")
