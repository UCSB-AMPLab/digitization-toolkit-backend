"""drop records.object_typology and records.material

Revision ID: b1c2d3e4f5a6
Revises: a4b5c6d7e8f9
Create Date: 2026-10-15 00:00:00.000000

Removes the two descriptive columns an operator never filled (NEH-259).

object_typology was written only by the capture path, which hard-coded
"document" for a single capture and "book" for a dual one. So it carried no
information capture_mode did not already carry under its own name, and it was
sometimes wrong: a loose bifolium shot as an L+R pair came out as "book".
material had no writer, no UI and no reader, so it is NULL on every row of
every unit.

The loose/bound distinction that mattered stays, in capture_mode, which
records how an item was shot rather than claiming what it is. Exported bags
carry capture_mode in metadata.json from this revision on, so the preservation
package does not lose the single-vs-dual distinction with the column.

Downgrade restores both columns exactly as 19e2aefe5b17 created them, nullable
and empty. It cannot restore object_typology's values, and since every value
was derived from capture_mode, a caller that needs them can recompute them:
"document" where capture_mode = 'single', "book" where it is 'dual'. material
had no values to lose.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b1c2d3e4f5a6'
down_revision: Union[str, None] = 'a4b5c6d7e8f9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column('records', 'material')
    op.drop_column('records', 'object_typology')


def downgrade() -> None:
    op.add_column('records', sa.Column('object_typology', sa.String(length=50), nullable=True))
    op.add_column('records', sa.Column('material', sa.String(length=255), nullable=True))
