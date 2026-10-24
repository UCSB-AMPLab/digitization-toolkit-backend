"""add offsite_objects: per-object image upload state

Revision ID: b8d2f4a6c1e3
Revises: a7c1e9d2b4f6
Create Date: 2026-10-23 20:00:00.000000

One row per object the unit uploads to, or adopts from, the offsite bucket:
the image it belongs to, which file (`capture` or `preview`), the object key
frozen at first upload, the local SHA1, and when it was uploaded, verified or
refused. See app/models/offsite_object.py for why this is a table and not
columns on record_images.

No backfill. The objects the Rionegro unit's interim script pushed are
recorded by the adoption command (app.core.image_upload), which lists the
bucket; a migration cannot.

Downgrade drops the table, and with it every frozen key the unit recorded.
The objects stay in the bucket.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b8d2f4a6c1e3'
down_revision: Union[str, None] = 'a7c1e9d2b4f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'offsite_objects',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('record_image_id', sa.Integer(), nullable=True),
        sa.Column('kind', sa.String(length=10), nullable=False),
        sa.Column('object_key', sa.String(length=1024), nullable=False),
        sa.Column('local_sha1', sa.String(length=40), nullable=False),
        sa.Column('remote_sha1', sa.String(length=40), nullable=True),
        sa.Column('uploaded_at', sa.DateTime(), nullable=True),
        sa.Column('verified_at', sa.DateTime(), nullable=True),
        sa.Column('refused_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['record_image_id'], ['record_images.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('object_key', name='offsite_objects_object_key_key'),
        sa.UniqueConstraint('record_image_id', 'kind', name='offsite_objects_image_kind_key'),
        sa.CheckConstraint("kind IN ('capture', 'preview')", name='check_offsite_object_kind'),
        sa.CheckConstraint('verified_at IS NULL OR refused_at IS NULL', name='check_offsite_object_state'),
    )
    op.create_index('ix_offsite_objects_id', 'offsite_objects', ['id'], unique=False)
    op.create_index('ix_offsite_objects_record_image_id', 'offsite_objects', ['record_image_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_offsite_objects_record_image_id', table_name='offsite_objects')
    op.drop_index('ix_offsite_objects_id', table_name='offsite_objects')
    op.drop_table('offsite_objects')
