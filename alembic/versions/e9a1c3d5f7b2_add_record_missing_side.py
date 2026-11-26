"""add records.missing_side: the page of a dual opening never photographed

Revision ID: e9a1c3d5f7b2
Revises: b3d5f7a9c1e2
Create Date: 2026-10-30 20:00:00.000000

AHR volumes digitised in 2022-24 with two cameras have openings where only
one camera fired. app.core.rebuild_volume writes such an opening as a dual
record with its one image and sets `missing_side` to the page that is
missing, "left" or "right"; a dual retake clears it. A nullable String(5),
held to those two values or null by `check_record_missing_side`.

Downgrade drops the check and the column. A record that still misses a side
then reads as an ordinary dual record with one image; nothing else records
which page is missing.

Written with batch_alter_table so it also runs on SQLite.

Version: 0.3.2
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e9a1c3d5f7b2'
down_revision: Union[str, None] = 'b3d5f7a9c1e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CHECK = "missing_side IS NULL OR missing_side IN ('left','right')"


def upgrade() -> None:
    with op.batch_alter_table('records') as batch:
        batch.add_column(sa.Column('missing_side', sa.String(length=5), nullable=True))
        batch.create_check_constraint('check_record_missing_side', CHECK)


def downgrade() -> None:
    with op.batch_alter_table('records') as batch:
        batch.drop_constraint('check_record_missing_side', type_='check')
        batch.drop_column('missing_side')
