"""unit_settings: the unit's mode and its projects setting (NEH-311, NEH-313)

Revision ID: d7f9a1c3e5b2
Revises: c4e6a8b0d2f4
Create Date: 2026-11-01 12:00:00.000000

One row (id 1, check_unit_settings_single_row), read and written only by
app.core.unit_mode:

- `mode` (String(16)): `offline`, `backup` or `fisqua`
  (check_unit_settings_mode);
- `projects_enabled` (Boolean, default false);
- `default_project_id` (nullable FK to projects, ON DELETE SET NULL): the
  project new volumes go into while projects are hidden;
- `updated_at`, `updated_by`.

No row is written here. The backend writes it at start-up, with the mode
the unit's configuration implies (app.core.unit_mode.initialise).

Downgrade drops the table, and the mode with it: the earlier revision
reads no mode, and behaves as `fisqua` would wherever Fisqua is configured.

Version: 0.3.3
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd7f9a1c3e5b2'
down_revision: Union[str, None] = 'c4e6a8b0d2f4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'unit_settings',
        sa.Column('id', sa.Integer(), autoincrement=False, nullable=False),
        sa.Column('mode', sa.String(length=16), nullable=False),
        sa.Column('projects_enabled', sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column('default_project_id', sa.Integer(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('updated_by', sa.String(length=150), nullable=True),
        sa.CheckConstraint("mode IN ('offline','backup','fisqua')", name='check_unit_settings_mode'),
        sa.CheckConstraint('id = 1', name='check_unit_settings_single_row'),
        sa.ForeignKeyConstraint(['default_project_id'], ['projects.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('unit_settings')
