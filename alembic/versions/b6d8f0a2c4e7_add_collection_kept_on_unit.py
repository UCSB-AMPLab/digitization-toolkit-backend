"""collections.kept_on_unit: volumes Captua keeps on the unit (NEH-320)

Revision ID: b6d8f0a2c4e7
Revises: d7f9a1c3e5b2
Create Date: 2026-11-07 12:00:00.000000

Adds `kept_on_unit` (Boolean, NOT NULL, server default false) to
`collections`. While it is true, Captua copies none of the volume's images
offsite and promotes and sends none of its Fisqua manifests; they wait on
the unit (app.core.kept_volumes). A volume created in Scanner only
(`offline`) mode is created with it set; an admin may set it on a new
volume in any mode, or on an existing one that has nothing offsite yet, and
may clear it.

Upgrade marks no existing volume: every row gets false, so every volume
made before this revision is copied and sent as before.

Downgrade drops the column, and with it the decision: the earlier revision
copies and sends every volume.

Version: 0.3.3
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b6d8f0a2c4e7'
down_revision: Union[str, None] = 'd7f9a1c3e5b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('collections') as batch:
        batch.add_column(sa.Column('kept_on_unit', sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('collections') as batch:
        batch.drop_column('kept_on_unit')
