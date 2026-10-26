"""drop unused collection export tracking columns

Revision ID: f6a7b8c9d0e1
Revises: 2525f43a4661
Create Date: 2026-07-25 19:20:00.000000

Removes collections.export_version / last_exported_at / last_export_hash,
resolving the schema-vs-model drift in NEH-105.

The three columns arrived in dda9bc1bc608 - an unadjusted autogenerate
that also dropped project_members (restored in ff2f751fadbb) - and were
never declared on the Collection model, never read, and never written.
So every row on every unit holds export_version=0 with the other two
NULL: dropping them loses no data.

They are dropped rather than modelled because export state is already
tracked on the filesystem, which is where the live code looks: exports
are named collection_{id}_{YYYYMMDDTHHMMSSZ}.zip and the download
endpoint resolves the most recent one by globbing that pattern. A
last_exported_at column would be a second, staler answer to a question
the filesystem already answers - and NEH-90 will prune those zips,
at which point the DB copy would start lying. When export needs real
persistence (NEH-90's async jobs and pruning; NEH-123/124's per-file
verification) it wants one row per export, not latest-only summary
columns on the parent.

Downgrade restores the columns exactly as dda9bc1bc608 created them.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f6a7b8c9d0e1'
down_revision: Union[str, None] = '2525f43a4661'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column('collections', 'last_export_hash')
    op.drop_column('collections', 'last_exported_at')
    op.drop_column('collections', 'export_version')


def downgrade() -> None:
    op.add_column('collections', sa.Column('export_version', sa.Integer(), server_default=sa.text('0'), nullable=False))
    op.add_column('collections', sa.Column('last_exported_at', sa.DateTime(), nullable=True))
    op.add_column('collections', sa.Column('last_export_hash', sa.String(length=64), nullable=True))
