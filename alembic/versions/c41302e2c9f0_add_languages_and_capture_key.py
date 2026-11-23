"""The unit's default language, each account's language and the unit's capture key (NEH-336)

Revision ID: c41302e2c9f0
Revises: c8e2a4f6b0d1
Create Date: 2026-11-22 12:00:00.000000

- `unit_settings.default_language` (String(2), NOT NULL, server default
  'es', `check_unit_settings_default_language`: 'es' or 'en'): the
  language of the pages shown before anyone signs in, and the one the
  first account takes.
- `unit_settings.capture_key` (JSON, NULL): the chord that starts a capture
  on the unit, `{key, ctrl, alt, meta, shift}`; NULL for the frontend's
  built-in default.
- `users.language` (String(2), NULL, `check_user_language`: 'es' or 'en'):
  the account's own language, NULL until the user chooses one.

Upgrade gives the existing row 'es' and no capture key, and every existing
account no language, so each is asked for one at its next sign-in.
Downgrade drops the three columns and both CHECKs; the languages and the
capture key stored are lost.

Written with batch_alter_table so it also runs on SQLite, where it rebuilds
both tables; the rebuild of users keeps the partial unique index
`uq_users_one_testing_account`, which is dropped before and created again
after it, since SQLite's reflection does not carry the index's WHERE.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c41302e2c9f0'
down_revision: Union[str, None] = 'c8e2a4f6b0d1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEFAULT_LANGUAGE_CHECK = "default_language IN ('es','en')"
USER_LANGUAGE_CHECK = "language IN ('es','en')"
ONE_TESTING_ACCOUNT_INDEX = 'uq_users_one_testing_account'
ONE_TESTING_ACCOUNT_WHERE = "role = 'testing'"


def _rebuilding_users(change) -> None:
    """Run `change` inside a batch on users, with the partial index dropped around it on SQLite."""
    sqlite = op.get_bind().dialect.name == "sqlite"
    if sqlite:
        op.drop_index(ONE_TESTING_ACCOUNT_INDEX, table_name='users')
    with op.batch_alter_table('users') as batch:
        change(batch)
    if sqlite:
        op.create_index(ONE_TESTING_ACCOUNT_INDEX, 'users', ['role'], unique=True,
                        sqlite_where=sa.text(ONE_TESTING_ACCOUNT_WHERE))


def upgrade() -> None:
    with op.batch_alter_table('unit_settings') as batch:
        batch.add_column(sa.Column('default_language', sa.String(2), server_default='es', nullable=False))
        batch.add_column(sa.Column('capture_key', sa.JSON(), nullable=True))
        batch.create_check_constraint('check_unit_settings_default_language', DEFAULT_LANGUAGE_CHECK)

    def add(batch):
        batch.add_column(sa.Column('language', sa.String(2), nullable=True))
        batch.create_check_constraint('check_user_language', USER_LANGUAGE_CHECK)

    _rebuilding_users(add)


def downgrade() -> None:
    def drop(batch):
        batch.drop_constraint('check_user_language', type_='check')
        batch.drop_column('language')

    _rebuilding_users(drop)

    with op.batch_alter_table('unit_settings') as batch:
        batch.drop_constraint('check_unit_settings_default_language', type_='check')
        batch.drop_column('capture_key')
        batch.drop_column('default_language')
