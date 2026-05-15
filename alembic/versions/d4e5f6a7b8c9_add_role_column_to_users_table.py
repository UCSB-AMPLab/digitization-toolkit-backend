"""add role column to users table

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-05-15 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add role column to users table
    # server_default fills existing rows; nullable=False is safe with it present
    op.add_column(
        'users',
        sa.Column('role', sa.String(50), nullable=False, server_default='reviewer')
    )


def downgrade() -> None:
    # Remove role column from users table
    op.drop_column('users', 'role')