"""add fisqua_outbox and fisqua_status: manifests to deliver, and Fisqua's latest word on each volume

Revision ID: 9b7087bd0bc5
Revises: a8c2e4f6b1d3
Create Date: 2026-10-24 18:00:00.000000

`fisqua_outbox` has one row per capture manifest the unit is to send
Fisqua: one per handover of a volume while Fisqua is configured, built once
the volume is received in the bucket (app.core.fisqua_outbox). `fisqua_status`
has, per volume, the latest status Fisqua gave for its latest delivered
manifest. See app/models/fisqua.py for the states and for why the status is
a table of its own rather than columns on collections.

Both reference collections with ON DELETE CASCADE: a volume can be deleted
only when it is empty, and its manifests go with it.

No backfill: volumes handed over before this revision have no row, and are
sent only when handed over again.

Downgrade drops both tables, and with them every manifest queued or sent
and every status received. Nothing is changed on Fisqua's side.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9b7087bd0bc5'
down_revision: Union[str, None] = 'a8c2e4f6b1d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Written out rather than imported from app.models.fisqua, so that a later
# change to the model does not change what this revision created.
_STATES = "'waiting_for_upload', 'ready', 'sending', 'delivered', 'refused', 'failed', 'superseded', 'withdrawn'"


def upgrade() -> None:
    op.create_table(
        'fisqua_outbox',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('collection_id', sa.Integer(), nullable=False),
        sa.Column('manifest_id', sa.String(length=36), nullable=False),
        sa.Column('stem', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('state', sa.String(length=20), nullable=False),
        sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
        sa.Column('next_attempt_at', sa.DateTime(), nullable=True),
        sa.Column('last_attempt_at', sa.DateTime(), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('refusal', sa.String(length=20), nullable=True),
        sa.Column('problems', sa.JSON(), nullable=True),
        sa.Column('manifest', sa.JSON(), nullable=True),
        sa.Column('response', sa.JSON(), nullable=True),
        sa.Column('external_id', sa.String(length=255), nullable=True),
        sa.Column('delivered_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['collection_id'], ['collections.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('manifest_id', name='fisqua_outbox_manifest_id_key'),
        sa.CheckConstraint(f"state IN ({_STATES})", name='check_fisqua_outbox_state'),
        sa.CheckConstraint(
            "(state = 'refused' AND refusal IS NOT NULL AND refusal IN ('not_exportable', 'fisqua')) OR "
            "(state <> 'refused' AND refusal IS NULL)",
            name='check_fisqua_outbox_refusal'),
        sa.CheckConstraint(
            "(state = 'delivered' AND delivered_at IS NOT NULL) OR "
            "(state <> 'delivered' AND delivered_at IS NULL)",
            name='check_fisqua_outbox_delivered'),
        sa.CheckConstraint('attempts >= 0', name='check_fisqua_outbox_attempts'),
    )
    op.create_index('ix_fisqua_outbox_id', 'fisqua_outbox', ['id'], unique=False)
    op.create_index('ix_fisqua_outbox_collection_id', 'fisqua_outbox', ['collection_id'], unique=False)

    op.create_table(
        'fisqua_status',
        sa.Column('collection_id', sa.Integer(), nullable=False),
        sa.Column('manifest_id', sa.String(length=36), nullable=True),
        sa.Column('status', sa.Text(), nullable=True),
        sa.Column('status_at', sa.DateTime(), nullable=True),
        sa.Column('status_checked_at', sa.DateTime(), nullable=False),
        sa.Column('problems', sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(['collection_id'], ['collections.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('collection_id'),
    )


def downgrade() -> None:
    op.drop_table('fisqua_status')
    op.drop_index('ix_fisqua_outbox_collection_id', table_name='fisqua_outbox')
    op.drop_index('ix_fisqua_outbox_id', table_name='fisqua_outbox')
    op.drop_table('fisqua_outbox')
