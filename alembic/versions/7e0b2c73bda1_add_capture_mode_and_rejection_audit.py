"""add capture_mode and rejection audit trail (NEH-208)

Revision ID: 7e0b2c73bda1
Revises: f6a7b8c9d0e1
Create Date: 2026-07-28 12:00:00.000000

Part 1 of the NEH-208 review-workflow redesign. This migration:

1. Creates `record_rejections`: one row per rejection event (mandatory
   predefined_reason from the same 6-value list the annotation feature
   already uses, optional free-text comment, who/when).
2. Adds `record_images.is_current` / `superseded_at` / `rejection_id` so a
   rejected image is never deleted or overwritten — it stays in place,
   flagged as superseded once a recapture installs its replacement, and
   stays linked to the rejection that flagged it.
3. Adds `records.capture_mode` ("single" | "dual") and backfills it for
   every existing record by inferring from its images: dual capture always
   writes RecordImage.role in ('left','right') with a shared pair_id
   (cameras.py), so any record with such images is 'dual'; everything else
   (including records with zero images) is 'single'.
4. Drops `records.rejection_note` — superseded by
   record_rejections.predefined_reason/comment, which now carries this
   information instead. Downgrade restores it empty (nullable); the text
   itself is not recoverable, same as this repo's other lossy downgrades.

The second half of the redesign (captured -> in_review data migration and
narrowing the status CHECK constraint) is a separate migration
(b2a1f9c4d3e6) chained after this one, so the judgment-call backfill here
stays isolated from that mechanical rewrite.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7e0b2c73bda1'
down_revision: Union[str, None] = 'f6a7b8c9d0e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Rejection audit table.
    op.create_table(
        'record_rejections',
        sa.Column('id', sa.Integer(), primary_key=True, index=True),
        sa.Column('record_id', sa.Integer(), sa.ForeignKey('records.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('predefined_reason', sa.String(length=20), nullable=False),
        sa.Column('comment', sa.Text(), nullable=True),
        sa.Column('rejected_by', sa.String(length=255), nullable=True),
        sa.Column('rejected_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_check_constraint(
        'check_record_rejection_reason',
        'record_rejections',
        "predefined_reason IN ('blur','glare','shadow','focus','exposure','dirt')",
    )

    # 2. RecordImage audit-trail columns.
    op.add_column('record_images', sa.Column('is_current', sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column('record_images', sa.Column('superseded_at', sa.DateTime(), nullable=True))
    op.add_column('record_images', sa.Column('rejection_id', sa.Integer(), sa.ForeignKey('record_rejections.id', ondelete='SET NULL'), nullable=True))
    op.create_index(op.f('ix_record_images_rejection_id'), 'record_images', ['rejection_id'])

    # 3. capture_mode: add nullable, backfill, then lock down.
    op.add_column('records', sa.Column('capture_mode', sa.String(length=10), nullable=True))
    op.execute("""
        UPDATE records SET capture_mode = 'dual'
        WHERE id IN (
            SELECT DISTINCT record_id FROM record_images
            WHERE role IN ('left', 'right') OR pair_id IS NOT NULL
        )
    """)
    op.execute("UPDATE records SET capture_mode = 'single' WHERE capture_mode IS NULL")
    op.alter_column('records', 'capture_mode', nullable=False)
    op.create_check_constraint(
        'check_record_capture_mode',
        'records',
        "capture_mode IN ('single','dual')",
    )

    # 4. rejection_note is superseded by record_rejections.
    op.drop_column('records', 'rejection_note')


def downgrade() -> None:
    op.add_column('records', sa.Column('rejection_note', sa.Text(), nullable=True))

    op.drop_constraint('check_record_capture_mode', 'records', type_='check')
    op.drop_column('records', 'capture_mode')

    op.drop_index(op.f('ix_record_images_rejection_id'), table_name='record_images')
    op.drop_column('record_images', 'rejection_id')
    op.drop_column('record_images', 'superseded_at')
    op.drop_column('record_images', 'is_current')

    op.drop_constraint('check_record_rejection_reason', 'record_rejections', type_='check')
    op.drop_table('record_rejections')
