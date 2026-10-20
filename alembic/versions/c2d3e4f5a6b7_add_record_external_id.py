"""add records.external_id

The identifier of the catalogue entry a record was captured for: Fisqua's
`reference_code`. Nullable, because a record captured without a catalogue entry
has none, and because every row that exists on a unit today is such a record.
Unique, so two records cannot claim the same catalogue entry. Carried by a
single unique index, `ix_records_external_id`, which is also the index the
exchange needs for lookup by code -- Postgres backs a unique constraint with an
index anyway, so declaring both would leave two.

Scope of what this revision does and does not settle. It adds the column; it
does not fill it. No writer exists in the tree: the column is absent from
RecordCreate and RecordUpdate, so neither POST /records/ nor
PATCH /records/{id} can set it, and the capture path does not touch it. The
only way a value arrives is an importer assigning the attribute, and that
importer is not built. Immutability is enforced on the model
(app/models/record.py, Record._external_id_is_write_once) rather than in the
database, because it is a property of a write and not of a row: no CHECK can
compare a new value against the old one. An ORM attribute guard covers every
writer that assigns the attribute, which is all of them; a bulk UPDATE or raw
SQL goes around it.

On upgrade there is nothing to back-fill and nothing to lose: every existing
row gets NULL, and NULLs do not collide under a unique index in Postgres or in
SQLite, so the index builds on a unit of any size without a conflict.

Downgrade drops the index and the column. It destroys any value the column
holds, which on a unit at this revision is none, since nothing writes it. That
stops being true the moment the importer lands, and from then on a dump taken
before the upgrade is the rollback mechanism, as it already is for the status
migration -- see docs/releases/0.1.0-beta/migration-rehearsal.md in the
workspace.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c2d3e4f5a6b7'
down_revision: Union[str, None] = 'b1c2d3e4f5a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('records', sa.Column('external_id', sa.String(length=255), nullable=True))
    op.create_index(op.f('ix_records_external_id'), 'records', ['external_id'], unique=True)


def downgrade() -> None:
    op.drop_index(op.f('ix_records_external_id'), table_name='records')
    op.drop_column('records', 'external_id')
