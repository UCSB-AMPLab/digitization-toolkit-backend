"""Retired offsite keys of pages and shots, and collections.captures_adopted_at (NEH-387)

Revision ID: c8e0a2f4b6d9
Revises: e7c3a9d1f5b2
Create Date: 2026-11-19 12:00:00.000000

An adoption of a hand-made volume's captures renames each file and uploads
it again under a key in the target volume. The unit's bucket key can upload
and list but cannot read, copy or delete, so the object at the old key stays
in the bucket until a person deletes it. Its row is kept, detached from the
image (record_image_id NULL) so that the image can take a new row of the
same kind, and marked retired: `retired_at` (DateTime, NULL), when, and
`retired_image_id` (FK to record_images.id, ON DELETE SET NULL, NULL), the
image it was the key of, indexed. `check_offsite_object_retired` holds a
retired row detached and verified:
`retired_at IS NULL OR (record_image_id IS NULL AND verified_at IS NOT NULL)`.
app.core.offsite_retired writes and reads them.

A colour card shot adopted with its volume is retired the same way in
colour_card_objects: `retired_at` (DateTime, NULL) and `retired_shot_id`
(FK to colour_card_shots.id, ON DELETE SET NULL, NULL), the shot it was the
key of, indexed, with `check_colour_card_object_retired`:
`retired_at IS NULL OR (shot_id IS NULL AND verified_at IS NOT NULL)`.

`collections.captures_adopted_at` (DateTime, NULL): when an adoption moved
the volume's captures into another volume, written in the adoption's
transaction. A volume with it set is closed (app.models.collection).

Upgrade marks no existing row: every row of the three tables gets NULL in
the new columns.

Downgrade drops the checks, the indexes and the columns of the three tables. A
retired row then reads as the row of a deleted image or shot: its key stays
recorded and keeps its stem reserved, and which image or shot it came from,
and when, is lost.

Written with batch_alter_table so it also runs on SQLite.

This revision was never applied outside test databases before it took this
form, with the colour card columns added to it.

Version: 0.3.3
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c8e0a2f4b6d9'
down_revision: Union[str, None] = 'e7c3a9d1f5b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CHECK = "retired_at IS NULL OR (record_image_id IS NULL AND verified_at IS NOT NULL)"
CARD_CHECK = "retired_at IS NULL OR (shot_id IS NULL AND verified_at IS NOT NULL)"


def upgrade() -> None:
    with op.batch_alter_table('offsite_objects') as batch:
        batch.add_column(sa.Column('retired_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('retired_image_id', sa.Integer(), nullable=True))
        batch.create_foreign_key('offsite_objects_retired_image_id_fkey', 'record_images',
                                 ['retired_image_id'], ['id'], ondelete='SET NULL')
        batch.create_index('ix_offsite_objects_retired_image_id', ['retired_image_id'], unique=False)
        batch.create_check_constraint('check_offsite_object_retired', CHECK)
    with op.batch_alter_table('colour_card_objects') as batch:
        batch.add_column(sa.Column('retired_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('retired_shot_id', sa.Integer(), nullable=True))
        batch.create_foreign_key('colour_card_objects_retired_shot_id_fkey', 'colour_card_shots',
                                 ['retired_shot_id'], ['id'], ondelete='SET NULL')
        batch.create_index('ix_colour_card_objects_retired_shot_id', ['retired_shot_id'], unique=False)
        batch.create_check_constraint('check_colour_card_object_retired', CARD_CHECK)
    with op.batch_alter_table('collections') as batch:
        batch.add_column(sa.Column('captures_adopted_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('collections') as batch:
        batch.drop_column('captures_adopted_at')
    with op.batch_alter_table('colour_card_objects') as batch:
        batch.drop_constraint('check_colour_card_object_retired', type_='check')
        batch.drop_index('ix_colour_card_objects_retired_shot_id')
        batch.drop_constraint('colour_card_objects_retired_shot_id_fkey', type_='foreignkey')
        batch.drop_column('retired_shot_id')
        batch.drop_column('retired_at')
    with op.batch_alter_table('offsite_objects') as batch:
        batch.drop_constraint('check_offsite_object_retired', type_='check')
        batch.drop_index('ix_offsite_objects_retired_image_id')
        batch.drop_constraint('offsite_objects_retired_image_id_fkey', type_='foreignkey')
        batch.drop_column('retired_image_id')
        batch.drop_column('retired_at')
