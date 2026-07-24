from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, CheckConstraint
from sqlalchemy.orm import relationship

from app.core.db import Base


class ProjectMember(Base):
    __tablename__ = "project_members"

    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    user_id    = Column(Integer, ForeignKey("users.id",    ondelete="CASCADE"), primary_key=True)
    role       = Column(String(50), nullable=False)   # 'operator' | 'reviewer'
    added_at   = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    added_by   = Column(String(255), nullable=True)

    __table_args__ = (
        CheckConstraint("role IN ('operator','reviewer')", name='check_project_member_role'),
    )
