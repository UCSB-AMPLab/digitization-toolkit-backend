"""add colour card shots: the unit's setting, and the sets, shots and offsite objects of each volume (NEH-340)

Revision ID: e7c3a9d1f5b2
Revises: d2b4f6a8c0e3
Create Date: 2026-11-22 18:00:00.000000

See app/models/colour_card.py for what each column means.

- `unit_settings.colour_card_shots` Boolean, NOT NULL, server default
  false: a unit that updates to this revision does not ask for colour card
  shots until an administrator turns it on.
- `colour_card_sets`: `collection_id` FK to collections ON DELETE CASCADE
  (`colour_card_sets_collection_id_fkey`), indexed; `number` unique in the
  volume (`colour_card_sets_collection_id_number_key`); `outcome` null,
  `taken` or `skipped` (`check_colour_card_set_outcome`), null exactly while
  `completed_at` is (`check_colour_card_set_completed`); `capture_mode`
  `single` or `dual` (`check_colour_card_set_capture_mode`); `cameras` JSON
  NOT NULL; `opened_at` NOT NULL, `opened_by`, `completed_at`,
  `completed_by`, `after_image_id`.
- `colour_card_shots`: `set_id` FK to colour_card_sets ON DELETE CASCADE
  (`colour_card_shots_set_id_fkey`), indexed; one per camera in a set
  (`colour_card_shots_set_id_camera_index_key`); `capture_id` unique
  (`colour_card_shots_capture_id_key`); the file's name, path, size and
  sha256, the preview's name, path and sha256, `rotate_deg`, `captured_at`
  NOT NULL, `captured_by`, `settings` JSON.
- `colour_card_objects`: `shot_id` FK to colour_card_shots ON DELETE SET
  NULL (`colour_card_objects_shot_id_fkey`), indexed; `kind` `shot` or
  `preview` (`check_colour_card_object_kind`); `object_key` unique
  (`colour_card_objects_object_key_key`); one per shot and kind
  (`colour_card_objects_shot_id_kind_key`); the states and refusals of
  offsite_objects (`check_colour_card_object_state`,
  `check_colour_card_object_refusal`, which also takes the skip reasons
  `staging_failed`, `file_unstable`, `listed_without_sha1` and `unverified`);
  `local_sha1` nullable (a row that only records skips); `last_skip_reason`
  String(20) and `skip_count` Integer NOT NULL, server default 0.

No backfill: no shot was taken before this revision.

Downgrade drops the three tables and the column. The shots' rows, and the
record of their offsite objects, are lost; their files stay in each
volume's `calibration/` folder and in the bucket.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e7c3a9d1f5b2'
down_revision: Union[str, None] = 'd2b4f6a8c0e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Written out rather than imported from app.models.colour_card, so that a
# later change to the model does not change what this revision created.
OUTCOME_CHECK = "outcome IS NULL OR outcome IN ('taken', 'skipped')"
COMPLETED_CHECK = "(outcome IS NULL) = (completed_at IS NULL)"
CAPTURE_MODE_CHECK = "capture_mode IN ('single', 'dual')"
OBJECT_KIND_CHECK = "kind IN ('shot', 'preview')"
OBJECT_STATE_CHECK = "verified_at IS NULL OR refused_at IS NULL"
OBJECT_REFUSAL_CHECK = (
    "(refused_at IS NULL AND refusal IS NULL) OR "
    "(refused_at IS NOT NULL AND refusal IS NOT NULL AND refusal IN ('remote_differs', 'fixity', "
    "'staging_failed', 'file_unstable', 'listed_without_sha1', 'unverified'))")


def upgrade() -> None:
    with op.batch_alter_table('unit_settings') as batch:
        batch.add_column(sa.Column('colour_card_shots', sa.Boolean(), server_default=sa.false(), nullable=False))

    op.create_table(
        'colour_card_sets',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('collection_id', sa.Integer(), nullable=False),
        sa.Column('number', sa.Integer(), nullable=False),
        sa.Column('outcome', sa.String(length=10), nullable=True),
        sa.Column('capture_mode', sa.String(length=10), nullable=False),
        sa.Column('cameras', sa.JSON(), nullable=False),
        sa.Column('opened_at', sa.DateTime(), nullable=False),
        sa.Column('opened_by', sa.String(length=150), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.Column('completed_by', sa.String(length=150), nullable=True),
        sa.Column('after_image_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['collection_id'], ['collections.id'], ondelete='CASCADE',
                                name='colour_card_sets_collection_id_fkey'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('collection_id', 'number', name='colour_card_sets_collection_id_number_key'),
        sa.CheckConstraint(OUTCOME_CHECK, name='check_colour_card_set_outcome'),
        sa.CheckConstraint(COMPLETED_CHECK, name='check_colour_card_set_completed'),
        sa.CheckConstraint(CAPTURE_MODE_CHECK, name='check_colour_card_set_capture_mode'),
    )
    op.create_index(op.f('ix_colour_card_sets_collection_id'), 'colour_card_sets', ['collection_id'], unique=False)

    op.create_table(
        'colour_card_shots',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('set_id', sa.Integer(), nullable=False),
        sa.Column('camera_index', sa.Integer(), nullable=False),
        sa.Column('hardware_id', sa.String(length=255), nullable=True),
        sa.Column('role', sa.String(length=10), nullable=True),
        sa.Column('capture_id', sa.String(length=36), nullable=False),
        sa.Column('filename', sa.String(length=255), nullable=False),
        sa.Column('file_path', sa.String(length=512), nullable=False),
        sa.Column('file_size', sa.Integer(), nullable=True),
        sa.Column('sha256', sa.String(length=64), nullable=False),
        sa.Column('preview_filename', sa.String(length=255), nullable=True),
        sa.Column('preview_path', sa.String(length=512), nullable=True),
        sa.Column('preview_sha256', sa.String(length=64), nullable=True),
        sa.Column('rotate_deg', sa.Integer(), nullable=True),
        sa.Column('captured_at', sa.DateTime(), nullable=False),
        sa.Column('captured_by', sa.String(length=150), nullable=True),
        sa.Column('settings', sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(['set_id'], ['colour_card_sets.id'], ondelete='CASCADE',
                                name='colour_card_shots_set_id_fkey'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('set_id', 'camera_index', name='colour_card_shots_set_id_camera_index_key'),
        sa.UniqueConstraint('capture_id', name='colour_card_shots_capture_id_key'),
    )
    op.create_index(op.f('ix_colour_card_shots_set_id'), 'colour_card_shots', ['set_id'], unique=False)

    op.create_table(
        'colour_card_objects',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('shot_id', sa.Integer(), nullable=True),
        sa.Column('kind', sa.String(length=10), nullable=False),
        sa.Column('object_key', sa.String(length=1024), nullable=False),
        sa.Column('local_sha1', sa.String(length=40), nullable=True),
        sa.Column('remote_sha1', sa.String(length=40), nullable=True),
        sa.Column('uploaded_at', sa.DateTime(), nullable=True),
        sa.Column('verified_at', sa.DateTime(), nullable=True),
        sa.Column('refused_at', sa.DateTime(), nullable=True),
        sa.Column('refusal', sa.String(length=20), nullable=True),
        sa.Column('last_skip_reason', sa.String(length=20), nullable=True),
        sa.Column('skip_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['shot_id'], ['colour_card_shots.id'], ondelete='SET NULL',
                                name='colour_card_objects_shot_id_fkey'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('object_key', name='colour_card_objects_object_key_key'),
        sa.UniqueConstraint('shot_id', 'kind', name='colour_card_objects_shot_id_kind_key'),
        sa.CheckConstraint(OBJECT_KIND_CHECK, name='check_colour_card_object_kind'),
        sa.CheckConstraint(OBJECT_STATE_CHECK, name='check_colour_card_object_state'),
        sa.CheckConstraint(OBJECT_REFUSAL_CHECK, name='check_colour_card_object_refusal'),
    )
    op.create_index(op.f('ix_colour_card_objects_shot_id'), 'colour_card_objects', ['shot_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_colour_card_objects_shot_id'), table_name='colour_card_objects')
    op.drop_table('colour_card_objects')
    op.drop_index(op.f('ix_colour_card_shots_set_id'), table_name='colour_card_shots')
    op.drop_table('colour_card_shots')
    op.drop_index(op.f('ix_colour_card_sets_collection_id'), table_name='colour_card_sets')
    op.drop_table('colour_card_sets')
    with op.batch_alter_table('unit_settings') as batch:
        batch.drop_column('colour_card_shots')
