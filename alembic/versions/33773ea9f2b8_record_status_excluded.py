"""add excluded to the record statuses, and a note to their history (Fisqua contract 1.6)

Revision ID: 33773ea9f2b8
Revises: 6df85ec6309b
Create Date: 2026-10-30 12:00:00.000000

Fisqua's QC gains a third decision beside approved and rejected: an opening
that is not a page of the volume (a test shot of the rig, a duplicate) is
`excluded`. Fisqua never processes it and asks for no retake; a later
decision of approved or rejected restores it. Captua holds it as a record
status, set only by a Fisqua decision (app.core.fisqua_decisions).

Widens three CHECKs to the five statuses:

- `check_record_status` on `records.status`;
- `check_record_status_change_from` and `check_record_status_change_to` on
  `record_status_changes`, the history row every status change writes.

Adds `record_status_changes.note`, nullable Text: the note of the Fisqua
decision that made the change, kept only for an exclusion (a rejection's
is its RecordRejection comment). It is bounded where it is written, at
REJECTION_COMMENT_MAX_LENGTH characters, as the rejection comment is; the
table does not check it, as it does not check the comment.

Downgrade narrows them back to four and drops the column, and refuses while any record has the
status `excluded` or any history row names it: a record's status is
Fisqua's decision and a history row is an audit row, and rewriting either to
fit the old list would falsify it. Change or remove those rows by hand, or
stay on this revision.

Written with batch_alter_table so it also runs on SQLite, where it rebuilds
both tables; alembic/env.py turns SQLite's foreign keys off around the run,
so the rebuild of `records` fires no ON DELETE action on the tables that
reference it.

Version: 0.2.1
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '33773ea9f2b8'
down_revision: Union[str, None] = '6df85ec6309b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD = "('captured','in_review','rejected','approved')"
NEW = "('captured','in_review','rejected','approved','excluded')"

CHECKS = (
    ('records', 'check_record_status', 'status'),
    ('record_status_changes', 'check_record_status_change_from', 'from_status'),
    ('record_status_changes', 'check_record_status_change_to', 'to_status'),
)


def _replace(statuses: str, note: str) -> None:
    for table in ('records', 'record_status_changes'):
        with op.batch_alter_table(table) as batch:
            for t, name, column in CHECKS:
                if t == table:
                    batch.drop_constraint(name, type_='check')
                    batch.create_check_constraint(name, f"{column} IN {statuses}")
            if table == 'record_status_changes':
                if note == 'add':
                    batch.add_column(sa.Column('note', sa.Text(), nullable=True))
                else:
                    batch.drop_column('note')


def upgrade() -> None:
    _replace(NEW, 'add')


def downgrade() -> None:
    conn = op.get_bind()
    records = conn.execute(sa.text(
        "SELECT count(*) FROM records WHERE status = 'excluded'"
    )).scalar()
    changes = conn.execute(sa.text(
        "SELECT count(*) FROM record_status_changes "
        "WHERE from_status = 'excluded' OR to_status = 'excluded'"
    )).scalar()
    if records or changes:
        raise RuntimeError(
            f"Cannot downgrade: {records} record(s) have the status 'excluded' and "
            f"{changes} status change(s) name it, which the earlier revision does not allow. "
            "This downgrade never rewrites a record's status or its history; change or "
            "remove those rows by hand first, or stay on this revision."
        )
    _replace(OLD, 'drop')
