"""Business projects and requirements with explicit repository sets."""
from alembic import op
import sqlalchemy as sa

revision = '0010'
down_revision = '0009'


def upgrade():
    op.add_column('projects', sa.Column('repositories_json', sa.Text(), nullable=False, server_default='[]'))
    op.add_column('projects', sa.Column('shared_files_json', sa.Text(), nullable=False, server_default='[]'))
    op.add_column('requirements', sa.Column('repositories_json', sa.Text(), nullable=False, server_default='[]'))


def downgrade():
    op.drop_column('requirements', 'repositories_json')
    op.drop_column('projects', 'shared_files_json')
    op.drop_column('projects', 'repositories_json')
