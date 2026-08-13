"""Store QQ conversation metadata separately from runtime status."""

from alembic import op


revision = "20260813_0009"
down_revision = "20260812_0008"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE TABLE conversation_channel_identities (
      conversation_id UUID NOT NULL REFERENCES conversations(id),
      member_openid VARCHAR(256) NOT NULL,
      display_name VARCHAR(120) NOT NULL,
      created_at TIMESTAMPTZ NOT NULL,
      PRIMARY KEY(conversation_id, member_openid)
    );
    """)


def downgrade():
    op.execute("DROP TABLE IF EXISTS conversation_channel_identities;")
