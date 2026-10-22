"""keep the status history of deleted records

Revision ID: f5a6b7c8d9e0
Revises: e4f5a6b7c8d9
Create Date: 2026-10-22 18:00:00.000000

e4f5a6b7c8d9 created record_status_changes with ON DELETE CASCADE, so
deleting a record deleted its history and a count of a day's reviews lost
the work done on it. The history is kept instead: record_id becomes
nullable and the foreign key becomes ON DELETE SET NULL. A row of a deleted
record still says what changed, who changed it and when; it no longer says
which record.

A row with no record would also have no collection, so no per-collection
count could include it. A new collection_id column stores the collection the
record was in at each change. Existing rows take their record's current
collection, which is where they are listed today.

A new revision rather than an edit of e4f5a6b7c8d9, because that revision is
already applied on the bench unit.

Downgrade drops collection_id, deletes the rows whose record is gone, since
a NOT NULL CASCADE column cannot hold them, and restores the constraint
e4f5a6b7c8d9 created.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f5a6b7c8d9e0'
down_revision: Union[str, None] = 'e4f5a6b7c8d9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


FK = 'record_status_changes_record_id_fkey'


def upgrade() -> None:
    op.drop_constraint(FK, 'record_status_changes', type_='foreignkey')
    op.alter_column('record_status_changes', 'record_id', existing_type=sa.Integer(), nullable=True)
    op.create_foreign_key(FK, 'record_status_changes', 'records', ['record_id'], ['id'], ondelete='SET NULL')

    op.add_column('record_status_changes', sa.Column(
        'collection_id', sa.Integer(),
        sa.ForeignKey('collections.id', ondelete='SET NULL'), nullable=True))
    op.create_index(op.f('ix_record_status_changes_collection_id'), 'record_status_changes', ['collection_id'])
    op.execute("""
        UPDATE record_status_changes AS h SET collection_id = r.collection_id
        FROM records AS r WHERE r.id = h.record_id
    """)


def downgrade() -> None:
    op.drop_index(op.f('ix_record_status_changes_collection_id'), table_name='record_status_changes')
    op.drop_column('record_status_changes', 'collection_id')
    op.execute("DELETE FROM record_status_changes WHERE record_id IS NULL")
    op.drop_constraint(FK, 'record_status_changes', type_='foreignkey')
    op.alter_column('record_status_changes', 'record_id', existing_type=sa.Integer(), nullable=False)
    op.create_foreign_key(FK, 'record_status_changes', 'records', ['record_id'], ['id'], ondelete='CASCADE')
