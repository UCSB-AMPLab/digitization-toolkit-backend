"""add record_status_changes

Revision ID: e4f5a6b7c8d9
Revises: d3e4f5a6b7c8
Create Date: 2026-10-22 12:00:00.000000

One row per change of a record's QA status: from_status, to_status,
changed_by, changed_at. records.status says only where a record is now and
records.modified_at moves with any edit, so until this table nothing said
when a record was approved. Rejections already carry their own time in
record_rejections.rejected_at; this table dates every transition, rejections
included, so a day's reviews can be counted from one place.

No backfill. The only date a backfill could use is modified_at, which dates
the last edit of any kind, and a guessed date in a history table cannot later
be told from a real one. Existing records get their first row at their next
change.

Rows are deleted with their record (ON DELETE CASCADE), as
record_rejections rows are, so a count of a day's reviews leaves out records
deleted since.

Downgrade drops the table and the history in it.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e4f5a6b7c8d9'
down_revision: Union[str, None] = 'd3e4f5a6b7c8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


STATUSES = "('captured','in_review','rejected','approved')"


def upgrade() -> None:
    op.create_table(
        'record_status_changes',
        sa.Column('id', sa.Integer(), primary_key=True, index=True),
        sa.Column('record_id', sa.Integer(), sa.ForeignKey('records.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('from_status', sa.String(length=20), nullable=False),
        sa.Column('to_status', sa.String(length=20), nullable=False),
        sa.Column('changed_by', sa.String(length=255), nullable=True),
        sa.Column('changed_at', sa.DateTime(), server_default=sa.func.now(), nullable=False, index=True),
    )
    op.create_check_constraint(
        'check_record_status_change_from',
        'record_status_changes',
        f"from_status IN {STATUSES}",
    )
    op.create_check_constraint(
        'check_record_status_change_to',
        'record_status_changes',
        f"to_status IN {STATUSES}",
    )


def downgrade() -> None:
    op.drop_table('record_status_changes')
