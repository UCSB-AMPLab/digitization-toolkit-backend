"""add adoption_requests: the adoptions a client named with its own id, and how each ended

Revision ID: b7d9f1a3c5e8
Revises: c8e0a2f4b6d9
Create Date: 2026-11-24 12:00:00.000000

POST /collections/{source}/adopt-into/{target} may carry
`client_request_id`, a UUID the client mints for one adoption. A row is
written when the adoption takes ADOPTION_LOCK and completed when the
request ends, so a client that lost the answer can ask for it, and a
repeated request is answered from the row (app.core.adoption_requests).
See app/models/adoption_request.py for the columns.

- `request_id` String(36), NOT NULL, unique (`adoption_requests_request_id_key`).
- `source_id`, `target_id` Integer, NULL only on a closed id's row: plain
  ids, no foreign key, since a refused request may name a volume that does
  not exist and a volume adopted away may be deleted afterwards.
- `started_at`, NULL only on a closed id's row; `ended_at` NULL until the
  request ends or the id is closed.
- `outcome` NULL, or `applied`, `refused`, `failed`, `reverted`, `closed`
  (`check_adoption_request_outcome`). `closed`: a lookup answered that the
  unit had no request with the id; no request with it adopts afterwards.
- `status` Integer NULL and `body` JSON NULL: the HTTP status and body the
  request answered; NULL for an outcome startup recovery wrote.

No backfill: adoptions made before this revision carry no client id.

Downgrade drops the table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7d9f1a3c5e8'
down_revision: Union[str, None] = 'c8e0a2f4b6d9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Written out rather than imported from app.models.adoption_request, so that a
# later change to the model does not change what this revision created.
OUTCOME_CHECK = "outcome IS NULL OR outcome IN ('applied', 'refused', 'failed', 'reverted', 'closed')"


def upgrade() -> None:
    op.create_table(
        'adoption_requests',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('request_id', sa.String(length=36), nullable=False),
        sa.Column('source_id', sa.Integer(), nullable=True),
        sa.Column('target_id', sa.Integer(), nullable=True),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('ended_at', sa.DateTime(), nullable=True),
        sa.Column('outcome', sa.String(length=12), nullable=True),
        sa.Column('status', sa.Integer(), nullable=True),
        sa.Column('body', sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('request_id', name='adoption_requests_request_id_key'),
        sa.CheckConstraint(OUTCOME_CHECK, name='check_adoption_request_outcome'),
    )


def downgrade() -> None:
    op.drop_table('adoption_requests')
