"""add fisqua_outbox.reference_code: the reference each manifest was sent under

Revision ID: 6df85ec6309b
Revises: a5f0306a8f8c
Create Date: 2026-10-25 18:00:00.000000

Status and QC decisions are asked for by a volume's reference code
(app.core.fisqua_outbox.volumes_to_poll). Taken from the volume as it is
now, a volume sent under its stem (no external_id recorded) and given an
external_id afterwards would be asked for under a reference Fisqua does not
know, and the status and decisions for the manifest it holds would be lost.
`reference_code` keeps the reference the manifest carried (its
`volumes[0].externalId`), written when the manifest is built; the poll asks
by it.

Backfilled from each row's stored manifest; a row with no manifest (not yet
built, or refused before it was) has none. Added with a plain ADD COLUMN,
which rebuilds no table on SQLite. Downgrade drops the column, rebuilding
fisqua_outbox on SQLite; no table references fisqua_outbox, so the rebuild
fires no foreign key action.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6df85ec6309b'
down_revision: Union[str, None] = 'a5f0306a8f8c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _reference(manifest):
    volumes = manifest.get('volumes') if isinstance(manifest, dict) else None
    if not volumes or not isinstance(volumes[0], dict):
        return None
    ref = volumes[0].get('externalId')
    return ref[:255] if isinstance(ref, str) and ref else None


def upgrade() -> None:
    op.add_column('fisqua_outbox', sa.Column('reference_code', sa.String(length=255), nullable=True))
    outbox = sa.table('fisqua_outbox', sa.column('id', sa.Integer), sa.column('manifest', sa.JSON),
                      sa.column('reference_code', sa.String))
    bind = op.get_bind()
    rows = bind.execute(sa.select(outbox.c.id, outbox.c.manifest).where(outbox.c.manifest.isnot(None))).all()
    for row_id, manifest in rows:
        ref = _reference(manifest)
        if ref is not None:
            bind.execute(outbox.update().where(outbox.c.id == row_id).values(reference_code=ref))


def downgrade() -> None:
    with op.batch_alter_table('fisqua_outbox') as batch:
        batch.drop_column('reference_code')
