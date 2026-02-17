from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, JSON, CheckConstraint
from sqlalchemy.orm import relationship

from app.core.db import Base


class Collection(Base):
    __tablename__ = "collections"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False, index=True)
    description = Column(Text, nullable=True)
    
    # Type of collection (e.g., "fonds", "series", "box", "folder", "volume")
    collection_type = Column(String(50), nullable=True)
    
    # Parent relationships - either under a project OR another collection
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=True)
    parent_collection_id = Column(Integer, ForeignKey("collections.id", ondelete="CASCADE"), nullable=True)
    
    # Metadata for archival-specific fields (flexible JSON)
    archival_metadata = Column(JSON, nullable=True)
    
    created_by = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # Relationships
    project = relationship("Project", back_populates="collections")
    parent_collection = relationship("Collection", remote_side=[id], back_populates="child_collections")
    child_collections = relationship("Collection", back_populates="parent_collection", cascade="all, delete-orphan")
    records = relationship("RecordImage", back_populates="collection")

    # Constraint: must have either project_id OR parent_collection_id (but not both)
    __table_args__ = (
        CheckConstraint(
            '(project_id IS NOT NULL AND parent_collection_id IS NULL) OR (project_id IS NULL AND parent_collection_id IS NOT NULL)',
            name='check_collection_parent'
        ),
    )
