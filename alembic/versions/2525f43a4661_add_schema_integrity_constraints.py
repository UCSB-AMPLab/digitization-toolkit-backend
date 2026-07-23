"""add schema integrity constraints

Revision ID: 2525f43a4661
Revises: 1a5f017816fd
Create Date: 2026-07-23 05:09:00.169388

Schema-hygiene batch covering four concerns:
  1. Missing indexes on records.project_id / records.collection_id
     (NEH-109).
  2. UNIQUE constraint on exif_data.record_image_id - enforces the
     documented one-to-one relationship between a RecordImage and its
     EXIF row (NEH-110). No dedupe logic - if duplicates exist the
     migration fails loudly by design.
  3. ondelete='CASCADE' on the camera_settings/exif_data ->
     record_images foreign keys, so deleting a RecordImage cleans up
     its children instead of orphaning rows or failing (NEH-111).
  4. CHECK constraints pinning the enum-like status/role/level/category
     columns to their documented vocabularies (NEH-112). Note:
     record_images.role is deliberately left unconstrained - capture
     code writes open-ended values (cam0/cam1/..., {role}_raw).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2525f43a4661'
down_revision: Union[str, None] = '1a5f017816fd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Missing indexes
    op.create_index(op.f('ix_records_project_id'), 'records', ['project_id'], unique=False)
    op.create_index(op.f('ix_records_collection_id'), 'records', ['collection_id'], unique=False)

    # 2. exif_data <-> record_images is documented as one-to-one; enforce it.
    # No dedupe logic - if duplicates exist the migration fails loudly by design.
    op.create_unique_constraint('exif_data_record_image_id_key', 'exif_data', ['record_image_id'])

    # 3. ondelete=CASCADE on the record_images children. Constraint names
    # match those established by migration c3d4e5f6a7b8 (verified current).
    op.drop_constraint('camera_settings_record_image_id_fkey', 'camera_settings', type_='foreignkey')
    op.create_foreign_key('camera_settings_record_image_id_fkey', 'camera_settings', 'record_images', ['record_image_id'], ['id'], ondelete='CASCADE')
    op.drop_constraint('exif_data_record_image_id_fkey', 'exif_data', type_='foreignkey')
    op.create_foreign_key('exif_data_record_image_id_fkey', 'exif_data', 'record_images', ['record_image_id'], ['id'], ondelete='CASCADE')

    # 4. CHECK constraints on the enum-like columns.
    op.create_check_constraint('check_record_status', 'records', "status IN ('captured','in_review','rejected','approved')")
    op.create_check_constraint('check_user_role', 'users', "role IN ('admin','operator','reviewer')")
    op.create_check_constraint('check_project_member_role', 'project_members', "role IN ('operator','reviewer')")
    op.create_check_constraint('check_system_log_level', 'system_logs', "level IN ('INFO','WARN','ERR')")
    op.create_check_constraint('check_system_log_category', 'system_logs', "category IN ('access','activity','capture','system')")


def downgrade() -> None:
    # 4. Drop CHECK constraints (reverse order).
    op.drop_constraint('check_system_log_category', 'system_logs', type_='check')
    op.drop_constraint('check_system_log_level', 'system_logs', type_='check')
    op.drop_constraint('check_project_member_role', 'project_members', type_='check')
    # Migration e5f6a7b8c9d0's downgrade re-introduces the 'contributor'
    # role value, so this CHECK must be dropped before the chain ever
    # reaches that revision. Alembic's chain order guarantees that (this
    # comment is for humans doing partial/manual downgrades).
    op.drop_constraint('check_user_role', 'users', type_='check')
    op.drop_constraint('check_record_status', 'records', type_='check')

    # 3. Restore the FKs without ondelete.
    op.drop_constraint('exif_data_record_image_id_fkey', 'exif_data', type_='foreignkey')
    op.create_foreign_key('exif_data_record_image_id_fkey', 'exif_data', 'record_images', ['record_image_id'], ['id'])
    op.drop_constraint('camera_settings_record_image_id_fkey', 'camera_settings', type_='foreignkey')
    op.create_foreign_key('camera_settings_record_image_id_fkey', 'camera_settings', 'record_images', ['record_image_id'], ['id'])

    # 2. Drop the UNIQUE constraint.
    op.drop_constraint('exif_data_record_image_id_key', 'exif_data', type_='unique')

    # 1. Drop the indexes.
    op.drop_index(op.f('ix_records_collection_id'), table_name='records')
    op.drop_index(op.f('ix_records_project_id'), table_name='records')
