"""add Fisqua's QC decisions: rotation corrections on record_images, the applied set on fisqua_status

Revision ID: a5f0306a8f8c
Revises: 9b7087bd0bc5
Create Date: 2026-10-25 12:00:00.000000

Contract 1.2 §9.5: Fisqua decides each opening (approved, or rejected with a
reason) and may correct an image's rotation; the unit reads the set and
applies it (app.core.fisqua_decisions).

- `record_images.rotation_correction_quarter_turns`: Fisqua's correction, in
  quarter-turns clockwise, 1 to 3 (`check_record_image_rotation_correction`),
  for display. NULL when Fisqua has made none. No file is changed.
- On `fisqua_status`: `decisions_id`, `decisions_decided_at`,
  `decisions_applied_at`, `decisions_manifest_id` and `decisions_report` for
  the last set applied, `decisions_stale_id` for the last set seen for
  another manifest, and `decisions_offered_id` for the last set a status
  entry offered, which keeps a final volume polled until it is read.

Nothing is backfilled. Written with batch_alter_table so it also runs on
SQLite, where it rebuilds record_images: alembic/env.py runs it with foreign
keys off so the rebuild fires no ON DELETE action of the tables that
reference record_images, and the migration refuses to run on SQLite with
them on. Downgrade drops the eight columns, and with them the corrections and
the record of which set was applied; the statuses and rejections the sets
wrote stay.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a5f0306a8f8c'
down_revision: Union[str, None] = '9b7087bd0bc5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _refuse_rebuild_with_foreign_keys_on() -> None:
    """On SQLite, refuse to rebuild record_images while foreign keys are enforced.

    batch_alter_table rebuilds the table there, and the DROP of the original
    would set offsite_objects.record_image_id to NULL and delete the
    camera_settings and exif_data rows. alembic/env.py turns enforcement off
    before it begins the transaction (app.core.db.sqlite_foreign_keys_off);
    a caller that has not is stopped here rather than losing those rows.
    PostgreSQL alters the table in place and is not affected.
    """
    bind = op.get_bind()
    if bind.dialect.name == 'sqlite' and bind.exec_driver_sql('PRAGMA foreign_keys').scalar():
        raise RuntimeError(
            "record_images cannot be rebuilt on SQLite with foreign keys enforced: the rebuild would "
            "fire the ON DELETE actions of offsite_objects, camera_settings and exif_data. Run the "
            "migration through alembic/env.py, which turns foreign keys off outside the transaction.")


def upgrade() -> None:
    _refuse_rebuild_with_foreign_keys_on()
    with op.batch_alter_table('record_images') as batch:
        batch.add_column(sa.Column('rotation_correction_quarter_turns', sa.Integer(), nullable=True))
        batch.create_check_constraint(
            'check_record_image_rotation_correction',
            'rotation_correction_quarter_turns IS NULL OR rotation_correction_quarter_turns IN (1, 2, 3)')
    with op.batch_alter_table('fisqua_status') as batch:
        batch.add_column(sa.Column('decisions_id', sa.String(length=255), nullable=True))
        batch.add_column(sa.Column('decisions_decided_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('decisions_applied_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('decisions_manifest_id', sa.String(length=36), nullable=True))
        batch.add_column(sa.Column('decisions_report', sa.JSON(), nullable=True))
        batch.add_column(sa.Column('decisions_stale_id', sa.String(length=255), nullable=True))
        batch.add_column(sa.Column('decisions_offered_id', sa.String(length=255), nullable=True))


def downgrade() -> None:
    _refuse_rebuild_with_foreign_keys_on()
    with op.batch_alter_table('fisqua_status') as batch:
        for name in ('decisions_offered_id', 'decisions_stale_id', 'decisions_report', 'decisions_manifest_id',
                     'decisions_applied_at', 'decisions_decided_at', 'decisions_id'):
            batch.drop_column(name)
    with op.batch_alter_table('record_images') as batch:
        batch.drop_constraint('check_record_image_rotation_correction', type_='check')
        batch.drop_column('rotation_correction_quarter_turns')
