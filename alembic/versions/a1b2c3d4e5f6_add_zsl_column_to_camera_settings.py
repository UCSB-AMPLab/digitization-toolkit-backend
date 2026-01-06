"""add zsl column to camera_settings

Revision ID: a1b2c3d4e5f6
Revises: 050d03fbb579
Create Date: 2026-01-06 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '050d03fbb579'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add zsl (Zero Shutter Lag) column to camera_settings table
    op.add_column(
        'camera_settings',
        sa.Column('zsl', sa.Boolean(), nullable=True, default=False)
    )


def downgrade() -> None:
    # Remove zsl column from camera_settings table
    op.drop_column('camera_settings', 'zsl')
