from typing import Optional
from pydantic import BaseModel
from datetime import datetime


class ProjectMemberCreate(BaseModel):
    user_id: int
    role: str  # 'operator' | 'reviewer'


class ProjectMemberRead(BaseModel):
    project_id: int
    user_id: int
    role: str
    added_at: datetime
    added_by: Optional[str] = None
    # Flattened user details (joined at query time)
    username: str
    email: str

    class Config:
        from_attributes = True
