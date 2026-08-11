"""Add local-agent device, workspace, and dispatch control-plane records.

Revision ID: 20260724_0003
Revises: 20260723_0002
Create Date: 2026-07-24
"""

from alembic import op


revision = "20260724_0003"
down_revision = "20260723_0002"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    ALTER TABLE agents ADD COLUMN execution_target VARCHAR(16) NOT NULL DEFAULT 'cloud'
      CHECK (execution_target IN ('cloud', 'local'));
    ALTER TABLE agents ADD COLUMN default_device_id UUID;
    ALTER TABLE agents ADD COLUMN default_workspace_id UUID;
    ALTER TABLE agents ADD COLUMN model_mode VARCHAR(24) NOT NULL DEFAULT 'server_proxy'
      CHECK (model_mode IN ('server_proxy', 'local_direct'));
    ALTER TABLE tools ADD COLUMN execution_scope VARCHAR(16) NOT NULL DEFAULT 'server'
      CHECK (execution_scope IN ('server', 'device'));
    ALTER TABLE tools ADD COLUMN capability_version VARCHAR(32) NOT NULL DEFAULT '1';
    ALTER TABLE tools ADD COLUMN workspace_required INTEGER NOT NULL DEFAULT 0;
    CREATE TABLE local_agent_devices (
      id UUID PRIMARY KEY, owner_user_id BIGINT NOT NULL, display_name VARCHAR(120) NOT NULL,
      platform VARCHAR(80) NOT NULL DEFAULT '', cli_version VARCHAR(40) NOT NULL DEFAULT '',
      status VARCHAR(16) NOT NULL DEFAULT 'offline' CHECK (status IN ('online','offline','degraded','revoked')),
      capabilities TEXT NOT NULL DEFAULT '[]', credential_hash VARCHAR(64), last_heartbeat_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL
    );
    CREATE INDEX local_agent_devices_owner_idx ON local_agent_devices (owner_user_id, created_at DESC);
    CREATE TABLE local_workspaces (
      id UUID PRIMARY KEY, device_id UUID NOT NULL REFERENCES local_agent_devices(id), display_name VARCHAR(120) NOT NULL,
      policy_version INTEGER NOT NULL DEFAULT 1, capabilities TEXT NOT NULL DEFAULT '[]', created_at TIMESTAMPTZ NOT NULL
    );
    CREATE TABLE local_agent_models (
      agent_id UUID PRIMARY KEY REFERENCES agents(id), device_id UUID NOT NULL REFERENCES local_agent_devices(id),
      model_base_url VARCHAR(1024) NOT NULL, model_id VARCHAR(160) NOT NULL, configured_at TIMESTAMPTZ NOT NULL
    );
    CREATE TABLE local_run_dispatches (
      run_id UUID PRIMARY KEY REFERENCES runs(id), device_id UUID NOT NULL REFERENCES local_agent_devices(id),
      workspace_id UUID NOT NULL REFERENCES local_workspaces(id), executor_state VARCHAR(24) NOT NULL DEFAULT 'pending'
        CHECK (executor_state IN ('pending','offered','claimed','completed','failed','cancelled','disconnected','recovery_required')),
      lease_id UUID, lease_expires_at TIMESTAMPTZ, local_session_id TEXT, last_acked_sequence BIGINT NOT NULL DEFAULT 0
    );
    CREATE TABLE pairing_sessions (
      id UUID PRIMARY KEY, pairing_secret_hash VARCHAR(64) NOT NULL, code_hash VARCHAR(64) NOT NULL,
      display_name VARCHAR(120) NOT NULL, platform VARCHAR(80) NOT NULL DEFAULT '', cli_version VARCHAR(40) NOT NULL DEFAULT '',
      capabilities TEXT NOT NULL DEFAULT '[]', state VARCHAR(16) NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending','approved','expired')),
      owner_user_id BIGINT, device_id UUID REFERENCES local_agent_devices(id),
      expires_at TIMESTAMPTZ NOT NULL, consumed_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL, approved_at TIMESTAMPTZ
    );
    """)


def downgrade():
    op.execute("""
    DROP TABLE IF EXISTS pairing_sessions, local_run_dispatches, local_agent_models, local_workspaces, local_agent_devices;
    ALTER TABLE tools DROP COLUMN IF EXISTS workspace_required;
    ALTER TABLE tools DROP COLUMN IF EXISTS capability_version;
    ALTER TABLE tools DROP COLUMN IF EXISTS execution_scope;
    ALTER TABLE agents DROP COLUMN IF EXISTS model_mode;
    ALTER TABLE agents DROP COLUMN IF EXISTS default_workspace_id;
    ALTER TABLE agents DROP COLUMN IF EXISTS default_device_id;
    ALTER TABLE agents DROP COLUMN IF EXISTS execution_target;
    """)
