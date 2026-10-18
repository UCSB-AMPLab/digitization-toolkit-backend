"""add records.date_start and records.date_end

The queryable form of the date `records.date` already holds verbally. `date` is
a free-text display field and stays one -- it is where "1750", "c. 1750" or
"mediados del siglo XVIII" belongs, and none of that can be compared, sorted or
filtered in SQL. Parking a structured range in `custom_attributes` instead
would not help: that column is `Text` holding encoded JSON rather than a `JSON`
column, so a range stored there is not queryable either.

Two `DATE` columns, so the range is a span with day resolution. A document
known only to its year is the whole of that year; one known to its month, the
whole of that month; one dated to a day, a span of one day. What the pair
cannot express is the *precision* of what is known -- "sometime in 1750" and
"throughout 1750" are the same two values -- nor approximation. `date` carries
that, which is why it stays. The shape is the one EAD uses for the same reason:
a human `unitdate` beside a normalised range for machines.

Both columns are nullable, and independently so. "after 1750" is a start with
no end and "before 1800" is an end with no start; both are real archival date
expressions, and refusing a half-open range would make them unstorable. All
four combinations are therefore legal: neither end, one end, or both.

What is refused is an end before its start, by `check_record_date_range`. This
is in the database rather than on the model, which is the opposite of the
choice `c2d3e4f5a6b7` made for `external_id`, and for a reason that does not
apply here: immutability compares a new value against the stored one, and no
CHECK can see the old row, so it had to be an ORM attribute guard. An ordering
rule compares two columns of the same row, which is exactly what a CHECK does
-- so it also holds against `query().update()` and raw SQL, the paths an ORM
guard misses, and which the importer that will fill these columns is the writer
most likely to use.

The constraint is written `date_start IS NULL OR date_end IS NULL OR
date_end >= date_start`. The two NULL clauses are for a reader and not for the
database: a bare `date_end >= date_start` evaluates to NULL when either end is
NULL, and a CHECK accepts NULL, so the two forms behave identically and no test
can tell them apart -- which was confirmed by mutating the model to the bare
form and watching the suite stay green. `>=` rather than `>` is a real
difference, because a single-day document has a start equal to its end.

On upgrade there is nothing to back-fill and nothing to lose: every existing
row gets NULL in both columns, and NULL in either end satisfies the check, so
it validates against a unit of any size without a conflict and without
touching `date`.

Downgrade drops the check and both columns. It destroys any range they hold,
which on a unit at this revision is none, since nothing writes them -- the
columns are absent from RecordCreate and RecordUpdate, so neither
POST /records/ nor PATCH /records/{id} can set them, and the capture path does
not touch them. That stops being true the moment a writer lands, and from then
on a dump taken before the upgrade is the rollback mechanism, as it already is
for the status migration -- see
docs/releases/0.1.0-beta/migration-rehearsal.md in the workspace. `date` is
untouched either way, so the verbal date survives a rollback regardless.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd3e4f5a6b7c8'
down_revision: Union[str, None] = 'c2d3e4f5a6b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('records', sa.Column('date_start', sa.Date(), nullable=True))
    op.add_column('records', sa.Column('date_end', sa.Date(), nullable=True))
    op.create_check_constraint(
        'check_record_date_range',
        'records',
        'date_start IS NULL OR date_end IS NULL OR date_end >= date_start',
    )


def downgrade() -> None:
    op.drop_constraint('check_record_date_range', 'records', type_='check')
    op.drop_column('records', 'date_end')
    op.drop_column('records', 'date_start')
