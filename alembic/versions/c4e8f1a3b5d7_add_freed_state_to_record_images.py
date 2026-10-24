"""add freed state to record_images: which local files are stored offsite only

Revision ID: c4e8f1a3b5d7
Revises: b8d2f4a6c1e3
Create Date: 2026-10-24 12:00:00.000000

An admin can free a received volume's files from the card
(app.core.offsite_free). Each image has up to two local files that are
uploaded, and each is freed, or found already removed, on its own: the
master (`<stem>/captures/...`) and, for a RAW master, its preview
(`<stem>/previews/<stem>_preview.jpg`). Hence two pairs of columns rather than
one timestamp:

- `master_freed_at`, `master_freed_bytes`: when the master was removed from
  the card, with its verified object still in the bucket, and its size.
- `preview_freed_at`, `preview_freed_bytes`: the same for the RAW preview.

A file whose `*_freed_at` is set is stored offsite, not missing. The size is
kept because a freed file cannot be measured on the card, and the volume's
offsite state reports the bytes freed. `check_record_image_master_freed` and
`check_record_image_preview_freed` keep each pair set or unset together, with
a size that is not negative.

The Rionegro unit's masters removed by hand in September and October 2026
are marked by the `mark-freed` command, which lists the bucket; a migration
cannot.

Written with batch_alter_table so it also runs on SQLite, which cannot add a
constraint to an existing table. Nothing is backfilled: every row gets NULL,
which every CHECK accepts. Downgrade drops the four columns, and with them the
record of which files were freed; the objects stay in the bucket and the
files stay absent from the card.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4e8f1a3b5d7'
down_revision: Union[str, None] = 'b8d2f4a6c1e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _pair_check(prefix: str) -> str:
    return (f"({prefix}_freed_at IS NULL AND {prefix}_freed_bytes IS NULL) OR "
            f"({prefix}_freed_at IS NOT NULL AND {prefix}_freed_bytes IS NOT NULL "
            f"AND {prefix}_freed_bytes >= 0)")


def upgrade() -> None:
    with op.batch_alter_table('record_images') as batch:
        batch.add_column(sa.Column('master_freed_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('master_freed_bytes', sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column('preview_freed_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('preview_freed_bytes', sa.BigInteger(), nullable=True))
        batch.create_check_constraint('check_record_image_master_freed', _pair_check('master'))
        batch.create_check_constraint('check_record_image_preview_freed', _pair_check('preview'))


def downgrade() -> None:
    with op.batch_alter_table('record_images') as batch:
        batch.drop_constraint('check_record_image_preview_freed', type_='check')
        batch.drop_constraint('check_record_image_master_freed', type_='check')
        batch.drop_column('preview_freed_bytes')
        batch.drop_column('preview_freed_at')
        batch.drop_column('master_freed_bytes')
        batch.drop_column('master_freed_at')
