"""Versioned materials, human evidence, and immutable export references."""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"


def upgrade():
    op.create_table('material_rounds',
        sa.Column('id', sa.String(40), primary_key=True),
        sa.Column('requirement_id', sa.String(40), sa.ForeignKey('requirements.id'), nullable=False),
        sa.Column('flow_id', sa.String(40), sa.ForeignKey('workflow_runs.id'), nullable=False),
        sa.Column('revision_id', sa.String(40), sa.ForeignKey('requirement_revisions.id'), nullable=False),
        sa.Column('basis_digest', sa.String(64), nullable=False),
        sa.Column('is_current', sa.Boolean(), nullable=False),
        sa.Column('state', sa.String(30), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('start_json', sa.Text(), nullable=False),
        sa.Column('idempotency_key', sa.String(100), nullable=True),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.UniqueConstraint('requirement_id', 'idempotency_key', name='uq_material_start'))
    op.create_index("uq_current_material_round", "material_rounds", ["requirement_id"], unique=True,
        sqlite_where=sa.text("is_current IS 1"), postgresql_where=sa.text("is_current IS true"))
    op.create_table('material_versions',
        sa.Column('id', sa.String(40), primary_key=True),
        sa.Column('round_id', sa.String(40), sa.ForeignKey('material_rounds.id'), nullable=False),
        sa.Column('kind', sa.String(40), nullable=False),
        sa.Column('number', sa.Integer(), nullable=False),
        sa.Column('content_json', sa.Text(), nullable=False),
        sa.Column('content_digest', sa.String(64), nullable=False),
        sa.Column('basis_json', sa.Text(), nullable=False),
        sa.Column('provenance', sa.String(30), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('job_id', sa.String(40), sa.ForeignKey('jobs.id'), nullable=False, unique=True),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.UniqueConstraint('round_id', 'kind', 'number', name='uq_material_version'))
    op.create_table('material_records',
        sa.Column('id', sa.String(40), primary_key=True),
        sa.Column('round_id', sa.String(40), sa.ForeignKey('material_rounds.id'), nullable=False),
        sa.Column('kind', sa.String(40), nullable=False),
        sa.Column('payload_json', sa.Text(), nullable=False),
        sa.Column('basis_json', sa.Text(), nullable=False),
        sa.Column('job_id', sa.String(40), sa.ForeignKey('jobs.id'), nullable=False, unique=True),
        sa.Column('created_at', sa.Float(), nullable=False))
    op.create_table('material_attempts',
        sa.Column('job_id', sa.String(40), sa.ForeignKey('jobs.id'), primary_key=True),
        sa.Column('round_id', sa.String(40), sa.ForeignKey('material_rounds.id'), nullable=False),
        sa.Column('step_key', sa.String(40), nullable=False),
        sa.Column('input_digest', sa.String(64), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False))
    op.create_table('material_exports',
        sa.Column('id', sa.String(40), primary_key=True),
        sa.Column('round_id', sa.String(40), sa.ForeignKey('material_rounds.id'), nullable=False),
        sa.Column('job_id', sa.String(40), sa.ForeignKey('jobs.id'), nullable=False, unique=True),
        sa.Column('sha256', sa.String(64), nullable=False),
        sa.Column('size', sa.Integer(), nullable=False),
        sa.Column('manifest_json', sa.Text(), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False))


def downgrade():
    op.drop_table('material_exports')
    op.drop_table('material_attempts')
    op.drop_table('material_records')
    op.drop_table('material_versions')
    op.drop_index("uq_current_material_round", table_name="material_rounds")
    op.drop_table('material_rounds')
