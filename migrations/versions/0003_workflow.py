"""Immutable requirement revisions and versioned workflow attempts."""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"


def upgrade():
    op.create_table("requirement_revisions",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("requirement_id", sa.String(40), sa.ForeignKey("requirements.id"), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False), sa.Column("content_json", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False), sa.Column("created_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("requirement_id", "number", name="uq_requirement_revision"))
    op.create_table("workflow_runs",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("requirement_id", sa.String(40), sa.ForeignKey("requirements.id"), nullable=False),
        sa.Column("revision_id", sa.String(40), sa.ForeignKey("requirement_revisions.id"), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False), sa.Column("state", sa.String(30), nullable=False),
        sa.Column("basis_digest", sa.String(64), nullable=False), sa.Column("config_json", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False), sa.Column("created_at", sa.Float(), nullable=False))
    op.create_index("uq_current_workflow", "workflow_runs", ["requirement_id"], unique=True,
        sqlite_where=sa.text("is_current IS 1"), postgresql_where=sa.text("is_current IS true"))
    op.create_table("workflow_steps",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("flow_id", sa.String(40), sa.ForeignKey("workflow_runs.id"), nullable=False),
        sa.Column("step_key", sa.String(40), nullable=False),
        sa.Column("job_id", sa.String(40), sa.ForeignKey("jobs.id"), nullable=False, unique=True),
        sa.Column("input_digest", sa.String(64), nullable=False), sa.Column("input_json", sa.Text(), nullable=False),
        sa.Column("output_json", sa.Text()), sa.Column("created_at", sa.Float(), nullable=False))


def downgrade():
    op.drop_table("workflow_steps")
    op.drop_index("uq_current_workflow", table_name="workflow_runs")
    op.drop_table("workflow_runs")
    op.drop_table("requirement_revisions")
