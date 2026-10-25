"""Immutable OpenCode-compatible CLI configurations and workspace bindings."""
from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"


def upgrade():
    op.create_table("agent_cli_configs",
        sa.Column("id", sa.String(40), primary_key=True), sa.Column("family", sa.String(40), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False), sa.Column("name", sa.String(160), nullable=False),
        sa.Column("content_json", sa.Text(), nullable=False), sa.Column("content_digest", sa.String(64), nullable=False),
        sa.Column("actor", sa.String(160), nullable=False), sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False), sa.UniqueConstraint("family", "number", name="uq_agent_cli_version"))
    op.create_table("agent_cli_default", sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("config_id", sa.String(40), sa.ForeignKey("agent_cli_configs.id")))
    op.create_table("workspace_agents", sa.Column("requirement_id", sa.String(40), sa.ForeignKey("requirements.id"), primary_key=True),
        sa.Column("config_id", sa.String(40), sa.ForeignKey("agent_cli_configs.id")))


def downgrade():
    op.drop_table("workspace_agents")
    op.drop_table("agent_cli_default")
    op.drop_table("agent_cli_configs")
