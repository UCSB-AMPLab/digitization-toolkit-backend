"""keep 'captured' as a record status (NEH-260 supersedes NEH-208 here)

Revision ID: e11b1fbb07e1
Revises: 7e0b2c73bda1
Create Date: 2026-07-28 12:05:00.000000

This revision has been emptied. As written for NEH-208 it mapped every
'captured' record to 'in_review', narrowed the CHECK constraint to three
values and moved the column's server default, on the reading that a record
enters the review queue the instant it is taken.

NEH-260 restores 'captured': a record is the operator's until she promotes the
collection she has finished, and the state machine the Fisqua exchange
contract describes has it. So the collapse must not happen, and 2525f43a4661
already creates the four-value constraint with 'captured' among them while
5e9345333809 already sets the column's server default to 'captured'. Nothing
is left for this revision to do.

It is emptied rather than reversed by a later migration because reversing it
is lossy in one direction: by the time a new migration ran, the records that
were 'captured' could no longer be told from those genuinely in review. On
the appliance in service that is 2,648 rows against one. Ruled 2026-10-17 on
the evidence that no real database has this revision stamped — the one
deployed unit is at 2525f43a4661 — so editing the body costs nothing. The
revision id does not change, so nothing downstream moves.

If a unit turns up having applied the original body, its database holds no
'captured' rows and is inconsistent with this file. It gets rebuilt from a
dump rather than patched: the distinction this revision used to destroy is
not recoverable from the database it left behind.
"""
from typing import Sequence, Union


# revision identifiers, used by Alembic.
revision: str = 'e11b1fbb07e1'
down_revision: Union[str, None] = '7e0b2c73bda1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Deliberately empty — see the module docstring.
    pass


def downgrade() -> None:
    # Deliberately empty. There is nothing to undo, and the four-value
    # constraint is the one 2525f43a4661 creates.
    pass
