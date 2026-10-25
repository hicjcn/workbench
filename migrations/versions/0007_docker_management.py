"""Versioned Dockerfiles and separately leased image maintenance jobs."""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"


def upgrade():
    op.create_table("docker_recipes",
        sa.Column("id", sa.String(40), primary_key=True), sa.Column("family", sa.String(40), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False), sa.Column("name", sa.String(160), nullable=False),
        sa.Column("reference", sa.String(240), nullable=False), sa.Column("dockerfile", sa.Text(), nullable=False),
        sa.Column("actor", sa.String(160), nullable=False), sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False), sa.UniqueConstraint("family", "number", name="uq_docker_recipe_version"))
    op.create_table("docker_jobs",
        sa.Column("id", sa.String(40), primary_key=True), sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("input_json", sa.Text(), nullable=False), sa.Column("status", sa.String(30), nullable=False),
        sa.Column("idempotency_key", sa.String(100), nullable=False, unique=True), sa.Column("owner", sa.String(80)),
        sa.Column("epoch", sa.Integer(), nullable=False), sa.Column("lease_until", sa.Float()),
        sa.Column("result_json", sa.Text()), sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.Float(), nullable=False), sa.Column("updated_at", sa.Float(), nullable=False))


def downgrade():
    op.drop_table("docker_jobs")
    op.drop_table("docker_recipes")
