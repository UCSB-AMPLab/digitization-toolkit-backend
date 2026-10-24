"""add a volume's identity to collections

Exchange contract 1.1 maps each Fisqua item onto a Captua volume, which is a
row of `collections`. This revision gives the volume the columns that mapping
writes to, and nothing that uses them: no stem assignment, no capture naming,
no layout change, no import and no export.

- `filename_stem`: the slug the volume's image file names are built from,
  `<stem>-img_<nnnn>_c<n>`. Nullable, because no existing volume has one and
  nothing generates one: an operator assigns it. Unique, by
  `collections_filename_stem_key`, so two volumes cannot produce the same file
  names. Two CHECKs:
  - `check_collection_filename_stem_length`: at most 27 characters. The
    contract reuses `<stem>-img_<nnnn>` as the pair id, and
    `record_images.pair_id` is String(36); 36 less the nine characters of
    `-img_0000` is 27. The model derives the same number from the column
    (app/models/collection.py, FILENAME_STEM_MAX_LENGTH); a migration is a
    snapshot, so it is written out here. The column itself is String(255),
    so that an overlong stem is refused by this named CHECK on both engines
    rather than by Postgres's varchar limit as a different kind of error.
  - `check_collection_filename_stem_pattern`: a lowercase slug,
    `^[a-z0-9]+(-[a-z0-9]+)*$`. On Postgres a POSIX regex with `~`. SQLite
    has no regex operator unless one is registered, so there the same rule
    is spelled with GLOB: only `[a-z0-9-]`, alphanumeric at both ends, no
    `--`. Any other engine gets no pattern CHECK.
- `external_id`: Fisqua's `referenceCode`. Nullable, unique and indexed by
  one unique index, `ix_collections_external_id`, as `records.external_id` is.
  Written once; that rule is on the model, since no CHECK can compare a new
  value with the stored one.
- `date`, `date_start`, `date_end`: the display date and its queryable range,
  the same columns `records` has, with `check_collection_date_range` refusing
  an end before its start in the form `check_record_date_range` uses.
- `sequence` and `custom_attributes` (Text holding JSON), as on `records`.

Every column applies to every row the same way, nested volumes
(`parent_collection_id`) included.

The stem's rule that it is fixed once the volume has an image is on the
model, for the same reason as external_id's.

Written with batch_alter_table so the revision also runs on SQLite, which
cannot add a constraint to an existing table; on Postgres batch mode emits
plain ALTER TABLE statements and copies nothing.

On upgrade there is nothing to back-fill and nothing to lose: every existing
row gets NULL in every new column, NULLs do not collide under a unique
constraint or index, and every CHECK passes on NULL.

Downgrade drops the constraints, the index and the columns, and with them any
stem, reference code or date range a volume holds. On a unit at this revision
none is written until an operator or the importer writes one; from then on a
dump taken before the upgrade is the rollback mechanism -- see
docs/releases/0.1.0-beta/migration-rehearsal.md in the workspace.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7c1e9d2b4f6'
down_revision: Union[str, None] = 'f5a6b7c8d9e0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


STEM_PATTERN_POSTGRES = "filename_stem IS NULL OR filename_stem ~ '^[a-z0-9]+(-[a-z0-9]+)*$'"
STEM_PATTERN_SQLITE = (
    "filename_stem IS NULL OR ("
    "filename_stem GLOB '[a-z0-9]*' AND filename_stem GLOB '*[a-z0-9]' "
    "AND filename_stem NOT GLOB '*[^a-z0-9-]*' AND filename_stem NOT GLOB '*--*')"
)


def _stem_pattern_check():
    dialect = op.get_bind().dialect.name
    if dialect == 'postgresql':
        return STEM_PATTERN_POSTGRES
    if dialect == 'sqlite':
        return STEM_PATTERN_SQLITE
    return None


def upgrade() -> None:
    pattern = _stem_pattern_check()
    with op.batch_alter_table('collections') as batch:
        batch.add_column(sa.Column('filename_stem', sa.String(length=255), nullable=True))
        batch.add_column(sa.Column('external_id', sa.String(length=255), nullable=True))
        batch.add_column(sa.Column('date', sa.String(length=50), nullable=True))
        batch.add_column(sa.Column('date_start', sa.Date(), nullable=True))
        batch.add_column(sa.Column('date_end', sa.Date(), nullable=True))
        batch.add_column(sa.Column('sequence', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('custom_attributes', sa.Text(), nullable=True))
        batch.create_unique_constraint('collections_filename_stem_key', ['filename_stem'])
        batch.create_index('ix_collections_external_id', ['external_id'], unique=True)
        batch.create_check_constraint(
            'check_collection_date_range',
            'date_start IS NULL OR date_end IS NULL OR date_end >= date_start',
        )
        batch.create_check_constraint(
            'check_collection_filename_stem_length',
            'filename_stem IS NULL OR length(filename_stem) <= 27',
        )
        if pattern is not None:
            batch.create_check_constraint('check_collection_filename_stem_pattern', pattern)


def downgrade() -> None:
    pattern = _stem_pattern_check()
    with op.batch_alter_table('collections') as batch:
        if pattern is not None:
            batch.drop_constraint('check_collection_filename_stem_pattern', type_='check')
        batch.drop_constraint('check_collection_filename_stem_length', type_='check')
        batch.drop_constraint('check_collection_date_range', type_='check')
        batch.drop_index('ix_collections_external_id')
        batch.drop_constraint('collections_filename_stem_key', type_='unique')
        batch.drop_column('custom_attributes')
        batch.drop_column('sequence')
        batch.drop_column('date_end')
        batch.drop_column('date_start')
        batch.drop_column('date')
        batch.drop_column('external_id')
        batch.drop_column('filename_stem')
