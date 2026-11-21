# Version: 0.1.0-beta

from typing import Optional
from pydantic import AliasChoices, BaseModel, Field
from datetime import datetime


class ProjectBase(BaseModel):
    name: str
    description: Optional[str] = None
    fondo: Optional[str] = None
    serie: Optional[str] = None
    signatura: Optional[str] = None


class ProjectCreate(ProjectBase):
    created_by: Optional[str] = None
    # A testing project's capture format: "JPEG" (the default when left out
    # or null) or "RAW+JPEG", the old name "RAW" included. Refused for an
    # ordinary project (app.api.projects.checked_capture_format).
    capture_format: Optional[str] = None


class ProjectUpdate(BaseModel):
    """Schema for updating a project. All fields optional."""
    name: Optional[str] = None
    description: Optional[str] = None
    fondo: Optional[str] = None
    serie: Optional[str] = None
    signatura: Optional[str] = None
    # A testing project's capture format; left out or null leaves it as it is.
    capture_format: Optional[str] = None


class ProjectRead(ProjectBase):
    """A project as every response gives it.

    `name` is the project's display name (app.models.project.Project.display_name):
    a testing project's stored name without the testing prefix, a real
    project's name as stored. Read from `display_name` on a model, from
    `name` in a dict. `testing` says whether it is a testing project.
    """
    name: str = Field(validation_alias=AliasChoices("display_name", "name"))
    id: int
    created_by: Optional[str] = None
    created_at: Optional[datetime]
    # Read-only: whether the project is testing work (app.core.testing_space).
    testing: bool = False
    # "JPEG" or "RAW+JPEG" on a testing project, null on an ordinary one.
    capture_format: Optional[str] = None

    class Config:
        from_attributes = True
