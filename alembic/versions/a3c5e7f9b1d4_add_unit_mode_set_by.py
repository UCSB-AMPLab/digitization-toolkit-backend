"""add unit_settings.mode_set_by and mode_set_at: who set the unit's mode and when (NEH-373)

Revision ID: a3c5e7f9b1d4
Revises: b7d9f1a3c5e8
Create Date: 2026-11-26 18:00:00.000000

See app/models/unit_settings.py for what each column means.

- `mode_set_by` String(150), NULL: the username of the admin whose switch
  stored the current mode; NULL for the mode the unit wrote itself at
  start-up, and for a mode nobody recorded.
- `mode_set_at` DateTime (UTC, no zone, as `updated_at`), NULL: when the
  current mode was stored; NULL for a mode nobody recorded.

No backfill: a unit that updates to this revision keeps NULL in both, since
nobody knows who set its mode or when (`updated_by` and `updated_at` also
move with every other setting).

Downgrade drops both columns; what they recorded is lost.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3c5e7f9b1d4'
down_revision: Union[str, None] = 'b7d9f1a3c5e8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('unit_settings') as batch:
        batch.add_column(sa.Column('mode_set_by', sa.String(length=150), nullable=True))
        batch.add_column(sa.Column('mode_set_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('unit_settings') as batch:
        batch.drop_column('mode_set_at')
        batch.drop_column('mode_set_by')
