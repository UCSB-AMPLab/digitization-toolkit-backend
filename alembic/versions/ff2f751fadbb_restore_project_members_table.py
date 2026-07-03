"""restore_project_members_table

Revision ID: ff2f751fadbb
Revises: dda9bc1bc608
Create Date: 2026-05-30 23:39:05.710078

Corrects migration dda9bc1bc608 which incorrectly dropped project_members
in its upgrade step.  The model was missing from alembic/env.py so
autogenerate never noticed.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'ff2f751fadbb'
down_revision: Union[str, None] = 'dda9bc1bc608'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'project_members',
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('user_id',    sa.Integer(), nullable=False),
        sa.Column('role',       sa.String(50), nullable=False),
        sa.Column('added_at',   sa.DateTime(), nullable=False,
                  server_default=sa.text('now()')),
        sa.Column('added_by',   sa.String(255), nullable=True),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'],
                                name='project_members_project_id_fkey',
                                ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'],
                                name='project_members_user_id_fkey',
                                ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('project_id', 'user_id',
                                name='project_members_pkey'),
    )


def downgrade() -> None:
    op.drop_table('project_members')
