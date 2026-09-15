"""migrate 'captured' record status to 'in_review' (NEH-208)

Revision ID: e11b1fbb07e1
Revises: 7e0b2c73bda1
Create Date: 2026-07-28 12:05:00.000000

Part 2 of the NEH-208 review-workflow redesign. "captured" stops being a
persisted/exposed status entirely — a record now enters the queue as
"in_review" the instant it's captured (see app/api/cameras.py), with no
manual promotion step. This maps every existing 'captured' row to
'in_review' and narrows the CHECK constraint accordingly.

Downgrade restores the wider constraint but does not attempt to reverse
the data migration — which rows were 'captured' vs. legitimately already
'in_review' is not recoverable, same reasoning as this repo's other lossy
downgrades (e.g. f6a7b8c9d0e1).
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'e11b1fbb07e1'
down_revision: Union[str, None] = '7e0b2c73bda1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("UPDATE records SET status = 'in_review' WHERE status = 'captured'")
    op.drop_constraint('check_record_status', 'records', type_='check')
    op.create_check_constraint(
        'check_record_status',
        'records',
        "status IN ('in_review','rejected','approved')",
    )
    op.alter_column('records', 'status', server_default='in_review')


def downgrade() -> None:
    op.drop_constraint('check_record_status', 'records', type_='check')
    op.create_check_constraint(
        'check_record_status',
        'records',
        "status IN ('captured','in_review','rejected','approved')",
    )
    op.alter_column('records', 'status', server_default='captured')
