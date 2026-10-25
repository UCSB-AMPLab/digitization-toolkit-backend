"""record each image's rotation on record_images

Revision ID: d7a1c3e5f9b2
Revises: c4e8f1a3b5d7
Create Date: 2026-10-24 18:00:00.000000

A body's rotation (NEH-71) was written only into the EXIF Orientation tag of
the captured file. piexif can insert only into a JPEG, so a RAW master (a
`.cr2` from the DSLR rig) kept Orientation 1 while its preview and thumbnail
were rotated. A RAW master is a preservation master and is left as the
camera wrote it; its rotation is recorded beside it instead, in the capture
record and here.

- `rotate_deg`: the clockwise rotation in degrees that displays the image
  upright, one of 0, 90, 180, 270 (`check_record_image_rotate_deg`). NULL
  for an upload, and for every image captured before this revision: nothing
  is backfilled, because a body's orientation can have changed since the
  capture.

Written with batch_alter_table so it also runs on SQLite. Downgrade drops
the column; the capture records keep the rotation of images captured after
this revision.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd7a1c3e5f9b2'
down_revision: Union[str, None] = 'c4e8f1a3b5d7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('record_images') as batch:
        batch.add_column(sa.Column('rotate_deg', sa.Integer(), nullable=True))
        batch.create_check_constraint('check_record_image_rotate_deg',
                                      'rotate_deg IS NULL OR rotate_deg IN (0, 90, 180, 270)')


def downgrade() -> None:
    with op.batch_alter_table('record_images') as batch:
        batch.drop_constraint('check_record_image_rotate_deg', type_='check')
        batch.drop_column('rotate_deg')
