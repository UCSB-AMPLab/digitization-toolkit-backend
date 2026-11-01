"""records: placeholders for openings never captured, and closing a missing record (Fisqua contract 1.9)

Revision ID: c4e6a8b0d2f4
Revises: e9a1c3d5f7b2
Create Date: 2026-10-31 12:00:00.000000

Contract 1.9 §10 lets the operator, or Fisqua, mark an opening that was
never captured: a placeholder, a dual record with no image whose
`missing_side` is "both". So `check_record_missing_side` takes "both"
beside "left" and "right". Five nullable columns are added to `records`:

- `missing_expected` (Text): what the placeholder should hold, such as the
  folios expected;
- `missing_note` (Text): the operator's or Fisqua's note on it;
- `missing_request_id` (String(64)): the Fisqua request it was made from,
  kept after the placeholder is captured into;
- `closed_reason` (String(32)): why the operator closed the missing record
  without capturing it, one of `foliation_skip`, `leaf_missing_from_volume`,
  `captured_elsewhere` or `other` (`check_record_closed_reason`);
- `closed_note` (Text): her note on closing it.

`closed_reason` may be set only while `missing_side` is
(`check_record_closed_needs_missing`).

Downgrade narrows the side back to "left" and "right" and drops the five
columns. It refuses while any record misses both sides, carries a closed
reason or note, or carries what a placeholder expects, its note or its
request id: the earlier revision cannot hold a placeholder, and dropping
any of those columns would lose what the operator or Fisqua recorded.
Delete or clear those records by hand first, or stay on this revision.

Written with batch_alter_table so it also runs on SQLite.

Version: 0.3.3
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4e6a8b0d2f4'
down_revision: Union[str, None] = 'e9a1c3d5f7b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_SIDE_CHECK = "missing_side IS NULL OR missing_side IN ('left','right')"
SIDE_CHECK = "missing_side IS NULL OR missing_side IN ('left','right','both')"
CLOSED_REASON_CHECK = (
    "closed_reason IS NULL OR closed_reason IN "
    "('foliation_skip','leaf_missing_from_volume','captured_elsewhere','other')"
)
CLOSED_NEEDS_MISSING_CHECK = "closed_reason IS NULL OR missing_side IS NOT NULL"


def upgrade() -> None:
    with op.batch_alter_table('records') as batch:
        batch.drop_constraint('check_record_missing_side', type_='check')
        batch.create_check_constraint('check_record_missing_side', SIDE_CHECK)
        batch.add_column(sa.Column('missing_expected', sa.Text(), nullable=True))
        batch.add_column(sa.Column('missing_note', sa.Text(), nullable=True))
        batch.add_column(sa.Column('missing_request_id', sa.String(length=64), nullable=True))
        batch.add_column(sa.Column('closed_reason', sa.String(length=32), nullable=True))
        batch.add_column(sa.Column('closed_note', sa.Text(), nullable=True))
        batch.create_check_constraint('check_record_closed_reason', CLOSED_REASON_CHECK)
        batch.create_check_constraint('check_record_closed_needs_missing', CLOSED_NEEDS_MISSING_CHECK)


def downgrade() -> None:
    conn = op.get_bind()
    both = conn.execute(sa.text("SELECT count(*) FROM records WHERE missing_side = 'both'")).scalar()
    closed = conn.execute(sa.text(
        "SELECT count(*) FROM records WHERE closed_reason IS NOT NULL OR closed_note IS NOT NULL"
    )).scalar()
    described = conn.execute(sa.text(
        "SELECT count(*) FROM records WHERE missing_expected IS NOT NULL OR missing_note IS NOT NULL "
        "OR missing_request_id IS NOT NULL"
    )).scalar()
    if both or closed or described:
        raise RuntimeError(
            f"Cannot downgrade: {both} record(s) miss both sides (placeholders), {closed} record(s) "
            f"were closed without capturing, and {described} record(s) carry what a placeholder is "
            "expected to hold, a missing note or a Fisqua request id, which the earlier revision cannot "
            "hold. This downgrade never drops them; delete or clear those records by hand first, or "
            "stay on this revision."
        )
    with op.batch_alter_table('records') as batch:
        batch.drop_constraint('check_record_closed_needs_missing', type_='check')
        batch.drop_constraint('check_record_closed_reason', type_='check')
        batch.drop_column('closed_note')
        batch.drop_column('closed_reason')
        batch.drop_column('missing_request_id')
        batch.drop_column('missing_note')
        batch.drop_column('missing_expected')
        batch.drop_constraint('check_record_missing_side', type_='check')
        batch.create_check_constraint('check_record_missing_side', OLD_SIDE_CHECK)
