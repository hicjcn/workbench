"""Immutable project runtime profiles and workspace bindings."""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"


def upgrade():
    op.create_table("project_profiles",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("project_id", sa.String(40), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False), sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("content_json", sa.Text(), nullable=False), sa.Column("content_digest", sa.String(64), nullable=False),
        sa.Column("actor", sa.String(160), nullable=False), sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False), sa.UniqueConstraint("project_id", "number", name="uq_project_profile_version"))
    op.create_index("uq_current_project_profile", "project_profiles", ["project_id"], unique=True,
        sqlite_where=sa.text("is_current IS 1"), postgresql_where=sa.text("is_current IS true"))
    op.create_table("workspace_profiles",
        sa.Column("requirement_id", sa.String(40), sa.ForeignKey("requirements.id"), primary_key=True),
        sa.Column("profile_id", sa.String(40), sa.ForeignKey("project_profiles.id"), nullable=False))


def downgrade():
    op.drop_table("workspace_profiles")
    op.drop_index("uq_current_project_profile", table_name="project_profiles")
    op.drop_table("project_profiles")
