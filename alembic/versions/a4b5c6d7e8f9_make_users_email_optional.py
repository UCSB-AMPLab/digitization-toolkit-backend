"""make users.email optional (NEH-162)

Revision ID: a4b5c6d7e8f9
Revises: e11b1fbb07e1
Create Date: 2026-09-09 00:00:00.000000

The appliance runs offline and nothing verifies an address, so the email
stays as a field on users but stops being required. Downgrade refuses
when any row already has a NULL email rather than inventing addresses to
make the NOT NULL constraint pass again.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a4b5c6d7e8f9'
down_revision: Union[str, None] = 'e11b1fbb07e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('users', 'email', existing_type=sa.String(255), nullable=True)


def downgrade() -> None:
    conn = op.get_bind()
    null_count = conn.execute(sa.text("SELECT count(*) FROM users WHERE email IS NULL")).scalar()
    if null_count:
        raise RuntimeError(
            f"Cannot downgrade: {null_count} user(s) have a NULL email. "
            "This downgrade never invents addresses to satisfy a NOT NULL "
            "constraint; give every user a real email first, or stay on "
            "this migration."
        )
    op.alter_column('users', 'email', existing_type=sa.String(255), nullable=False)
