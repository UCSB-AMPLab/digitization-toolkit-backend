from sqlalchemy import Column, Integer, String, DateTime
from sqlalchemy.sql import func

from app.core.db import Base


class SystemLog(Base):
    __tablename__ = "system_logs"

    id         = Column(Integer, primary_key=True, index=True)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    level    = Column(String(10),  nullable=False)  # INFO | WARN | ERR
    category = Column(String(20),  nullable=False)  # access | activity | capture | system
    actor    = Column(String(150), nullable=True)   # username who triggered the event
    action   = Column(String(80),  nullable=False)  # login_success | project_created | …
    subject  = Column(String(300), nullable=True)   # affected entity name
    detail   = Column(String(500), nullable=True)   # extra context (IP, count, …)
