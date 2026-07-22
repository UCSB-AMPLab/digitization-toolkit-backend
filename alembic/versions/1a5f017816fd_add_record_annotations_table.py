"""add_record_annotations_table

Revision ID: 1a5f017816fd
Revises: ff2f751fadbb
Create Date: 2026-07-22 15:15:20.499432

Adds record_annotations: persists the QA "Anotaciones" tab (flagged error
typologies + free-text notes), which previously only lived in local
component state and was lost on navigation/refresh.

Note: autogenerate also proposed dropping collections.export_version,
last_exported_at and last_export_hash — those columns exist in the DB
(migration dda9bc1bc608) but aren't declared on the Collection model, a
pre-existing drift unrelated to this change. Left untouched here.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '1a5f017816fd'
down_revision: Union[str, None] = 'ff2f751fadbb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('record_annotations',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('record_id', sa.Integer(), nullable=False),
    sa.Column('error_types', sa.JSON(), nullable=True),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('created_by', sa.String(length=255), nullable=True),
    sa.ForeignKeyConstraint(['record_id'], ['records.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_record_annotations_id'), 'record_annotations', ['id'], unique=False)
    op.create_index(op.f('ix_record_annotations_record_id'), 'record_annotations', ['record_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_record_annotations_record_id'), table_name='record_annotations')
    op.drop_index(op.f('ix_record_annotations_id'), table_name='record_annotations')
    op.drop_table('record_annotations')
