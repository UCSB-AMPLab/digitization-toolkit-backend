"""The testing space: testing projects and volumes, the testing role, the unit's testing settings (NEH-391)

Revision ID: c8e2a4f6b0d1
Revises: b6d8f0a2c4e7
Create Date: 2026-11-19 12:00:00.000000

A unit may hold testing work beside its real work, while it stays linked
to Fisqua. A testing volume is always kept on the unit: none of its images
is copied offsite and none of its manifests is sent to Fisqua, and it is
never released into real work (app.core.testing_space).

- `projects.testing`, `collections.testing` (Boolean, NOT NULL, server
  default false): set at insert and never changed; a volume's is its
  project's, or its parent's (app.models.collection).
- `projects.capture_format` (String(16), NULL): the capture format of a
  testing project, "JPEG" or "RAW+JPEG", and NULL on an ordinary project
  (`check_project_capture_format`); every capture into the project's
  volumes takes it (app.api.cameras).
- `check_collection_testing_kept` on collections: `NOT testing OR
  kept_on_unit`, so a testing volume is kept on the unit whatever writes it.
- `collections.testing_expires_at` (DateTime, NULL): when a testing volume
  is due to be removed.
- `check_user_role` gains `testing`, and `uq_users_one_testing_account`, a
  unique index on `users.role` where the role is `testing`, allows at most
  one account in it.
- On `unit_settings`: `testing_enabled` (Boolean, NOT NULL, default false),
  `testing_user_id` (nullable FK to users, ON DELETE SET NULL, named
  `unit_settings_testing_user_id_fkey`) and `testing_expiry_days` (Integer,
  NOT NULL, default 30, `check_unit_settings_testing_expiry_days`: at
  least 1) and `testing_reserve_gb` (Integer, NOT NULL, default 20,
  `check_unit_settings_testing_reserve_gb`: 1 to 10000), the free space in
  GB kept for real work, below which testing captures are refused.
- `users.token_version` (Integer, NOT NULL, server default 0): the
  account's session generation. Every token carries it as `ver`, and a
  token whose `ver` differs is refused (app.api.auth); a token with no
  `ver` counts as 0, so the sessions open when a unit takes this revision
  keep working.

Upgrade marks nothing as testing: every existing project and volume is
real work, no account has the role, and the space is off. Testing work
has a namespace of its own (app.core.project_names.TESTING_PREFIX), which
no real project's name or directory name, and no real volume's name,
file name stem or reference code, may lie in; the upgrade refuses, naming the rows,
while any does, before it changes anything. A testing manifest's space is
read from its directory's name, so the upgrade also refuses while a
projects root holds a top-level directory, or `.retired-manifests/` a
file, whose name lies in the namespace. The roots are the application's
(the active one and the default one, from its settings); a root that does
not exist is passed over, and one that cannot be read stops the upgrade. No schema holds the prefix:
the models check it (app.models.project, app.models.collection).

Downgrade refuses while any project or volume is testing or any account
has the testing role: the earlier revision would read a testing volume as
real work and copy it offsite and send it, and its role CHECK has no
`testing`. Remove those rows first, or stay on this revision. Otherwise it
drops the columns (`token_version`, `testing_reserve_gb` and `capture_format` among them), the CHECKs, the index and
the foreign key, and narrows the role CHECK back.

Written with batch_alter_table so it also runs on SQLite, where it rebuilds
the four tables; alembic/env.py turns SQLite's foreign keys off around the
run, so the rebuilds fire no ON DELETE action of the tables that reference
them.

Version: 0.3.3
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c8e2a4f6b0d1'
down_revision: Union[str, None] = 'b6d8f0a2c4e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_ROLE_CHECK = "role IN ('admin','operator','reviewer')"
NEW_ROLE_CHECK = "role IN ('admin','operator','reviewer','testing')"
TESTING_KEPT_CHECK = "NOT testing OR kept_on_unit"
CAPTURE_FORMAT_CHECK = (
    "(testing AND capture_format IS NOT NULL AND capture_format IN ('JPEG','RAW+JPEG')) OR (NOT testing AND capture_format IS NULL)")
EXPIRY_CHECK = "testing_expiry_days >= 1"
RESERVE_CHECK = "testing_reserve_gb >= 1 AND testing_reserve_gb <= 10000"
ONE_TESTING_ACCOUNT_INDEX = 'uq_users_one_testing_account'
ONE_TESTING_ACCOUNT_WHERE = "role = 'testing'"
TESTING_USER_FK = 'unit_settings_testing_user_id_fkey'


def _rows_in_the_testing_namespace(conn) -> list:
    """The projects and volumes whose name, directory name, stem or reference code lies in the testing namespace."""
    from app.core.project_names import in_reserved_namespace

    found = []
    for row_id, name in conn.execute(sa.text("SELECT id, name FROM projects ORDER BY id")):
        if in_reserved_namespace(name):
            found.append(f"project {row_id} (name {name!r})")
    columns = {c["name"] for c in sa.inspect(conn).get_columns("collections")}
    # Every volume is real work before this revision, so its name is checked too.
    for field in ("name", "filename_stem", "external_id"):
        if field not in columns:
            continue
        for row_id, value in conn.execute(sa.text(
                f"SELECT id, {field} FROM collections WHERE {field} IS NOT NULL ORDER BY id")):
            if in_reserved_namespace(value):
                found.append(f"volume {row_id} ({field} {value!r})")
    return found


def _projects_roots() -> list:
    """The projects roots the application reads: the active one (a storage override's, when set) and the default one.

    Read from the application's settings, as alembic/env.py imports them,
    so the migration looks where the running unit keeps its files.
    """
    from app.core.config import settings

    roots = []
    for root in (settings.projects_dir, settings.default_projects_dir):
        if root not in roots:
            roots.append(root)
    return roots


def _paths_in_the_testing_namespace() -> list:
    """The directories directly under a projects root, and the retired manifests, whose names lie in the testing namespace.

    A root that does not exist holds nothing yet, as on a fresh install,
    and is passed over. A root that exists but cannot be listed cannot be
    checked: RuntimeError, so the upgrade stops rather than leave a real
    manifest there to be read as testing work afterwards.
    """
    from app.core.integrity import RETIRED_MANIFESTS_DIR
    from app.core.project_names import in_reserved_namespace

    found = []
    for root in _projects_roots():
        try:
            if not root.exists():
                continue
            for entry in sorted(root.iterdir()):
                if entry.is_dir() and in_reserved_namespace(entry.name):
                    found.append(f"directory {entry}")
            retired = root / RETIRED_MANIFESTS_DIR
            if retired.is_dir():
                for entry in sorted(retired.iterdir()):
                    if in_reserved_namespace(entry.name):
                        found.append(f"retired manifest {entry}")
        except OSError as exc:
            raise RuntimeError(
                f"Cannot upgrade: the projects root {root} cannot be read ({exc}), so Captua cannot check "
                "that no directory or retired manifest in it lies in the namespace kept for the testing "
                "space. Make it readable, then upgrade."
            ) from exc
    return found


def upgrade() -> None:
    found = _rows_in_the_testing_namespace(op.get_bind()) + _paths_in_the_testing_namespace()
    if found:
        raise RuntimeError(
            "Cannot upgrade: these rows lie in the namespace kept for the testing space, which real work "
            "never uses: " + "; ".join(found) + ". Rename or move them first, then upgrade."
        )
    with op.batch_alter_table('users') as batch:
        batch.drop_constraint('check_user_role', type_='check')
        batch.create_check_constraint('check_user_role', NEW_ROLE_CHECK)
        batch.add_column(sa.Column('token_version', sa.Integer(), server_default=sa.text('0'), nullable=False))
    op.create_index(ONE_TESTING_ACCOUNT_INDEX, 'users', ['role'], unique=True,
                    postgresql_where=sa.text(ONE_TESTING_ACCOUNT_WHERE),
                    sqlite_where=sa.text(ONE_TESTING_ACCOUNT_WHERE))

    with op.batch_alter_table('projects') as batch:
        batch.add_column(sa.Column('testing', sa.Boolean(), server_default=sa.false(), nullable=False))
        batch.add_column(sa.Column('capture_format', sa.String(16), nullable=True))
        batch.create_check_constraint('check_project_capture_format', CAPTURE_FORMAT_CHECK)

    with op.batch_alter_table('collections') as batch:
        batch.add_column(sa.Column('testing', sa.Boolean(), server_default=sa.false(), nullable=False))
        batch.add_column(sa.Column('testing_expires_at', sa.DateTime(), nullable=True))
        batch.create_check_constraint('check_collection_testing_kept', TESTING_KEPT_CHECK)

    with op.batch_alter_table('unit_settings') as batch:
        batch.add_column(sa.Column('testing_enabled', sa.Boolean(), server_default=sa.false(), nullable=False))
        batch.add_column(sa.Column('testing_user_id', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('testing_expiry_days', sa.Integer(), server_default=sa.text('30'),
                                   nullable=False))
        batch.add_column(sa.Column('testing_reserve_gb', sa.Integer(), server_default=sa.text('20'),
                                   nullable=False))
        batch.create_foreign_key(TESTING_USER_FK, 'users', ['testing_user_id'], ['id'], ondelete='SET NULL')
        batch.create_check_constraint('check_unit_settings_testing_expiry_days', EXPIRY_CHECK)
        batch.create_check_constraint('check_unit_settings_testing_reserve_gb', RESERVE_CHECK)


def downgrade() -> None:
    conn = op.get_bind()
    projects = conn.execute(sa.text("SELECT count(*) FROM projects WHERE testing")).scalar()
    volumes = conn.execute(sa.text("SELECT count(*) FROM collections WHERE testing")).scalar()
    accounts = conn.execute(sa.text("SELECT count(*) FROM users WHERE role = 'testing'")).scalar()
    if projects or volumes or accounts:
        raise RuntimeError(
            f"Cannot downgrade: {projects} testing project(s), {volumes} testing volume(s) and "
            f"{accounts} account(s) with the testing role exist. The earlier revision would read "
            "the testing volumes as real work, and allows no testing role. Remove them first, or "
            "stay on this revision."
        )

    with op.batch_alter_table('unit_settings') as batch:
        batch.drop_constraint('check_unit_settings_testing_reserve_gb', type_='check')
        batch.drop_constraint('check_unit_settings_testing_expiry_days', type_='check')
        batch.drop_constraint(TESTING_USER_FK, type_='foreignkey')
        batch.drop_column('testing_reserve_gb')
        batch.drop_column('testing_expiry_days')
        batch.drop_column('testing_user_id')
        batch.drop_column('testing_enabled')

    with op.batch_alter_table('collections') as batch:
        batch.drop_constraint('check_collection_testing_kept', type_='check')
        batch.drop_column('testing_expires_at')
        batch.drop_column('testing')

    with op.batch_alter_table('projects') as batch:
        batch.drop_constraint('check_project_capture_format', type_='check')
        batch.drop_column('capture_format')
        batch.drop_column('testing')

    op.drop_index(ONE_TESTING_ACCOUNT_INDEX, table_name='users')
    with op.batch_alter_table('users') as batch:
        batch.drop_column('token_version')
        batch.drop_constraint('check_user_role', type_='check')
        batch.create_check_constraint('check_user_role', OLD_ROLE_CHECK)
