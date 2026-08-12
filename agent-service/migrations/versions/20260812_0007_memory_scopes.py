"""Add explicit memory scopes for conversation and channel isolation."""

from alembic import op


revision = "20260812_0007"
down_revision = "20260811_0006"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE memory_items ADD COLUMN scope_type VARCHAR(32) NOT NULL DEFAULT 'agent';")
    op.execute("ALTER TABLE memory_items ADD COLUMN scope_id VARCHAR(256) NOT NULL DEFAULT '';")
    op.execute("CREATE INDEX memory_items_scope_idx ON memory_items (agent_id, owner_user_id, scope_type, scope_id, conflict_state);")
    op.execute("ALTER TABLE conversations ADD COLUMN channel_provider VARCHAR(32) NOT NULL DEFAULT '';")
    op.execute("ALTER TABLE conversations ADD COLUMN channel_scope_type VARCHAR(32) NOT NULL DEFAULT '';")
    op.execute("ALTER TABLE conversations ADD COLUMN channel_scope_id VARCHAR(256) NOT NULL DEFAULT '';")


def downgrade():
    op.execute("DROP INDEX IF EXISTS memory_items_scope_idx;")
    op.execute("ALTER TABLE memory_items DROP COLUMN IF EXISTS scope_id;")
    op.execute("ALTER TABLE memory_items DROP COLUMN IF EXISTS scope_type;")
    op.execute("ALTER TABLE conversations DROP COLUMN IF EXISTS channel_scope_id;")
    op.execute("ALTER TABLE conversations DROP COLUMN IF EXISTS channel_scope_type;")
    op.execute("ALTER TABLE conversations DROP COLUMN IF EXISTS channel_provider;")
