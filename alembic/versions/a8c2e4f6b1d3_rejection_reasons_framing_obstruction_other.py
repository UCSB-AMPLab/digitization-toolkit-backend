"""add framing, obstruction and other to the rejection reasons

Revision ID: a8c2e4f6b1d3
Revises: d7a1c3e5f9b2
Create Date: 2026-10-24 20:00:00.000000

Widens `check_record_rejection_reason` on `record_rejections` from six
reasons to nine:

- `framing`: a page cut off or badly framed.
- `obstruction`: a finger, weight or object in the shot.
- `other`: anything else. The API refuses `other` without a non-blank
  comment (RecordRejectRequest); the table does not check the comment, since
  rows written before this revision carry none.

Downgrade narrows the constraint back to the six original reasons, and
refuses while any rejection uses one of the three new ones: a rejection is an
audit row, and rewriting its reason to fit the old list would falsify it.
Delete or keep those rows by hand, or stay on this revision.

Written with batch_alter_table so it also runs on SQLite.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a8c2e4f6b1d3'
down_revision: Union[str, None] = 'd7a1c3e5f9b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NAME = 'check_record_rejection_reason'
OLD = "predefined_reason IN ('blur','glare','shadow','focus','exposure','dirt')"
NEW = ("predefined_reason IN ('blur','glare','shadow','focus','exposure','dirt',"
       "'framing','obstruction','other')")


def upgrade() -> None:
    with op.batch_alter_table('record_rejections') as batch:
        batch.drop_constraint(NAME, type_='check')
        batch.create_check_constraint(NAME, NEW)


def downgrade() -> None:
    conn = op.get_bind()
    count = conn.execute(sa.text(
        "SELECT count(*) FROM record_rejections "
        "WHERE predefined_reason IN ('framing','obstruction','other')"
    )).scalar()
    if count:
        raise RuntimeError(
            f"Cannot downgrade: {count} rejection(s) use the reason 'framing', "
            "'obstruction' or 'other', which the earlier revision does not allow. "
            "This downgrade never rewrites a rejection's reason; remove those "
            "rows by hand first, or stay on this revision."
        )
    with op.batch_alter_table('record_rejections') as batch:
        batch.drop_constraint(NAME, type_='check')
        batch.create_check_constraint(NAME, OLD)
