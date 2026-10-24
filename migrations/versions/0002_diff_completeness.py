"""Persist whether the review interface contains the complete diff."""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"


def upgrade():
    op.add_column("workspaces", sa.Column("diff_truncated", sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade():
    with op.batch_alter_table("workspaces") as batch:
        batch.drop_column("diff_truncated")
