"""Cross-project delivery, workspace reservations and runtime admission."""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"


def upgrade():
    op.create_table('delivery_groups',
        sa.Column('id', sa.String(40), primary_key=True),
        sa.Column('state', sa.String(30), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False))
    op.create_table('delivery_revisions',
        sa.Column('id', sa.String(40), primary_key=True),
        sa.Column('group_id', sa.String(40), sa.ForeignKey('delivery_groups.id'), nullable=False),
        sa.Column('number', sa.Integer(), nullable=False),
        sa.Column('is_current', sa.Boolean(), nullable=False),
        sa.Column('content_json', sa.Text(), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.UniqueConstraint('group_id', 'number', name='uq_delivery_revision'))
    op.create_index("uq_current_delivery_revision", "delivery_revisions", ["group_id"], unique=True,
        sqlite_where=sa.text("is_current IS 1"), postgresql_where=sa.text("is_current IS true"))
    op.create_table('delivery_members',
        sa.Column('revision_id', sa.String(40), sa.ForeignKey('delivery_revisions.id'), primary_key=True),
        sa.Column('requirement_id', sa.String(40), sa.ForeignKey('requirements.id'), primary_key=True),
        sa.Column('project_id', sa.String(40), sa.ForeignKey('projects.id'), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('required', sa.Boolean(), nullable=False),
        sa.Column('skip_reason', sa.Text(), nullable=False),
        sa.UniqueConstraint('revision_id', 'project_id', name='uq_delivery_project'))
    op.create_table('delivery_decisions',
        sa.Column('id', sa.String(40), primary_key=True),
        sa.Column('group_id', sa.String(40), sa.ForeignKey('delivery_groups.id'), nullable=False),
        sa.Column('revision_id', sa.String(40), sa.ForeignKey('delivery_revisions.id'), nullable=False),
        sa.Column('kind', sa.String(30), nullable=False),
        sa.Column('input_digest', sa.String(64), nullable=False),
        sa.Column('payload_json', sa.Text(), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False))
    op.create_table('delivery_attempts',
        sa.Column('job_id', sa.String(40), sa.ForeignKey('jobs.id'), primary_key=True),
        sa.Column('group_id', sa.String(40), sa.ForeignKey('delivery_groups.id'), nullable=False),
        sa.Column('revision_id', sa.String(40), sa.ForeignKey('delivery_revisions.id'), nullable=False),
        sa.Column('kind', sa.String(30), nullable=False),
        sa.Column('input_digest', sa.String(64), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False))
    op.create_table('delivery_exports',
        sa.Column('id', sa.String(40), primary_key=True),
        sa.Column('group_id', sa.String(40), sa.ForeignKey('delivery_groups.id'), nullable=False),
        sa.Column('revision_id', sa.String(40), sa.ForeignKey('delivery_revisions.id'), nullable=False),
        sa.Column('job_id', sa.String(40), sa.ForeignKey('jobs.id'), nullable=False, unique=True),
        sa.Column('input_digest', sa.String(64), nullable=False),
        sa.Column('sha256', sa.String(64), nullable=False),
        sa.Column('size', sa.Integer(), nullable=False),
        sa.Column('manifest_json', sa.Text(), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False))
    op.create_table('workspace_reservations',
        sa.Column('requirement_id', sa.String(40), sa.ForeignKey('requirements.id'), primary_key=True),
        sa.Column('job_id', sa.String(40), sa.ForeignKey('jobs.id'), nullable=False),
        sa.Column('group_id', sa.String(40), sa.ForeignKey('delivery_groups.id'), nullable=False))
    op.create_table('workspace_lifecycles',
        sa.Column('requirement_id', sa.String(40), sa.ForeignKey('requirements.id'), primary_key=True),
        sa.Column('group_id', sa.String(40), sa.ForeignKey('delivery_groups.id'), nullable=False))
    op.create_table('runtime_guard',
        sa.Column('id', sa.Integer(), primary_key=True))
    op.create_table('runner_reports',
        sa.Column('owner', sa.String(80), primary_key=True),
        sa.Column('report_json', sa.Text(), nullable=False),
        sa.Column('updated_at', sa.Float(), nullable=False))


def downgrade():
    op.drop_table('runner_reports')
    op.drop_table('runtime_guard')
    op.drop_table('workspace_lifecycles')
    op.drop_table('workspace_reservations')
    op.drop_table('delivery_exports')
    op.drop_table('delivery_attempts')
    op.drop_table('delivery_decisions')
    op.drop_table('delivery_members')
    op.drop_index("uq_current_delivery_revision", table_name="delivery_revisions")
    op.drop_table('delivery_revisions')
    op.drop_table('delivery_groups')
