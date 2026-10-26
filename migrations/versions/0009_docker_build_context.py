"""Optional local build context on immutable Dockerfile versions."""
from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"


def upgrade():
    op.add_column("docker_recipes", sa.Column("context_dir", sa.String(2000), nullable=False, server_default=""))


def downgrade():
    op.drop_column("docker_recipes", "context_dir")
