"""add the digitisation queue's assignee and hidden time to collections (Fisqua contract 1.7)

Revision ID: b3d5f7a9c1e2
Revises: 33773ea9f2b8
Create Date: 2026-10-30 18:00:00.000000

The unit polls Fisqua's digitisation queue and applies it to its volumes
(app.core.fisqua_queue). Two nullable columns on `collections`, both set
only by that sync:

- `assignee_username`, String(150) as `users.username`: the unit user the
  volume's queue entry is assigned to. Not a foreign key, since Fisqua may
  name a user deleted on the unit since it last heard of them.
- `queue_hidden_at`, DateTime: when a withdrawn entry took the volume off
  Cola, which happens only while the volume has no records. A later entry
  for the same reference code clears it.

Downgrade drops both; what they held is Fisqua's, sent again with the next
queue revision after an upgrade.

Written with batch_alter_table so it also runs on SQLite.

Version: 0.3.0
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3d5f7a9c1e2'
down_revision: Union[str, None] = '33773ea9f2b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('collections') as batch:
        batch.add_column(sa.Column('assignee_username', sa.String(length=150), nullable=True))
        batch.add_column(sa.Column('queue_hidden_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('collections') as batch:
        batch.drop_column('queue_hidden_at')
        batch.drop_column('assignee_username')
