"""add system_logs table

Revision ID: 2dfe4c99b732
Revises: 7f8a9b0c1d2e
Create Date: 2026-05-19 21:49:07.647267

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2dfe4c99b732'
down_revision: Union[str, None] = '7f8a9b0c1d2e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'system_logs',
        sa.Column('id',         sa.Integer(),                                     nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('level',      sa.String(length=10),                             nullable=False),
        sa.Column('category',   sa.String(length=20),                             nullable=False),
        sa.Column('actor',      sa.String(length=150),                            nullable=True),
        sa.Column('action',     sa.String(length=80),                             nullable=False),
        sa.Column('subject',    sa.String(length=300),                            nullable=True),
        sa.Column('detail',     sa.String(length=500),                            nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_system_logs_id'),         'system_logs', ['id'],         unique=False)
    op.create_index(op.f('ix_system_logs_created_at'), 'system_logs', ['created_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_system_logs_created_at'), table_name='system_logs')
    op.drop_index(op.f('ix_system_logs_id'),         table_name='system_logs')
    op.drop_table('system_logs')
