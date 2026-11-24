"""add capture_requests: the captures a client named with its own id, and what each saved

Revision ID: d2b4f6a8c0e3
Revises: c41302e2c9f0
Create Date: 2026-11-22 12:00:00.000000

A capture request may carry `client_capture_id`, a UUID the client mints
for one capture. A row here is written in the capture's own transaction,
so it exists exactly when the capture was saved, and a repeated request
with the same id is answered from it without reaching the camera
(app.core.capture_requests). See app/models/capture_request.py for the
columns.

- `client_capture_id` String(36), NOT NULL; `space` String(10), NOT NULL,
  `real` or `testing` (`check_capture_request_space`). Unique together
  (`capture_requests_client_capture_id_space_key`): each space's ids are
  its own.
- `kind` `single` or `dual` (`check_capture_request_kind`).
- `collection_id`: nullable FK to collections, ON DELETE SET NULL, as a
  record's volume is (`capture_requests_collection_id_fkey`).
- `record_id`: nullable FK to records, ON DELETE SET NULL
  (`capture_requests_record_id_fkey`), indexed
  (`ix_capture_requests_record_id`): a stored id outlives its record.
- `image_ids` JSON, NOT NULL; `request_fingerprint` String(64), NOT NULL;
  `created_at` NOT NULL.

No backfill: captures saved before this revision carry no client id.

Downgrade drops the table: a request repeated after it photographs again,
as every request did before this revision.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd2b4f6a8c0e3'
down_revision: Union[str, None] = 'c41302e2c9f0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Written out rather than imported from app.models.capture_request, so that a
# later change to the model does not change what this revision created.
KIND_CHECK = "kind IN ('single', 'dual')"
SPACE_CHECK = "space IN ('real', 'testing')"


def upgrade() -> None:
    op.create_table(
        'capture_requests',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('client_capture_id', sa.String(length=36), nullable=False),
        sa.Column('space', sa.String(length=10), nullable=False),
        sa.Column('kind', sa.String(length=10), nullable=False),
        sa.Column('collection_id', sa.Integer(), nullable=True),
        sa.Column('record_id', sa.Integer(), nullable=True),
        sa.Column('image_ids', sa.JSON(), nullable=False),
        sa.Column('request_fingerprint', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['collection_id'], ['collections.id'], ondelete='SET NULL',
                                name='capture_requests_collection_id_fkey'),
        sa.ForeignKeyConstraint(['record_id'], ['records.id'], ondelete='SET NULL',
                                name='capture_requests_record_id_fkey'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('client_capture_id', 'space', name='capture_requests_client_capture_id_space_key'),
        sa.CheckConstraint(KIND_CHECK, name='check_capture_request_kind'),
        sa.CheckConstraint(SPACE_CHECK, name='check_capture_request_space'),
    )
    op.create_index(op.f('ix_capture_requests_record_id'), 'capture_requests', ['record_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_capture_requests_record_id'), table_name='capture_requests')
    op.drop_table('capture_requests')
