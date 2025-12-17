from typing import Optional
from pydantic import BaseModel
from datetime import datetime


class ProjectBase(BaseModel):
    name: str
    description: Optional[str] = None


class ProjectCreate(ProjectBase):
    created_by: Optional[str] = None


class ProjectRead(ProjectBase):
    id: int
    created_by: Optional[str] = None
    created_at: Optional[datetime]

    class Config:
        from_attributes = True
