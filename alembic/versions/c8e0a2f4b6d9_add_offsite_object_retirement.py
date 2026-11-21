"""offsite_objects.retired_at, retired_image_id: keys an adoption retires (NEH-387)

Revision ID: c8e0a2f4b6d9
Revises: c8e2a4f6b0d1
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

Upgrade marks no existing row: every row gets NULL in both.

Downgrade drops the check, the index and both columns. A retired row then
reads as the row of a deleted image: its key stays recorded and keeps its
stem reserved, and which image it came from, and when, is lost.

Written with batch_alter_table so it also runs on SQLite.

Version: 0.3.3
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c8e0a2f4b6d9'
down_revision: Union[str, None] = 'c8e2a4f6b0d1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CHECK = "retired_at IS NULL OR (record_image_id IS NULL AND verified_at IS NOT NULL)"


def upgrade() -> None:
    with op.batch_alter_table('offsite_objects') as batch:
        batch.add_column(sa.Column('retired_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('retired_image_id', sa.Integer(), nullable=True))
        batch.create_foreign_key('offsite_objects_retired_image_id_fkey', 'record_images',
                                 ['retired_image_id'], ['id'], ondelete='SET NULL')
        batch.create_index('ix_offsite_objects_retired_image_id', ['retired_image_id'], unique=False)
        batch.create_check_constraint('check_offsite_object_retired', CHECK)


def downgrade() -> None:
    with op.batch_alter_table('offsite_objects') as batch:
        batch.drop_constraint('check_offsite_object_retired', type_='check')
        batch.drop_index('ix_offsite_objects_retired_image_id')
        batch.drop_constraint('offsite_objects_retired_image_id_fkey', type_='foreignkey')
        batch.drop_column('retired_image_id')
        batch.drop_column('retired_at')
