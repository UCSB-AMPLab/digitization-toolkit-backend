"""drop records.object_typology and records.material

Removes two descriptive columns the interface never wrote (NEH-259).

What the code establishes. The capture path is the only writer that sets
object_typology to a fixed value: "document" for a single capture
(app/api/cameras.py:798) and "book" for a dual one (:978). Both are restatements
of capture_mode under another name, and the dual value is sometimes wrong -- a
loose bifolium shot as an L+R pair came out as "book". Beyond that, POST
/records/ accepted a client-supplied value for BOTH columns
(app/api/records.py:91-93, app/schemas/record.py:153-155), so the model's wider
vocabulary -- book, dossier, document, map, planimetry, other -- was reachable
through the API. object_typology also had a query filter (records.py:131-132)
and both columns went into the export dict (app/core/export_service.py:151).
Neither name appears anywhere in the frontend source, so no value ever reached
either column from the interface.

What the code cannot establish, and the data must. Whether any row on any unit
holds an object_typology outside {document, book}, or a non-null material. Check
before applying this to a unit that holds real captures:

    SELECT object_typology, count(*) FROM records GROUP BY object_typology;
    SELECT count(*) FROM records WHERE material IS NOT NULL;

A value outside that set is information this migration destroys, and the
rehearsal against a restored appliance database is where that is settled -- see
docs/releases/0.1.0-beta/migration-rehearsal.md in the workspace.

The loose/bound distinction that mattered stays in capture_mode, which records
how an item was shot rather than claiming what it is. Exported bags carry
capture_mode in metadata.json from this revision on, so the preservation package
does not lose the single-vs-dual distinction with the column.

Downgrade restores both columns as 19e2aefe5b17 created them, nullable and
empty. It cannot restore any value, and it cannot be trusted to recompute one
either. Two API writers existed: POST /records/ accepted both columns from the
caller, and RecordUpdate accepted both as well, so PATCH /records/{id} could
edit either one on any row -- including a row the capture path had created. So
the rule "document" where capture_mode = 'single', "book" where it is 'dual'
holds only where the two columns actually agree on the unit in front of you.
Where they disagree, recomputing overwrites a value somebody chose. The
rehearsal gate therefore groups on capture_mode and object_typology together,
not on typology alone; see docs/releases/0.1.0-beta/migration-rehearsal.md.
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
