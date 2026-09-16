from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Boolean, DateTime, CheckConstraint

from app.core.db import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(150), unique=True, index=True, nullable=False)
    # Optional: the appliance runs offline and nothing verifies an address
    # (NEH-162). PostgreSQL treats NULLs as distinct in a unique index, so
    # several users without an email are allowed.
    email = Column(String(255), unique=True, index=True, nullable=True)
    hashed_password = Column(String(255), nullable=False)
    role = Column(String(50), default="reviewer", nullable=False)  # admin, operator, reviewer
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    __table_args__ = (
        CheckConstraint("role IN ('admin','operator','reviewer')", name='check_user_role'),
    )
