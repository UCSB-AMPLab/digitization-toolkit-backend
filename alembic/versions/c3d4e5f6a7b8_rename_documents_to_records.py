"""rename documents to records

Revision ID: c3d4e5f6a7b8
Revises: b7c8d9e0f1a2
Create Date: 2026-02-16 19:36:06.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, None] = 'b7c8d9e0f1a2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Rename the document_images table to record_images
    op.rename_table('document_images', 'record_images')
    
    # Rename the foreign key column in camera_settings
    op.alter_column('camera_settings', 'document_image_id',
                    new_column_name='record_image_id',
                    existing_type=sa.Integer(),
                    existing_nullable=False)
    
    # Rename the foreign key column in exif_data
    op.alter_column('exif_data', 'document_image_id',
                    new_column_name='record_image_id',
                    existing_type=sa.Integer(),
                    existing_nullable=False)
    
    # Update the foreign key constraint in camera_settings
    # Drop old foreign key constraint
    op.drop_constraint('camera_settings_document_image_id_fkey', 'camera_settings', type_='foreignkey')
    # Create new foreign key constraint
    op.create_foreign_key('camera_settings_record_image_id_fkey',
                         'camera_settings', 'record_images',
                         ['record_image_id'], ['id'])
    
    # Update the foreign key constraint in exif_data
    # Drop old foreign key constraint
    op.drop_constraint('exif_data_document_image_id_fkey', 'exif_data', type_='foreignkey')
    # Create new foreign key constraint
    op.create_foreign_key('exif_data_record_image_id_fkey',
                         'exif_data', 'record_images',
                         ['record_image_id'], ['id'])
    
    # Rename indexes
    op.execute('ALTER INDEX ix_document_images_id RENAME TO ix_record_images_id')
    op.execute('ALTER INDEX ix_document_images_filename RENAME TO ix_record_images_filename')


def downgrade() -> None:
    # Reverse the index renames
    op.execute('ALTER INDEX ix_record_images_filename RENAME TO ix_document_images_filename')
    op.execute('ALTER INDEX ix_record_images_id RENAME TO ix_document_images_id')
    
    # Drop the new foreign key constraints
    op.drop_constraint('exif_data_record_image_id_fkey', 'exif_data', type_='foreignkey')
    op.drop_constraint('camera_settings_record_image_id_fkey', 'camera_settings', type_='foreignkey')
    
    # Recreate the old foreign key constraints
    op.create_foreign_key('exif_data_document_image_id_fkey',
                         'exif_data', 'document_images',
                         ['record_image_id'], ['id'])
    op.create_foreign_key('camera_settings_document_image_id_fkey',
                         'camera_settings', 'document_images',
                         ['record_image_id'], ['id'])
    
    # Rename the foreign key columns back
    op.alter_column('exif_data', 'record_image_id',
                    new_column_name='document_image_id',
                    existing_type=sa.Integer(),
                    existing_nullable=False)
    
    op.alter_column('camera_settings', 'record_image_id',
                    new_column_name='document_image_id',
                    existing_type=sa.Integer(),
                    existing_nullable=False)
    
    # Rename the table back
    op.rename_table('record_images', 'document_images')
