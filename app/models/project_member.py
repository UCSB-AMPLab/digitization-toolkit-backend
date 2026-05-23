from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey
from sqlalchemy.orm import relationship

from app.core.db import Base


class ProjectMember(Base):
    __tablename__ = "project_members"

    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    user_id    = Column(Integer, ForeignKey("users.id",    ondelete="CASCADE"), primary_key=True)
    role       = Column(String(50), nullable=False)   # 'operator' | 'reviewer'
    added_at   = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    added_by   = Column(String(255), nullable=True)
